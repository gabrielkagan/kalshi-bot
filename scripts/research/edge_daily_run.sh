#!/bin/bash
# Daily edge-tracker wrapper (launchd: io.kalshi.edge-daily). Mac-local per the
# VPS-compute-isolation rule. Pulls clean bronze, runs the markout MM evaluator,
# alerts on any gate survivor. Ticket umbrella 86ba747ke.
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
cd /Users/gabrielkagan/Documents/kalshi-bot || exit 1
exec "/usr/bin/python3" -m scripts.research.edge_daily_run --hours 30 \
  --workdir "$HOME/kalshi-edge-daily" >> "$HOME/kalshi-edge-daily/run.log" 2>&1
