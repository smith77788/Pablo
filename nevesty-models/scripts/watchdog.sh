#!/bin/bash
# Watchdog: restart bot if it's not polling
while true; do
  sleep 60
  if ! pm2 list 2>/dev/null | grep -q "nevesty-models.*online"; then
    echo "[watchdog] $(date): Bot not online, restarting..."
    pm2 start ecosystem.config.js 2>/dev/null
  fi
done
