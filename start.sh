#!/bin/bash
cd /home/botuser/kalshi-bot
source /home/botuser/kalshi-bot/venv/bin/activate
source /home/botuser/kalshi-bot/.env
exec python3 bot.py
