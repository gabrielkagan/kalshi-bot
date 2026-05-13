# state.db S3 backup — operator setup

One-time bucket + IAM + lifecycle setup. Phase 0a per
`kb/decisions/autoresearch-design-may05.md`. Ticket: 86b9vd9e3.

After this is done once, `scripts/ops/setup_state_db_backup_timer.sh` on
the VPS handles everything else (timers, rclone config, sentinel
upload to verify creds).

## 1. Create the S3 bucket

```bash
# Pick a region close to the VPS (NYC3 → us-east-1 is cheapest egress).
AWS_REGION=us-east-1
BUCKET=kalshi-state-db-backup-$(openssl rand -hex 3)  # collision-resistant suffix

aws s3api create-bucket \
    --bucket "$BUCKET" \
    --region "$AWS_REGION"
# (us-east-1 doesn't take --create-bucket-configuration; other regions do.)

# Block all public access (defense in depth).
aws s3api put-public-access-block \
    --bucket "$BUCKET" \
    --public-access-block-configuration \
    "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"

# Enable versioning (so writer compromise can't silently overwrite history).
aws s3api put-bucket-versioning \
    --bucket "$BUCKET" \
    --versioning-configuration Status=Enabled

# Enable default encryption.
aws s3api put-bucket-encryption \
    --bucket "$BUCKET" \
    --server-side-encryption-configuration '{
        "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]
    }'

echo "Bucket created: $BUCKET in $AWS_REGION"

# Round-3 B3-M3: persist the bucket name so subsequent steps + later
# shell sessions can recover it. Both step 4 (VPS .env) and step 6
# (Mac restore) reference $BUCKET — losing the random suffix means
# `aws s3api list-buckets | grep kalshi-state-db-backup-` to recover.
echo "BUCKET=$BUCKET" > ~/kalshi-state-db-backup.env
echo "AWS_REGION=$AWS_REGION" >> ~/kalshi-state-db-backup.env
echo "Bucket name saved to ~/kalshi-state-db-backup.env. To resume in a fresh shell:"
echo "  source ~/kalshi-state-db-backup.env"
```

## 2. Apply lifecycle policy (retain forever, tier to Glacier)

Save as `lifecycle.json`:

```json
{
  "Rules": [
    {
      "ID": "tier-to-glacier-forever",
      "Status": "Enabled",
      "Filter": {"Prefix": "daily/"},
      "Transitions": [
        {"Days": 30,  "StorageClass": "GLACIER_IR"},
        {"Days": 90,  "StorageClass": "DEEP_ARCHIVE"}
      ],
      "NoncurrentVersionTransitions": [
        {"NoncurrentDays": 30, "StorageClass": "GLACIER_IR"},
        {"NoncurrentDays": 90, "StorageClass": "DEEP_ARCHIVE"}
      ]
    },
    {
      "ID": "expire-install-probes",
      "Status": "Enabled",
      "Filter": {"Prefix": "_install_check/"},
      "Expiration": {"Days": 7}
    }
  ]
}
```

```bash
aws s3api put-bucket-lifecycle-configuration \
    --bucket "$BUCKET" \
    --lifecycle-configuration file://lifecycle.json
```

Two rules:
- **`tier-to-glacier-forever`** — daily snapshots never expire; tier
  to cheaper storage classes over time.
- **`expire-install-probes`** — Round-1 finding A-M1: the installer's
  install-time sentinel (`_install_check/setup-<ts>.txt`) cannot be
  deleted by the writer-IAM (PutObject-only). Without this rule, every
  re-run leaves another sentinel in Standard storage forever.

Cost projection (100 MB compressed × 365 daily snapshots/yr): ~$0.04/mo
year 1 → ~$0.30/mo year 5 (mostly Deep Archive). Trivial. See plan
doc for the table.

## 2b. Bucket-policy: deny `DeleteObjectVersion` (ransomware protection)

Round-1 finding A-C2: writer IAM grants `s3:PutObject` only — but
PutObject **overwrites** an existing key. A compromised VPS can
silently destroy a day's backup by PUTting garbage at the same key.
Versioning saves the prior object as a noncurrent version, but the
attacker can also keep PUTting more versions to bloat noncurrent
storage.

Defense-in-depth: deny `DeleteObjectVersion` to *all* principals on
this bucket (including root). The reader account can still GET prior
versions; only AWS account root with explicit policy edit can clean up.

