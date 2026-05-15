#!/bin/bash
# deploy.sh — Nevesty Models deployment script
# Usage: ./deploy.sh [--skip-tests] [--branch=BRANCH]
set -euo pipefail

BRANCH="${BRANCH:-claude/modeling-agency-website-jp2Qd}"
SKIP_TESTS=false
APP_DIR="$(cd "$(dirname "$0")" && pwd)"
NEVESTY_DIR="$APP_DIR/nevesty-models"

for arg in "$@"; do
  case $arg in
    --skip-tests) SKIP_TESTS=true ;;
    --branch=*) BRANCH="${arg#*=}" ;;
  esac
done

log() { echo "[$(date '+%H:%M:%S')] $*"; }

log "=== Nevesty Models Deploy ==="
log "Branch: $BRANCH"

# 1. Pull latest
log "Pulling from origin/$BRANCH..."
cd "$APP_DIR"
git fetch origin "$BRANCH"
git checkout "$BRANCH"
git pull origin "$BRANCH"

# 2. Install Node.js dependencies
log "Installing Node.js dependencies..."
cd "$NEVESTY_DIR"
npm ci --production

# 3. Run tests (unless skipped)
if [ "$SKIP_TESTS" = false ]; then
  log "Running tests..."
  NODE_ENV=test npm test || { log "TESTS FAILED — aborting deploy!"; exit 1; }
  log "Tests passed."
fi

# 4. Syntax check
log "Syntax check..."
node --check bot.js
node --check routes/api.js
node --check server.js
log "Syntax OK."

# 5. Backup DB before reload
if [ -f "data.db" ]; then
  log "Backing up database..."
  bash scripts/backup.sh || log "Backup warning: ${?}"
fi

# 6. Reload PM2
log "Reloading PM2 processes..."
cd "$APP_DIR"
pm2 reload all --update-env || {
  log "PM2 reload failed — attempting restart..."
  pm2 restart all --update-env
}

# 7. Health check
log "Waiting for health check..."
PORT="${PORT:-3000}"
for i in $(seq 1 15); do
  sleep 2
  if curl -sf "http://localhost:$PORT/api/health" > /dev/null 2>&1; then
    log "Health check passed."
    break
  fi
  if [ "$i" -eq 15 ]; then
    log "WARNING: Health check not passing after 30s. Check logs."
  fi
done

log "=== Deploy complete! ==="
pm2 list
