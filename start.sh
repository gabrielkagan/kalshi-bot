#!/bin/bash
cd /home/botuser/kalshi-bot-repo
source /home/botuser/kalshi-bot-repo/venv/bin/activate
source /home/botuser/kalshi-bot-repo/.env
exec python3 -m bot
