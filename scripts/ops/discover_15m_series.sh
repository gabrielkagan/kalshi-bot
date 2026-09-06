#!/bin/bash
# Daily 15M-series discovery wrapper (launchd: io.kalshi.15m-discovery; plist
# at scripts/ops/launchd/io.kalshi.15m-discovery.plist). Mac-local per the
# VPS-compute-isolation rule. Ticket 86bbvdc8y. Logs to ~/kalshi-15m-discovery
# (never /tmp — memory: feedback_monitor_the_monitor). Sources the repo .env
# (if present) for TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID ONLY so they reach the alert path
# under launchd; without them the script falls back to a macOS notification.
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
cd /Users/gabrielkagan/Documents/kalshi-bot || exit 1
mkdir -p "$HOME/kalshi-15m-discovery"
# Export ONLY the two Telegram vars from .env (not the whole bot env).
if [ -f .env ]; then
  export TELEGRAM_BOT_TOKEN="$(grep -E '^TELEGRAM_BOT_TOKEN=' .env | tail -1 | cut -d= -f2- | tr -d '"')"
  export TELEGRAM_CHAT_ID="$(grep -E '^TELEGRAM_CHAT_ID=' .env | tail -1 | cut -d= -f2- | tr -d '"')"
fi
exec /usr/bin/python3 -m scripts.ops.discover_15m_series \
  --state-dir "$HOME/kalshi-15m-discovery" >> "$HOME/kalshi-15m-discovery/run.log" 2>&1
