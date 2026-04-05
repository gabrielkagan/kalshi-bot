---
status: pending
updated: 2026-03-28
tags: [research, automation, claude]
---
# Claude Code Automation Architecture — Complete Design

Source: Mar 2026
Chat link: https://claude.ai/chat/c12c48df-22d0-474e-96d4-3510bd7e5081

---

## Concept
Use Claude Code (Opus) as an autonomous research and maintenance agent. Runs on schedule, proposes changes, waits for approval via Telegram.

## Architecture Questions Resolved

### How often does Opus run?
- researcher.py already runs 3x daily digests (existing infrastructure)
- Opus deeper analysis: weekly (e.g., every Sunday full-week review)
- auditor.py: 17 deterministic checks hourly (existing infrastructure)

### How does Opus interact with the codebase?

**Option A: API call with curated context**
- researcher.py packages data + code snippets → send to Opus API → get analysis + code back
- You control exactly what Opus sees
- Cheaper, more predictable
- Opus can't explore on its own

**Option B: Claude CLI invoked programmatically**
- Script runs `claude -p "prompt"` giving Opus full file system access
- More powerful — Opus can read files, run commands, grep codebase
- Harder to control, more expensive (many tool calls)
- Better for investigation-style tasks

### Approval UX
- **Ideal:** Telegram inline buttons — see diff, tap Approve/Reject
- **Requires:** webhook server or polling loop (additional infrastructure)
- **Simpler:** Opus sends proposal to Telegram, you reply "approve" or "reject" as text, listener script picks it up
- **Simplest:** Opus proposes, you go to Claude CLI to execute when ready

### Approval Tiers

**Auto (no approval needed):**
- Kill underperforming shadow variants
- Generate reports and digests
- Investigate anomalies
- Run health checks

**One-tap approval:**
- Bug fixes with clear root cause
- Shadow variant creation
- Parameter tweaks within defined bounds (e.g., ±10% of current value)

**Full conversation required:**
- Promoting shadow to live
- Changing risk parameters
- Anything touching position sizing
- Architectural changes
- New strategy deployment

## Current Automation Layer (what exists)
- researcher.py: 3x daily digests with performance summaries, Telegram delivery
- auditor.py: 17 hourly deterministic checks, Telegram alerts on failures
- watchdog: 4 cron checks for bot health
- GitHub Actions CI/CD: automated testing and deployment

## What Would Change With Opus Autonomy
- Deeper weekly analysis replacing manual Claude Code sessions
- Autonomous shadow variant creation and evaluation
- Proactive bug detection (pattern matching against failure catalog)
- Auto-generated KB updates after significant findings

## Status
Conceptual design only. The existing automation (researcher + auditor + watchdog) handles routine monitoring. Full Opus autonomy would add the "thinking" layer on top. Not implemented — the manual Claude Code workflow is working well enough that automation isn't the bottleneck.

## Related (KB operational articles)
- (No direct kb/ counterpart)
