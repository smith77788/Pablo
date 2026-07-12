#!/usr/bin/env bash
# Run both bots in one Railway container:
#   - tg-manager  : primary customer bot, foreground (behaviour unchanged)
#   - assistant   : personal Claude bot, background, self-supervising
#
# The assistant lives in its own venv (/app/pablo/.venv) and its own code tree
# (/app/pablo), so it can NEVER interfere with tg-manager's deps or runtime.
set -u

# --- personal assistant bot (background, isolated, never fatal) --------------
(
  cd /app/pablo 2>/dev/null || { echo "[start-all] /app/pablo missing, assistant skipped"; exit 0; }
  while true; do
    /app/pablo/.venv/bin/python -u -m assistant || true
    echo "[start-all] assistant process returned — restarting in 5s"
    sleep 5
  done
) &

# --- primary: tg-manager customer bot (foreground) --------------------------
# exec keeps it as PID 1 so Railway's restart policy behaves exactly as before.
cd /app
exec python main.py