Save as `bucket-policy.json`:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "DenyDeleteOnDailySnapshots",
      "Effect": "Deny",
      "Principal": "*",
      "Action": ["s3:DeleteObjectVersion"],
      "Resource": "arn:aws:s3:::REPLACE_BUCKET_NAME/daily/*"
    }
  ]
}
```

**Important: scope to `daily/*` only.** A previous draft used
`Resource: "arn:aws:s3:::BUCKET/*"` (whole bucket), which would block
the `expire-install-probes` lifecycle rule (lifecycle DELETEs are
evaluated against bucket policy in versioned buckets — the rule needs
to delete `_install_check/` noncurrent versions to free storage).
`daily/*` is the resource we actually care about protecting.

```bash
# Portable cross-platform sed (works on both GNU/Linux + macOS):
sed -i.bak "s/REPLACE_BUCKET_NAME/$BUCKET/" bucket-policy.json && \
    rm bucket-policy.json.bak
aws s3api put-bucket-policy --bucket "$BUCKET" --policy file://bucket-policy.json
```

To recover from this protection (e.g., disposing of an old test
bucket): edit the policy via console to remove the Deny statement, or
use `aws s3api put-bucket-policy` with an empty Statement array.

## 3. Create writer IAM user (PutObject ONLY, no Delete/Get/List)

This is the key paranoia: a compromised VPS can upload garbage but
can't delete or read prior snapshots.

```bash
aws iam create-user --user-name kalshi-state-db-backup-writer

# Save as writer-policy.json:
cat > writer-policy.json <<EOF
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid": "PutObjectsOnly",
            "Effect": "Allow",
            "Action": ["s3:PutObject"],
            "Resource": "arn:aws:s3:::${BUCKET}/daily/*"
        },
        {
            "Sid": "InstallProbe",
            "Effect": "Allow",
            "Action": ["s3:PutObject"],
            "Resource": "arn:aws:s3:::${BUCKET}/_install_check/*"
        }
    ]
}
EOF

aws iam put-user-policy \
    --user-name kalshi-state-db-backup-writer \
    --policy-name s3-put-only \
    --policy-document file://writer-policy.json

aws iam create-access-key --user-name kalshi-state-db-backup-writer
# ^^ JSON output: copy AccessKeyId + SecretAccessKey into VPS .env (step 4).
```

## 4. Drop creds in VPS .env

Append to `/home/botuser/kalshi-bot-repo/.env`:

```
S3_BACKUP_BUCKET=<the bucket name from step 1>
S3_BACKUP_REGION=us-east-1
S3_BACKUP_AWS_ACCESS_KEY_ID=<AccessKeyId from step 3>
S3_BACKUP_AWS_SECRET_ACCESS_KEY=<SecretAccessKey from step 3>
```

## 5. Create reader IAM user (Get + List + RestoreObject)

Round-1 A-C2: reader needs `s3:ListBucketVersions` so versioning is
actually usable as the recovery mechanism (the writer can overwrite a
key; reader needs to enumerate prior versions to find the un-tampered
one). Reader also needs `s3:RestoreObject` to thaw Glacier-tiered
snapshots during incident recovery.

```bash
aws iam create-user --user-name kalshi-state-db-backup-reader

cat > reader-policy.json <<EOF
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid": "ReadOnly",
            "Effect": "Allow",
            "Action": ["s3:GetObject", "s3:GetObjectVersion"],
            "Resource": "arn:aws:s3:::${BUCKET}/daily/*"
        },
        {
            "Sid": "ListBucketAndVersions",
            "Effect": "Allow",
            "Action": ["s3:ListBucket", "s3:ListBucketVersions"],
            "Resource": "arn:aws:s3:::${BUCKET}",
            "Condition": {"StringLike": {"s3:prefix": ["daily/*", "daily/"]}}
        },
        {
            "Sid": "RestoreObjectFromGlacier",
            "Effect": "Allow",
            "Action": ["s3:RestoreObject"],
            "Resource": "arn:aws:s3:::${BUCKET}/daily/*"
        }
    ]
}
EOF

aws iam put-user-policy \
    --user-name kalshi-state-db-backup-reader \
    --policy-name s3-get-only \
    --policy-document file://reader-policy.json

aws iam create-access-key --user-name kalshi-state-db-backup-reader
# ^^ Copy these creds into ~/.aws/credentials on the dev Mac as a
#    new profile (step 6).
```

## 6. Set up reader profile on the dev Mac

Append to `~/.aws/credentials`:

```ini
[kalshi-state-db-restore]
aws_access_key_id = <reader AccessKeyId>
aws_secret_access_key = <reader SecretAccessKey>
region = us-east-1
```

Configure local rclone for restore:

```bash
rclone config create kalshi-restore s3 \
    provider AWS \
    env_auth false \
    access_key_id "$(aws --profile kalshi-state-db-restore configure get aws_access_key_id)" \
    secret_access_key "$(aws --profile kalshi-state-db-restore configure get aws_secret_access_key)" \
    region us-east-1 \
    location_constraint us-east-1
```

To restore manually from the Mac:

```bash
python3 scripts/ops/state_db_restore.py \
    --store s3 \
    --rclone-remote kalshi-restore \
    --bucket "$BUCKET" \
    --to /tmp/recovered.db
```

## 7. Install timers on VPS

```bash
ssh -t botuser@$VPS_HOST 'cd ~/kalshi-bot-repo && git fetch origin main && git reset --hard origin/main && bash scripts/ops/setup_state_db_backup_timer.sh'
```

The installer runs an end-to-end probe (uploads a tiny sentinel file)
to verify the writer creds work. If anything fails it tells you what
to fix.

## 8. Verified-restore acceptance test (AC #3)

After the first nightly backup runs (next 06:00 UTC), on the Mac:

```bash
python3 scripts/ops/state_db_restore.py \
    --store s3 \
    --rclone-remote kalshi-restore \
    --bucket "$BUCKET"
```

Expected output:

```
state_db_restore: latest key = daily/state-db-2026-05-09.db.zst
state_db_restore: downloaded 100123456 bytes
state_db_restore: decompressed -> /tmp/.../state-db-2026-05-09.db
state_db_restore: integrity_check = ok
state_db_restore: snapshot row counts: {'settled_trades': N, 'evaluated_opportunities': M, 'rejected_opportunities': K}
state_db_restore: no --baseline-from-live; skipping row-count parity
```

Document the row counts in the shipped KB doc as the AC artifact.

## 9. Restoring a tiered snapshot

Round-1 finding B-C1 + Round-2 R2-M4: snapshots transition to **Glacier
Instant Retrieval (GLACIER_IR)** at day 30 and **Deep Archive** at day
90. The lifecycle here uses GLACIER_IR (instant access, no thaw needed)
NOT GLACIER (Flexible Retrieval, which would need thaw).

| Age | Storage class | Restore needed? | Latency |
|---|---|---|---|
| 0–30 d | STANDARD | no | ms |
| 30–90 d | GLACIER_IR | **no** (instant retrieval) | ms |
| 90 d+ | DEEP_ARCHIVE | **yes** (RestoreObject + 12h thaw) | 12h |

So in practice, only snapshots > 90 days old need a thaw step.
`state_db_restore.py` pre-checks via `rclone lsjson` and raises a clear
error pointing here only if the storage class is GLACIER (the Flexible
flavor — shouldn't appear in this lifecycle but defensive) or
DEEP_ARCHIVE.

For a DEEP_ARCHIVE key (default Standard tier — 12 hour retrieval):

```bash
KEY=daily/state-db-2026-01-15.db.zst   # the snapshot you want
aws s3api restore-object \
    --bucket "$BUCKET" \
    --key "$KEY" \
    --restore-request '{"Days":7,"GlacierJobParameters":{"Tier":"Standard"}}'
# Wait 12 hours.
```

Tier options for DEEP_ARCHIVE:
- `Standard` — 12 hours, $0.02/GB
- `Bulk` — 48 hours, $0.0025/GB (cheapest)

GLACIER (Flexible Retrieval) tiers (won't normally apply here, since
our lifecycle uses GLACIER_IR not GLACIER):
- `Expedited` — 1–5 minutes, $0.03/GB (most expensive, fastest)
- `Standard` — 3–5 hours
- `Bulk` — 5–12 hours, cheapest

See https://docs.aws.amazon.com/AmazonS3/latest/userguide/restoring-objects-retrieval-options.html

After `restore-object` returns 200, the snapshot is available as a
**temporary copy in Standard storage** for the requested `Days` period.
Re-run `state_db_restore.py` exactly as in §8.

## 10. Known gaps (deferred follow-ups)

The following are intentionally NOT shipped in Phase 0a:

1. ~~**Heartbeat alerter ("no upload in 36h").**~~ SHIPPED 2026-05-10 as
   Mac-side launchd job — see §11 below. Ticket: 86b9vgjxw. Closes the
   B-M3 silent-failure gap. KB:
   `kb/decisions/phase0a-fu-backup-heartbeat-shipped-may10.md`.

2. **Real-S3 integration test.** Round-1 finding B-M4: the 66 unit
   tests run against `LocalDirStore` and prove the local round-trip is
   correct, but stub `subprocess.run` for S3 paths. Real-S3 integration
   tests gated on `RCLONE_TEST_REMOTE` env var would catch
   actually-rclone-misconfigured deploys. Defer until the test bucket
   exists; not a blocker for Phase 0a ship.

## 11. Install the backup heartbeat alerter (Mac-side launchd)

Operator-only one-time install. Closes the B-M3 gap: the daily backup
timer on the VPS could be disabled / hung / unit-file-rejected and
nobody would notice until the weekly verify runs ~6 days later. This
heartbeat catches it within 6h.

Architectural choice (option (b)) per ticket 86b9vgjxw: heartbeat
lives on the dev Mac, not on the VPS, so it survives VPS-down events
that would otherwise eat the alert. Uses the existing
`kalshi-state-db-restore` AWS profile (read-only) created in §6 —
NO new IAM creation needed.

### 11.1 Prerequisites

- §1–§7 done (bucket exists, reader profile on Mac, at least one
  daily snapshot has uploaded).
- `boto3` installed in your Mac's Python:
  ```bash
  pip3 install --user boto3
  ```
- Telegram bot token + chat ID handy (same values used on the VPS).

### 11.2 Smoke-test the script manually

```bash
cd ~/Documents/kalshi-bot  # or wherever you cloned the repo

# Sanity: dry-run without alerts (no Telegram creds set → skips POST).
AWS_PROFILE=kalshi-state-db-restore \
    python3 scripts/ops/state_db_backup_heartbeat.py --bucket "$BUCKET"
# Expected: `backup_heartbeat: status=ok key=...` and exit code 0.

# Smoke the alert path by passing an absurd threshold:
AWS_PROFILE=kalshi-state-db-restore \
TELEGRAM_BOT_TOKEN=$YOUR_TOKEN \
TELEGRAM_CHAT_ID=$YOUR_CHAT \
    python3 scripts/ops/state_db_backup_heartbeat.py \
    --bucket "$BUCKET" --max-age-hours 0
# Expected: telegram message received, exit code 1.
```

### 11.3 Install the LaunchAgent

The repo ships a template — substitute the placeholders for your
local values:

```bash
REPO_PATH=$(pwd)  # the kalshi-bot repo root
HOME_DIR=$HOME
PLIST_DEST=~/Library/LaunchAgents/io.kalshi.state-db-backup-heartbeat.plist

sed \
    -e "s|REPLACE_REPO_PATH|$REPO_PATH|g" \
    -e "s|REPLACE_HOME_DIR|$HOME_DIR|g" \
    -e "s|REPLACE_BUCKET_NAME|$BUCKET|g" \
    -e "s|REPLACE_TELEGRAM_BOT_TOKEN|$YOUR_TOKEN|g" \
    -e "s|REPLACE_TELEGRAM_CHAT_ID|$YOUR_CHAT|g" \
    scripts/io.kalshi.state-db-backup-heartbeat.plist.template \
    > "$PLIST_DEST"

# Verify the substitutions:
grep REPLACE_ "$PLIST_DEST" && echo "STILL HAS PLACEHOLDERS — abort" || echo "OK"

# Load it:
launchctl unload "$PLIST_DEST" 2>/dev/null  # idempotent re-install
launchctl load "$PLIST_DEST"

# Confirm:
launchctl list | grep io.kalshi.state-db-backup-heartbeat
```

`RunAtLoad=true` in the plist means the first heartbeat fires now
(within a few seconds). Verify a healthy bucket reports OK:

```bash
tail -f ~/Library/Logs/kalshi-state-db-backup-heartbeat.out.log
# Expected line:
# backup_heartbeat: status=ok key='daily/state-db-YYYY-MM-DD.db.zst' age_hours=...
```

### 11.4 Crontab alternative (if you prefer cron over launchd)

**Important — crontab does NOT expand shell variables.** Unlike a
login shell, crontab inherits only cron's own environment (typically
just `PATH`, `LOGNAME`, `HOME`, `SHELL`). `$YOUR_TOKEN`, `$YOUR_CHAT`,
`$BUCKET`, and `$HOME` are NOT substituted at crontab-load time. You
MUST hand-substitute the literal values before pasting into
`crontab -e`, OR generate the line with `sed` first.

Run this snippet locally to print a ready-to-paste line (substitutes
`$BUCKET`, `$YOUR_TOKEN`, `$YOUR_CHAT`, `$HOME` with their current
shell values):

```bash
# Set these once, then run the sed pipeline below:
export YOUR_TOKEN=...  # Telegram bot token
export YOUR_CHAT=...   # Telegram chat ID
# $BUCKET should already be set from §1; if not: source ~/kalshi-state-db-backup.env

cat <<'TEMPLATE' | sed \
    -e "s|@BUCKET@|$BUCKET|g" \
    -e "s|@TOKEN@|$YOUR_TOKEN|g" \
    -e "s|@CHAT@|$YOUR_CHAT|g" \
    -e "s|@HOME@|$HOME|g"
0 */6 * * * AWS_PROFILE=kalshi-state-db-restore TELEGRAM_BOT_TOKEN=@TOKEN@ TELEGRAM_CHAT_ID=@CHAT@ /usr/bin/python3 @HOME@/Documents/kalshi-bot/scripts/ops/state_db_backup_heartbeat.py --bucket @BUCKET@ >> @HOME@/Library/Logs/kalshi-state-db-backup-heartbeat.out.log 2>&1
TEMPLATE

