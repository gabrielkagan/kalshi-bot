---
status: active
updated: 2026-04-04
tags: [openclaw, gemma, automation, security, decision]
date: 2026-04-04
---
# OpenClaw + Gemma Integration — Deferred

## Decision
Evaluated OpenClaw (open-source AI agent, 247K GitHub stars) and Google Gemma 3 (open-weights LLM) for bot operations automation. **Deferred** due to security concerns outweighing benefits for a live trading system. Will build a simple Python Telegram bot instead for the immediate operational need. Revisit OpenClaw only for news regime detection on an isolated server.

## What OpenClaw Is
An open-source personal AI assistant (TypeScript/Node.js) with a Gateway/WebSocket architecture, 24+ messaging platform integrations (Telegram, WhatsApp, etc.), and a skills/plugin system (ClawHub registry with 13,700+ skills). Created by Peter Steinberger (Nov 2025, originally "Clawdbot"), renamed to OpenClaw Jan 2026. Steinberger joined OpenAI Feb 2026, project moved to open-source foundation.

## What Was Evaluated
- OpenClaw as Telegram-based ops interface (replace manual Claude Code sessions)
- Gemma 3 4B (local via Ollama) for analysis/summarization of audit results
- Scheduled Gemma-powered daily briefings replacing bot/ai/researcher.py
- News/event regime detection via RSS + Gemma classification
- Anomaly detection + self-healing via log monitoring

## Why Deferred

### Security (dealbreaker)
1. **SSH keys + OpenClaw = full trading account access.** VPS has Kalshi API key + RSA private key. One compromised skill = access to live trading.
2. **ClawHub supply chain risk.** 2,419 malicious skills purged (Kaspersky reported 21,639 exposed instances). Project is 5 months old.
3. **Telegram as command plane.** Compromised Telegram account/bot token = arbitrary command execution.
4. **Node.js dependency chain.** Hundreds of npm packages vs current pure-Python stack.

### Practical
5. **VPS can't run Gemma.** 1 vCPU / 2GB RAM. Even Gemma 1B needs ~2GB.
6. **MacBook not always on.** Local OpenClaw only monitors when laptop is open — defeats the purpose of always-on monitoring.
7. **TypeScript is foreign.** Entire stack is Python. Debugging across two runtimes adds friction.
8. **Gemma << Claude for complex analysis.** Wrong initial diagnosis is already the #1 friction point with Claude Opus. Gemma 4B would be worse.

### Most of the value already exists
- Scheduled reports: `bot/ai/researcher.py` runs 3x/day via cron
- Anomaly detection: `bot/ai/auditor.py` runs hourly via cron
- Audit automation: 17 Claude Code skills already built
- The only genuine gap: news monitoring and mobile ops interface

## Recommended Alternative
**Python Telegram bot** (200-300 lines, `python-telegram-bot` library):
- Runs on VPS alongside trading bot (always on)
- Responds to `/status`, `/audit`, `/investigate`, `/pnl` commands
- Calls existing scripts directly (no SSH, no new runtime)
- Uses Claude API for NL analysis when needed (already paying for it)
- Zero new attack surface

## When to Revisit
- If a Python-native OpenClaw SDK emerges (removes TypeScript dependency)
- If ClawHub security matures significantly (1+ years of clean track record)
- For news regime detection specifically: could run on isolated VPS with read-only SSH key, no write access to trading VPS
- If VPS is upgraded to 4GB+ RAM: local Gemma 1B becomes feasible for lightweight tasks

## Related
- `kb-research/bot/autoresearch-applicability.md` — Karpathy autoresearch mapped to bot
- `kb-research/infrastructure/claude-code-automation.md` — Claude as autonomous agent
