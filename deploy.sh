#!/bin/bash
# deploy.sh — Nevesty Models one-command Docker deploy
# Usage: ./deploy.sh [--skip-pull] [--branch=BRANCH]
set -euo pipefail

BRANCH="${BRANCH:-claude/modeling-agency-website-jp2Qd}"
SKIP_PULL=false
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

for arg in "$@"; do
  case $arg in
    --skip-pull) SKIP_PULL=true ;;
    --branch=*) BRANCH="${arg#*=}" ;;
  esac
done

log() { echo "[$(date '+%H:%M:%S')] $*"; }

cd "$APP_DIR"

log "=== Nevesty Models Docker Deploy ==="
log "Branch: $BRANCH"

# 1. Pull latest code
if [ "$SKIP_PULL" = false ]; then
  log "Pulling from origin/$BRANCH..."
  git pull origin "$BRANCH"
fi

# 2. Ensure required directories exist
log "Ensuring data and backup directories..."
mkdir -p data backups

# 3. Check that factory .env exists (create from example if not)
if [ ! -f "factory/.env" ]; then
  if [ -f ".env.example" ]; then
    log "WARNING: factory/.env not found — copying from .env.example"
    cp .env.example factory/.env
    echo ""
    echo "  Edit factory/.env with your ANTHROPIC_API_KEY and other values!"
    echo ""
  else
    log "WARNING: factory/.env not found — factory service may fail to start"
  fi
fi

# 4. Build and start all services
log "Building Docker images..."
docker-compose build

log "Starting services..."
docker-compose up -d

# 5. Show running services
log "Service status:"
docker-compose ps

log "=== Deploy complete! ==="
echo ""
echo "  App URL  : http://localhost:3000"
echo "  Logs     : docker-compose logs -f nevesty-models"
echo "  Stop     : docker-compose down"
echo ""
