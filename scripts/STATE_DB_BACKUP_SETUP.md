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
      "ID": "journals-archive",
      "Status": "Enabled",
      "Filter": {"Prefix": "journals/"},
      "Transitions": [
        {"Days": 30, "StorageClass": "DEEP_ARCHIVE"}
      ]
    },
    {
      "ID": "market-obs-archive",
      "Status": "Enabled",
      "Filter": {"Prefix": "market_obs/"},
      "Transitions": [
        {"Days": 0, "StorageClass": "GLACIER_IR"}
      ]
    },
    {
      "ID": "bronze-archive",
      "Status": "Enabled",
      "Filter": {"Prefix": "bronze/"},
      "Transitions": [
        {"Days": 30, "StorageClass": "DEEP_ARCHIVE"}
      ]
    },
    {
      "ID": "silver-archive",
      "Status": "Enabled",
      "Filter": {"Prefix": "silver/"},
      "Transitions": [
        {"Days": 90, "StorageClass": "GLACIER_IR"}
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

D1.5 (ticket `86b9ypna4`, 2026-05-16) extended the template from 2 rules (daily + install-probes) to 6 rules. Two pre-existing prefixes (`journals/` + `market_obs/`) had lifecycle rules applied on the live bucket via side-channel tickets (`86b9xgp7k` + `86b9xcdwg`) but were never folded back into the §2 template — that's exactly the F6 from D0.1 drift (the template lied about what production looked like). D1.5 folds those rules into the template at the AUDIT-CONFIRMED production cadences (`kb/findings/s3-existing-corpus-audit.md` §1): journals → DEEP_ARCHIVE @ 30d; market_obs → GLACIER_IR @ 0d. The NEW `bronze/` + `silver/` rules come from `kb/decisions/data-corpus-architecture.md` §8 (D0.3 operator decision: bronze skips IA — Standard → DEEP_ARCHIVE @ 30d; silver lands at GLACIER_IR @ 90d for regenerable typed-Parquet derivatives). `gold/` intentionally has NO rule (consumer-facing features stay hot at the Standard default; explicit no-op rule would clutter the policy).

**Operator note for an existing pre-D1.5 bucket — GET-merge-PUT, NOT verbatim re-run.** The audit ground truth (`kb/findings/s3-existing-corpus-audit.md` §1) is: the live `kalshi-bot-archive` bucket has 4 rules — `tier-to-glacier-forever` for daily/ at cadence `→ GLACIER_IR @ 7d, never expires` (NOT the 30d→90d cadence in the template above; pre-existing per-bucket drift documented at D0.1 F6), plus the (unrelated) `journals-archive` + `market-obs-archive` + `expire-install-probes` rules that DO match the §2 template's cadences. 3 of 4 audit-state rules match the §2 template; the daily/ rule diverges. **A verbatim `put-bucket-lifecycle-configuration` against the live bucket would silently mutate daily/'s cadence from 7d→GLACIER_IR (the audited live state) to 30d→GLACIER_IR→90d→DEEP_ARCHIVE (the template state) — a real cost + latency change that the operator did not intend.** Always GET-merge-PUT instead:

```bash
aws s3api get-bucket-lifecycle-configuration --bucket "$BUCKET" > current-lifecycle.json
# Hand-merge the NEW bronze-archive + silver-archive rules into current-lifecycle.json
# (keep the live daily/ + journals/ + market_obs/ + install-probes rules verbatim).
aws s3api put-bucket-lifecycle-configuration --bucket "$BUCKET" --lifecycle-configuration file://current-lifecycle.json
```

The verbatim `lifecycle.json` template above is for a FRESH bucket where no prior lifecycle exists.

```bash
aws s3api put-bucket-lifecycle-configuration \
    --bucket "$BUCKET" \
    --lifecycle-configuration file://lifecycle.json
```

Six rules (D1.5 template expansion; pre-D1.5 the TEMPLATE had only `tier-to-glacier-forever` + `expire-install-probes` — the live bucket separately picked up `journals-archive` + `market-obs-archive` via side-channel tickets pre-D1.5, see D0.1 F6):
- **`tier-to-glacier-forever`** — daily snapshots never expire; tier
  to cheaper storage classes over time (Standard → GLACIER_IR @ 30d → DEEP_ARCHIVE @ 90d).
- **`journals-archive`** — bot's per-tick JSONL streams sync'd via
  `kalshi-journal-archives-sync.timer` (ticket `86b9xgp7k`). Standard
  → DEEP_ARCHIVE @ 30d. Never expires.
- **`market-obs-archive`** — bot's `market_observations_continuous`
  table parquet-archived via `kalshi-market-obs-archive.timer` (ticket
  `86b9xcdwg`). Rarely read but want instant retrieval; Standard →
  GLACIER_IR @ 0d. Never expires.
- **`bronze-archive`** — Data Corpus raw WS-frame JSONL.zst written by
  the D1.5 collector. Standard → DEEP_ARCHIVE @ 30d (skip IA per
  `kb/decisions/data-corpus-architecture.md` §8). Never expires.
- **`silver-archive`** — D2.x ETL-produced typed Parquet from bronze.
  Standard → GLACIER_IR @ 90d (regenerable; cheap to keep warm-ish for
  dbt re-runs).  Never expires.
- **`expire-install-probes`** — Round-1 finding A-M1: the installer's
  install-time sentinel (`_install_check/setup-<ts>.txt`) cannot be
  deleted by the writer-IAM (no `s3:DeleteObject*`; §2b Deny is the
  defense-in-depth backstop). Without this rule, every re-run leaves
  another sentinel in Standard storage forever.

Cost projection (100 MB compressed × 365 daily snapshots/yr): ~$0.04/mo
year 1 → ~$0.30/mo year 5 (mostly Deep Archive). Trivial. See plan
doc for the table.

## 2b. Bucket-policy: deny `DeleteObjectVersion` (ransomware protection)

Round-1 finding A-C2 (May 9 2026, pre-D1.5): writer IAM granted
`s3:PutObject` to the bucket — but PutObject **overwrites** an
existing key. A compromised VPS can silently destroy a day's backup
by PUTting garbage at the same key. Versioning saves the prior object
as a noncurrent version, but the attacker can also keep PUTting more
versions to bloat noncurrent storage. (Path C, ticket `86b9xgz66`,
2026-05-16: §3 writer-IAM now grants Put + Get + List per the
rclone HeadObject quirk — the overwrite-vs-versioning analysis here
is unchanged because Get/List add read paths but no new write
surface.)

Defense-in-depth: deny `DeleteObjectVersion` to *all* principals on
this bucket (including root). The reader account can still GET prior
versions; only AWS account root with explicit policy edit can clean up.

Save as `bucket-policy.json`:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "DenyDeleteOnArchivePrefixes",
      "Effect": "Deny",
      "Principal": "*",
      "Action": ["s3:DeleteObjectVersion"],
      "Resource": [
        "arn:aws:s3:::REPLACE_BUCKET_NAME/daily/*",
        "arn:aws:s3:::REPLACE_BUCKET_NAME/journals/*",
        "arn:aws:s3:::REPLACE_BUCKET_NAME/market_obs/*",
        "arn:aws:s3:::REPLACE_BUCKET_NAME/bronze/*",
        "arn:aws:s3:::REPLACE_BUCKET_NAME/silver/*",
        "arn:aws:s3:::REPLACE_BUCKET_NAME/gold/*"
      ]
    }
  ]
}
```

**Important: scope to specific archive prefixes, NOT the whole bucket.**
A previous draft used `Resource: "arn:aws:s3:::BUCKET/*"` (whole
bucket), which would block the `expire-install-probes` lifecycle rule
(lifecycle DELETEs are evaluated against bucket policy in versioned
buckets — the rule needs to delete `_install_check/` noncurrent
versions to free storage). Listing each archive prefix is verbose but
correct: the install-probe deletion path stays clear, and any new
archive prefix added in the future must be added here too.

D1.5 (ticket `86b9ypna4`, 2026-05-16) extended the deny scope from
`daily/*` only to ALL six archive prefixes — the bucket-policy half of
F6 from D0.1 per `kb/decisions/data-corpus-architecture.md` §1 + §15
(F6's other half is §2 lifecycle template; D1.5 §2 closes that half).
**D1.5 §2 + §2b together close F6 in full.** Without the extension, a
compromised collector writer IAM key could silently DELETE bronze
snapshots and the immutability guarantee would not hold.

```bash
# Portable cross-platform sed (works on both GNU/Linux + macOS):
sed -i.bak "s/REPLACE_BUCKET_NAME/$BUCKET/" bucket-policy.json && \
    rm bucket-policy.json.bak
aws s3api put-bucket-policy --bucket "$BUCKET" --policy file://bucket-policy.json
```

To recover from this protection (e.g., disposing of an old test
bucket): edit the policy via console to remove the Deny statement, or
use `aws s3api put-bucket-policy` with an empty Statement array.

## 3. Create writer IAM user (Put + Get + List for rclone, NO Delete)

The paranoia narrows post-Path-C (ticket `86b9xgz66`, 2026-05-16): a
compromised VPS still cannot DELETE prior snapshots (§2b bucket-policy
Deny on `s3:DeleteObjectVersion` is the defense-in-depth backstop), but
the writer DOES need `s3:GetObject` + `s3:ListBucket` for rclone's
pre-PUT `HeadObject` probe. The two grants cover two distinct AWS S3
disclosure rules: `s3:GetObject` is required to HEAD an existing key
(without it, HeadObject returns 403 unconditionally), and `s3:ListBucket`
is required to receive a 404 (NoSuchKey) for a non-existent key
(without it, HeadObject returns 403 to avoid leaking key-existence
information). rclone's pre-PUT probe hits BOTH paths across the
collector's lifetime (re-upload after restart vs first-chunk upload of
a new key), so the writer needs both grants. Without them, `rclone
copyto` fails on at least one of the two destination states.

D1.5 (ticket `86b9ypna4`, 2026-05-16) extended the writer-IAM Resource
set from `daily/*` only to ALL six archive prefixes (`daily/`,
`journals/`, `market_obs/`, `bronze/`, `silver/`, `gold/`). This is
NOT an F6 surface (F6 is §2 lifecycle + §2b bucket-policy per
`kb/findings/s3-existing-corpus-audit.md` §4-F6) — it's adjacent
template-vs-live drift in the same class as F6, surfaced at D1.5
kickoff. Path C (ticket `86b9xgz66`, post-D1.5 follow-up) adds
`s3:GetObject` + `s3:ListBucket` to align the template with the live
policy (which carried Get + List all along — verbatim re-run of the
pre-Path-C template against the live bucket would have silently
DROPPED those grants, same REPLACE-semantics class as the §2
lifecycle clobber). Without the `bronze/*` Resource grant, the
collector's first
`rclone copyto s3prod:kalshi-bot-archive/bronze/...` returns 403, the
KEEP-local-on-failure posture (collector/uploader.py) accumulates
chunks in `outbox/`, and the 2 GB VPS fills the root volume within
hours of bronze day-zero — well before D1.6 disk-pressure alerting
ships.

**Policy-name preserved.** The inline policy is still named `s3-put-only`
for operational compatibility with the live IAM identifier — renaming
would orphan the live policy and require a manual cleanup. The name is
now a misnomer; the actual permission set is Put + Get + List
(documented inline below).

```bash
aws iam create-user --user-name kalshi-state-db-backup-writer

# Save as writer-policy.json:
cat > writer-policy.json <<EOF
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid": "PutAndHeadObject",
            "Effect": "Allow",
            "Action": [
                "s3:PutObject",
                "s3:GetObject"
            ],
            "Resource": [
                "arn:aws:s3:::${BUCKET}/daily/*",
                "arn:aws:s3:::${BUCKET}/journals/*",
                "arn:aws:s3:::${BUCKET}/market_obs/*",
                "arn:aws:s3:::${BUCKET}/bronze/*",
                "arn:aws:s3:::${BUCKET}/silver/*",
                "arn:aws:s3:::${BUCKET}/gold/*"
            ]
        },
        {
            "Sid": "ListBucketForRclone",
            "Effect": "Allow",
            "Action": ["s3:ListBucket"],
            "Resource": "arn:aws:s3:::${BUCKET}"
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

D1.5 (ticket `86b9ypna4`, 2026-05-16) extended the reader scope from
`daily/*` only to ALL six archive prefixes, preemptively unblocking
the D2.x off-VPS silver/gold ETL chain (DuckDB + dbt running on the
operator's Mac reads bronze via this same reader profile — see
`kb/decisions/data-corpus-architecture.md` §13).

Path C (ticket `86b9xgz66`, post-D1.5 follow-up): the
`ListBucketAndVersions` Sid previously carried a 12-element
`Condition.StringLike` `s3:prefix` array (bare + glob form of all 6
prefixes). Path C drops the Condition entirely — the 6 archive
prefixes carry all the durable content; the only other prefix is
`_install_check/` (installer sentinels auto-expired @ 7d by the
`expire-install-probes` lifecycle rule, not durable). The reader can
now `ListBucket` on the bucket root unconditionally, but `GetObject`
remains gated to the 6 archive prefixes via the `ReadOnly` Sid
Resource list — so listing `_install_check/` keys yields names but
no readable bodies. Dropping the Condition removes a frequent
half-extension trap (a bucket whose Condition omitted `bronze/*`
would `AccessDenied` on `aws s3 ls s3://<bucket>/bronze/` even with
`ReadOnly` Resource covering `bronze/*`).

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
            "Resource": [
                "arn:aws:s3:::${BUCKET}/daily/*",
                "arn:aws:s3:::${BUCKET}/journals/*",
                "arn:aws:s3:::${BUCKET}/market_obs/*",
                "arn:aws:s3:::${BUCKET}/bronze/*",
                "arn:aws:s3:::${BUCKET}/silver/*",
                "arn:aws:s3:::${BUCKET}/gold/*"
            ]
        },
        {
            "Sid": "ListBucketAndVersions",
            "Effect": "Allow",
            "Action": ["s3:ListBucket", "s3:ListBucketVersions"],
            "Resource": "arn:aws:s3:::${BUCKET}"
        },
        {
            "Sid": "RestoreObjectFromGlacier",
            "Effect": "Allow",
            "Action": ["s3:RestoreObject"],
            "Resource": [
                "arn:aws:s3:::${BUCKET}/daily/*",
                "arn:aws:s3:::${BUCKET}/journals/*",
                "arn:aws:s3:::${BUCKET}/market_obs/*",
                "arn:aws:s3:::${BUCKET}/bronze/*",
                "arn:aws:s3:::${BUCKET}/silver/*",
                "arn:aws:s3:::${BUCKET}/gold/*"
            ]
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

After the first sub-daily backup runs (next 4h tick: 00/04/08/12/16/20:00 UTC), on the Mac:

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

Round-1 finding B-C1 + Round-2 R2-M4: per the §2 TEMPLATE, snapshots
transition to **Glacier Instant Retrieval (GLACIER_IR)** at day 30 and
**Deep Archive** at day 90. The lifecycle uses GLACIER_IR (instant
access, no thaw needed) NOT GLACIER (Flexible Retrieval, which would
need thaw).

| Age | Storage class | Restore needed? | Latency |
|---|---|---|---|
| 0–30 d | STANDARD | no | ms |
| 30–90 d | GLACIER_IR | **no** (instant retrieval) | ms |
| 90 d+ | DEEP_ARCHIVE | **yes** (RestoreObject + 12h thaw) | 12h |

**Audit-confirmed live cadence on `daily/`** (`kb/findings/s3-existing-corpus-audit.md` §1) DIVERGES from the §2 template: the live `tier-to-glacier-forever` rule is `7d → GLACIER_IR (forever)` with NO DEEP_ARCHIVE step. On the live bucket, `daily/` snapshots reach GLACIER_IR at day 7 and stay there forever — they never enter DEEP_ARCHIVE. The thaw path below applies to (a) a fresh bucket where the §2 template cadence has been applied verbatim, or (b) the D1.5 `bronze-archive` rule which DOES transition to DEEP_ARCHIVE @ 30d for bronze/* objects.

So in practice on the audited live bucket: daily/ snapshots NEVER need a thaw step (GLACIER_IR is instant-access). bronze/* snapshots > 30 days old DO need the thaw step below.
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

Operator-only one-time install. Closes the B-M3 gap: the sub-daily
(every-4h post-86b9zkp89) backup timer on the VPS could be disabled /
hung / unit-file-rejected and nobody would notice until the weekly
verify runs ~6 days later. This heartbeat catches it within ~14h
worst case (8h staleness threshold + cron-every-6h granularity = up
to 6h between heartbeat ticks).

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
(8h staleness threshold post-86b9zkp89; pre-86b9zkp89 was 36h with
12h slack — under the tighter 8h threshold the cutover-gap matters
more, so coordinate the swap with operator awareness), but worth
knowing during the cutover.

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

D1.5 (ticket `86b9ypna4`, 2026-05-16) closed F6 from D0.1 — `§2 lifecycle` + `§2b bucket-policy` templates folded to the canonical 6-prefix shape: `daily/`, `journals/`, `market_obs/`, `bronze/`, `silver/`, `gold/`. D1.5 ALSO extended `§3 writer-IAM Resource` + `§5 reader-IAM scope` to the same 6 prefixes — adjacent template-vs-live drift in the same class as F6 but outside the original F6 scope. Path C (ticket `86b9xgz66`, 2026-05-16) closed the `§3 writer-IAM Actions` gap (rclone HeadObject quirk: added `s3:GetObject` + `s3:ListBucket`) + dropped `§5 reader-IAM Condition.StringLike` (cosmetic) — also adjacent template-vs-live drift, same class as F6 but outside F6's scope. Re-running §1-§5 on a fresh bucket now produces the correct multi-prefix policy AND the rclone-compatible Action set without hand-edits.

If you are operating an EXISTING bucket that was provisioned before D1.5 (i.e., before 2026-05-16), the templates partition into TWO classes per the **§2 Operator note** above:

- **§2 lifecycle** — `put-bucket-lifecycle-configuration` is REPLACE semantics, but the audited live bucket has a `tier-to-glacier-forever` rule for `daily/` at cadence `7d → GLACIER_IR (forever)` that DIVERGES from the §2 template's `30d → GLACIER_IR → 90d → DEEP_ARCHIVE`. **Verbatim re-run would silently mutate daily/'s cadence — DON'T.** Use `GET-merge-PUT` per the §2 Operator note (`aws s3api get-bucket-lifecycle-configuration` → hand-merge the NEW `bronze-archive` + `silver-archive` rules into `current-lifecycle.json` → `put-bucket-lifecycle-configuration`).
- **§2b bucket policy / §3 writer-IAM / §5 reader-IAM** — post-Path-C these `put-bucket-policy` / `put-user-policy` calls ARE idempotent and safe to re-run verbatim. (Pre-Path-C, verbatim re-run of §3 against a live bucket would have DROPPED the Get/List grants the live IAM carried for rclone — that gap is what Path C closes. Operators reading git blame on an existing bucket from before 2026-05-16 should run the post-Path-C templates.)

Verify with:
```bash
aws s3api get-bucket-lifecycle-configuration --bucket <bucket> | jq '[.Rules[].ID] | sort'
# Expected: ["bronze-archive", "expire-install-probes", "journals-archive", "market-obs-archive", "silver-archive", "tier-to-glacier-forever"]
aws s3api get-bucket-policy --bucket <bucket> | jq -r '.Policy' | jq '.Statement[].Resource'
# Expected: 6-element array covering daily/journals/market_obs/bronze/silver/gold *
aws iam get-user-policy --user-name kalshi-state-db-backup-writer --policy-name s3-put-only | jq '[.PolicyDocument.Statement[].Sid] | sort'
# Expected post-Path-C: ["InstallProbe", "ListBucketForRclone", "PutAndHeadObject"]
aws iam get-user-policy --user-name kalshi-state-db-backup-writer --policy-name s3-put-only | jq '.PolicyDocument.Statement[] | select(.Sid=="PutAndHeadObject").Action | sort'
# Expected: ["s3:GetObject", "s3:PutObject"] — Get is the rclone HeadObject quirk requirement (closes ticket 86b9xgz66).
aws iam get-user-policy --user-name kalshi-state-db-backup-writer --policy-name s3-put-only | jq '.PolicyDocument.Statement[] | select(.Sid=="PutAndHeadObject").Resource'
# Expected: 6-element array covering daily/journals/market_obs/bronze/silver/gold * — the canonical D1.5 archive set.
aws iam get-user-policy --user-name kalshi-state-db-backup-reader --policy-name s3-get-only | jq '.PolicyDocument.Statement[] | select(.Sid=="ReadOnly" or .Sid=="RestoreObjectFromGlacier").Resource'
# Expected: ReadOnly + RestoreObjectFromGlacier each carry the 6-element array.
aws iam get-user-policy --user-name kalshi-state-db-backup-reader --policy-name s3-get-only | jq '.PolicyDocument.Statement[] | select(.Sid=="ListBucketAndVersions") | {Resource, Condition}'
# Expected post-Path-C: Resource is the bucket root, Condition is null/absent. (Pre-Path-C this Sid carried a 12-element StringLike Condition; Path C dropped it because `GetObject` is still gated to the 6 archive prefixes via the `ReadOnly` Sid Resource list — listing the bucket root only exposes key NAMES, not bodies. The 7th prefix `_install_check/` auto-expires @ 7d.)
```

All seven verify one-liners must return the post-Path-C shape:

- Lifecycle: rule IDs include `bronze-archive`, `journals-archive`, `market-obs-archive`, `silver-archive`, `tier-to-glacier-forever`, `expire-install-probes`.
- Bucket policy Deny: Resource is a 6-element archive-prefix array (`daily/*`, `journals/*`, `market_obs/*`, `bronze/*`, `silver/*`, `gold/*`).
- Writer-IAM Sids: `[InstallProbe, ListBucketForRclone, PutAndHeadObject]`.
- Writer-IAM `PutAndHeadObject` Actions: `[s3:GetObject, s3:PutObject]`.
- Writer-IAM `PutAndHeadObject` Resource: 6-element archive-prefix array.
- Reader-IAM `ReadOnly` + `RestoreObjectFromGlacier` Resources: 6-element archive-prefix array each.
- Reader-IAM `ListBucketAndVersions`: Resource is bucket root, Condition is absent.

A bucket whose `s3-put-only` user-policy is missing `bronze/*` from the Put-target Resource list is the canonical pre-D1.5 state and will 403 on the collector's first chunk upload (pre-D1.5 the Sid was `PutObjectsOnly` with `daily/*`-only scope; post-Path-C the equivalent Sid is `PutAndHeadObject` with 6-prefix scope). A bucket whose `s3-put-only` user-policy is missing `s3:GetObject` from the Put-target Actions is the canonical pre-Path-C state and will 403 on rclone's pre-PUT `HeadObject` probe.

### 12.1 Install the timer

```bash
ssh -t botuser@$VPS_HOST 'bash /home/botuser/kalshi-bot-repo/scripts/ops/setup_journal_archives_sync_timer.sh'
```

The installer:
- Pre-flights: rclone present, `s3prod` remote configured, `S3_BACKUP_BUCKET` in `.env`, `/var/lock` writable, Telegram creds present (warn-only).
- Installs `kalshi-journal-archives-sync.{service,timer}` at `/etc/systemd/system/`.
- Enables + starts the timer (next fire: next 4h tick at HH:30 UTC ∈ {00,04,08,12,16,20}; cadence revised by ticket `86b9zkp89` 2026-05-17).

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
