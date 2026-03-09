# Database Sync — Canonical Pattern

Copy state.db from VPS to local `/tmp/state.db` for querying. Must be done before any skill that reads DB data.

## Steps

### 1. Check if /tmp/state.db is already fresh
```bash
stat -f "%Sm" -t "%Y-%m-%dT%H:%M:%S" /tmp/state.db 2>/dev/null || echo "NOT_FOUND"
```
- If the file exists and was modified **less than 5 minutes ago**, skip the sync and reuse it.
- If the user says "fresh" or "re-sync", always sync regardless of age.
- If the file is missing or older than 5 minutes, proceed with sync.

### 2. Checkpoint WAL on VPS
```bash
ssh botuser@45.55.181.30 "cd ~/kalshi-bot-repo && python3 -c \"import sqlite3; c=sqlite3.connect('state.db'); c.execute('PRAGMA wal_checkpoint(PASSIVE)'); c.close()\""
```
**Why PASSIVE?** PASSIVE checkpoints without blocking the live bot's writes. TRUNCATE or FULL would block the writer thread, risking missed ticks. PASSIVE is always safe.

**Why checkpoint at all?** SQLite WAL mode keeps recent writes in a separate `-wal` file. Without checkpointing, SCP copies only the main DB file and misses the latest data — sometimes hours of writes.

### 3. SCP the database
```bash
scp botuser@45.55.181.30:~/kalshi-bot-repo/state.db /tmp/state.db
```

### 4. Verify the copy
```bash
python3 -c "import sqlite3; c=sqlite3.connect('/tmp/state.db'); c.execute('PRAGMA busy_timeout=10000'); r=c.execute('SELECT MAX(evaluation_time) FROM evaluated_opportunities').fetchone(); print(f'Latest eval: {r[0]}'); c.close()"
```
The latest eval should be within the last few minutes during market hours. If it's hours old, the bot may not be scanning — flag this.

## Error Handling

| Error | Cause | Action |
|-------|-------|--------|
| `ssh: connect to host ... Connection refused` | VPS down or SSH blocked | Tell user "VPS unreachable". Try MCP fallback: `mcp__kalshi-vps__query_db`. If that also fails, the VPS is down — escalate. |
| `scp: ... No such file or directory` | Bot hasn't created state.db yet (unlikely) | Check if bot service is running: `ssh botuser@45.55.181.30 "systemctl is-active kalshi-bot"` |
| `database is locked` on verification query | Shouldn't happen on local copy; if it does, file is corrupted | Re-run SCP. If persistent, the VPS DB may be corrupted — escalate to user. |
| Latest eval is hours old during market hours | Bot may be crashed or stuck | Flag immediately. Check VPS logs: `ssh botuser@45.55.181.30 "journalctl -u kalshi-bot --no-pager -n 20"` |

## MCP Fallback
If SSH is unavailable, use the MCP tools as a fallback for quick queries:
```
mcp__kalshi-vps__checkpoint_wal
mcp__kalshi-vps__query_db with SQL query
```
MCP is slower and limited to single queries, so prefer SSH+SCP for full audits. But for quick status checks, MCP can work without a local DB copy.
