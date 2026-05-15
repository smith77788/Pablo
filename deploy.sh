#!/bin/bash
# deploy.sh — Nevesty Models one-command Docker deploy
# Usage: ./deploy.sh [--skip-pull] [--branch=BRANCH] [--no-cache]
set -euo pipefail

BRANCH="${BRANCH:-claude/modeling-agency-website-jp2Qd}"
SKIP_PULL=false
NO_CACHE=false
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HEALTH_TIMEOUT=60  # seconds to wait for health check

for arg in "$@"; do
  case $arg in
    --skip-pull) SKIP_PULL=true ;;
    --branch=*) BRANCH="${arg#*=}" ;;
    --no-cache) NO_CACHE=true ;;
  esac
done

log() { echo "[$(date '+%H:%M:%S')] $*"; }
err() { echo "[$(date '+%H:%M:%S')] ERROR: $*" >&2; }

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
mkdir -p data backups logs/bot logs/factory logs/nginx

# 3. Check that nevesty-models .env exists
if [ ! -f "nevesty-models/.env" ]; then
  if [ -f "nevesty-models/.env.example" ]; then
    log "WARNING: nevesty-models/.env not found — copying from .env.example"
    cp nevesty-models/.env.example nevesty-models/.env
    echo ""
    echo "  Edit nevesty-models/.env with your secrets before running again!"
    echo ""
    exit 1
  else
    err "nevesty-models/.env not found and no .env.example available"
    exit 1
  fi
fi

# 4. Check that factory .env exists (create from example if not)
if [ ! -f "factory/.env" ]; then
  if [ -f "factory/.env.example" ]; then
    log "WARNING: factory/.env not found — copying from factory/.env.example"
    cp factory/.env.example factory/.env
    echo ""
    echo "  Edit factory/.env with your ANTHROPIC_API_KEY and other values!"
    echo ""
  elif [ -f ".env.example" ]; then
    log "WARNING: factory/.env not found — copying from root .env.example"
    cp .env.example factory/.env
    echo ""
    echo "  Edit factory/.env with your ANTHROPIC_API_KEY and other values!"
    echo ""
  else
    log "WARNING: factory/.env not found — factory service may fail to start"
  fi
fi

# 5. Build Docker images
BUILD_ARGS=""
if [ "$NO_CACHE" = true ]; then
  BUILD_ARGS="--no-cache"
  log "Building Docker images (no cache)..."
else
  log "Building Docker images..."
fi
docker-compose build $BUILD_ARGS

# 6. Restart services gracefully
log "Stopping existing services..."
docker-compose down --remove-orphans

log "Starting services..."
docker-compose up -d

# 7. Wait for health check on nevesty-bot
log "Waiting for nevesty-bot health check (up to ${HEALTH_TIMEOUT}s)..."
elapsed=0
until curl -sf http://localhost:3000/api/health > /dev/null 2>&1; do
  if [ "$elapsed" -ge "$HEALTH_TIMEOUT" ]; then
    err "Health check timed out after ${HEALTH_TIMEOUT}s"
    log "Last 20 lines of nevesty-bot logs:"
    docker-compose logs --tail=20 nevesty-models || true
    exit 1
  fi
  sleep 3
  elapsed=$((elapsed + 3))
done
log "Health check passed after ${elapsed}s"

# 8. Show running services
log "Service status:"
docker-compose ps

log "=== Deploy complete! ==="
echo ""
echo "  App URL  : http://localhost:3000"
echo "  Admin    : http://localhost:3000/admin"
echo "  Health   : http://localhost:3000/api/health"
echo "  Logs bot : docker-compose logs -f nevesty-models"
echo "  Logs fac : docker-compose logs -f nevesty-factory"
echo "  Stop     : docker-compose down"
echo ""