# Copy the printed line, run `crontab -e`, paste, save.
```

Note on cadence: the launchd plist uses `StartInterval=21600` (every
21600 seconds = 6 hours from load-time / last-run); cron's
`0 */6 * * *` runs at wall-clock 00:00 / 06:00 / 12:00 / 18:00 of the
crontab-process timezone. The two are approximately equivalent (both
fire 4×/day) but NOT identical — if the operator switches between
them, the first invocation under the new scheduler may be up to 6h
later than the last invocation under the old one. Not load-bearing
(36h staleness threshold has 12h slack), but worth knowing during
the cutover.

launchd is the more idiomatic choice on macOS (survives reboot,
respects sleep/wake), but cron works equivalently if you already
have other cron jobs and want to keep them together.

**Caveat for both schedulers:** crontab inherits cron's environment
only (typically `PATH=/usr/bin:/bin`), NOT your interactive shell's
environment. If `python3` is at a non-standard path (homebrew, pyenv),
hard-code the full path in the crontab line. Verify with `which
python3` in your interactive shell vs `env -i /usr/bin/which python3`.

### 11.5 Trade-offs of the Mac-side choice

- The heartbeat doesn't fire while the Mac is asleep / off. launchd
  schedules the job for the next wake; cron skips missed runs entirely.
  In practice, an operator away from their Mac for 7+ days has bigger
  problems than a missed heartbeat — but this is a known gap.
- If you want VPS-side observability later, file a follow-up ticket
  for option (a): a separate read-only IAM user on the VPS. That
  requires AWS console work (not Phase 0a-fu scope).

## 12. Journals sync (ticket 86b9xgp7k)

The per-tick forensic JSONL streams under
`~/kalshi-bot-repo/journal_archives/` (`opportunity_journal_*`,
`scan_journal_*`, `rejection_journal_*`, ...) are NOT covered by the
state.db backup. `rotate_journals.sh` deletes them at the 90-day local
retention boundary; without S3 archival the per-tick record is gone
forever. This step installs an incremental sync that runs 30 min after
the rotation cron.

### Pre-reqs

- §1–§3 above completed (S3 bucket + lifecycle + writer IAM + .env on VPS).
- The `s3prod` rclone remote exists on the VPS (`setup_state_db_backup_timer.sh` created it).

### Bucket-side multi-prefix expansion (operator one-time)

§1–§3's lifecycle, deny-policy, and writer-IAM as originally written only cover the `daily/*` prefix. The journals (and market_obs, ticket 86b9xcdwg) need the same protections extended to two additional prefixes. **Ticket 86b9xgz66 tracks updating §1-§3 in this runbook to reflect this multi-prefix shape**; until that lands, the operator must hand-extend:

1. **Lifecycle policy** — extend `Filter` from `{"Prefix": "daily/"}` to use `OrPrefixes`:
   ```json
   "Filter": {"And": {"Prefix": "", "ObjectSizeGreaterThan": 0}}
   ```
   Or split into 3 rules (one per prefix). Apply via `aws s3api put-bucket-lifecycle-configuration`.
2. **Bucket policy** (§2b deny) — extend `Resource` array to include all three:
   ```json
   "Resource": [
     "arn:aws:s3:::<bucket>/daily/*",
     "arn:aws:s3:::<bucket>/journals/*",
     "arn:aws:s3:::<bucket>/market_obs/*"
   ]
   ```
3. **Writer IAM policy** (§3) — extend the `PutObject` resource list to include `arn:aws:s3:::<bucket>/journals/*` and `arn:aws:s3:::<bucket>/market_obs/*`.

Verify with:
```bash
aws s3api get-bucket-lifecycle-configuration --bucket <bucket> | jq '.Rules[].Filter'
aws s3api get-bucket-policy --bucket <bucket> | jq -r '.Policy' | jq '.Statement[].Resource'
aws iam get-user-policy --user-name kalshi-bot-vps-writer --policy-name <policy> | jq '.PolicyDocument.Statement[].Resource'
```

All three must include both `daily/*` AND `journals/*` (AND `market_obs/*` if §86b9xcdwg shipped).

### 12.1 Install the timer

```bash
ssh -t botuser@$VPS_HOST 'bash /home/botuser/kalshi-bot-repo/scripts/ops/setup_journal_archives_sync_timer.sh'
```

The installer:
- Pre-flights: rclone present, `s3prod` remote configured, `S3_BACKUP_BUCKET` in `.env`, `/var/lock` writable, Telegram creds present (warn-only).
- Installs `kalshi-journal-archives-sync.{service,timer}` at `/etc/systemd/system/`.
- Enables + starts the timer (next fire: 04:30 UTC).

The service wraps via `h4_run_with_alert.py` so non-zero exits Telegram-alert.

### 12.2 Smoke fire on demand

The first run uploads the ~11 GB backlog (typical post-rotation: 33 days × ~250-360 MB compressed). It may take up to 1 hour on the VPS uplink.

```bash
sudo systemctl start kalshi-journal-archives-sync.service
journalctl -u kalshi-journal-archives-sync.service --no-pager -n 50
# Expected tail line: journal_sync: OK dest=s3prod:<bucket>/journals/
rclone ls s3prod:<bucket>/journals/ | head -20
# Should list opportunity_journal_*.jsonl.{zst,gz}, scan_journal_*, etc.
```

### 12.3 Idempotency verification (HARD AC)

Re-running the script after a successful first run MUST be a no-op:

```bash
sudo systemctl start kalshi-journal-archives-sync.service
journalctl -u kalshi-journal-archives-sync.service --no-pager -n 20
# Expected: rclone stdout shows "Transferred: 0 / 0" or equivalent zero-byte summary.
```

`rclone copy --checksum --immutable` short-circuits per-file via S3 ETag. If `--immutable` ever fires a divergence (rclone exit 6, "Source and destination exist but do not match: immutable file modified"), that is a bug or tampering — the wrapper escalates to Telegram.

### 12.4 Notes for operators

- `rotation.log` is intentionally excluded from the sync (`--exclude rotation.log` in the script's argv): `rotate_journals.sh` appends to it daily, which would trip `--immutable` and abort the entire sync from day 2 onward. The forensic loss is small (rotation.log just logs which file was rotated when); the journals themselves are the valuable artifacts. If forensics is needed, operator can `scp` rotation.log manually.
- Primitive is `rclone copy` (NOT `sync`). `sync` would mirror local deletions to S3 — when `rotate_journals.sh` prunes a journal at the 90-day boundary, `sync` would DELETE the S3 object too, defeating the entire purpose. `copy` is one-way: upload-or-skip, never delete from destination. The `--checksum` flag keeps it idempotent (re-runs short-circuit per-file via ETag).

### 12.5 Drift-check note

The timer files live at `/etc/systemd/system/kalshi-journal-archives-sync.*` — outside `kalshi-bot.service`'s drift-check scope. Re-running `setup_journal_archives_sync_timer.sh` is the source-of-truth operation for them.
