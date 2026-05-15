#!/bin/bash
# =============================================================================
# Nevesty Models — Production Deploy Script (scripts/deploy.sh)
# Runs from the nevesty-models/ directory on a bare-metal / VPS server.
# For Docker-based deploys see docker-compose.yml in the project root.
#
# Usage:
#   chmod +x scripts/deploy.sh
#   ./scripts/deploy.sh
# =============================================================================
set -euo pipefail

trap 'echo ""; echo "❌ Deploy failed at line $LINENO. Check the output above." >&2; exit 1' ERR

# Resolve project root (parent of scripts/)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_DIR"

echo "============================================="
echo "  Nevesty Models — Production Deploy"
echo "  Directory : $PROJECT_DIR"
echo "  Date      : $(date '+%Y-%m-%d %H:%M:%S')"
echo "============================================="
echo ""

# --------------------------------------------------------------------------
# 1. Pull latest code
# --------------------------------------------------------------------------
CURRENT_BRANCH="$(git -C "$PROJECT_DIR" rev-parse --abbrev-ref HEAD)"
echo "[ 1/9 ] Pulling latest changes (branch: ${CURRENT_BRANCH})..."
git -C "$PROJECT_DIR" fetch origin
git -C "$PROJECT_DIR" pull origin "$CURRENT_BRANCH"
echo "   ✔ Up to date ($(git -C "$PROJECT_DIR" rev-parse --short HEAD))"
echo ""

# --------------------------------------------------------------------------
# 2. Check .env
# --------------------------------------------------------------------------
echo "[ 2/9 ] Checking .env file..."
if [ ! -f ".env" ]; then
  echo "⚠️  WARNING: .env not found! Copy and configure it:"
  echo "   cp .env.example .env && nano .env"
  echo ""
  echo "   Continuing — app may fail without proper config."
  echo ""
else
  echo "   ✔ .env found"
fi

# --------------------------------------------------------------------------
# 3. Install production dependencies
# --------------------------------------------------------------------------
echo "[ 3/9 ] Installing production dependencies..."
npm ci --omit=dev
echo "   ✔ Dependencies installed"
echo ""

# --------------------------------------------------------------------------
# 4. Ensure runtime directories exist
# --------------------------------------------------------------------------
echo "[ 4/9 ] Ensuring runtime directories..."
mkdir -p logs data backups uploads/thumbs uploads/models
echo "   ✔ logs/ data/ backups/ uploads/ ready"
echo ""

# --------------------------------------------------------------------------
# 5. Initialize / migrate database
# --------------------------------------------------------------------------
echo "[ 5/9 ] Initializing database..."
node database.js
echo "   ✔ Database ready"
echo ""

# --------------------------------------------------------------------------
# 6. Seed models (idempotent — skips existing records)
# --------------------------------------------------------------------------
echo "[ 6/9 ] Seeding models..."
node tools/seed-models.js
echo "   ✔ Models seeded"
echo ""

# --------------------------------------------------------------------------
# 7. Check / install PM2
# --------------------------------------------------------------------------
echo "[ 7/9 ] Checking PM2..."
if ! command -v pm2 &>/dev/null; then
  echo "   PM2 not found — installing globally..."
  npm install -g pm2
  echo "   ✔ PM2 installed"
else
  echo "   ✔ PM2 $(pm2 --version) found"
fi
echo ""

# --------------------------------------------------------------------------
# 8. Start or reload via PM2
# --------------------------------------------------------------------------
echo "[ 8/9 ] Starting / reloading application..."
if pm2 describe nevesty-models &>/dev/null 2>&1; then
  echo "   App running — zero-downtime reload..."
  pm2 reload ecosystem.config.js
  echo "   ✔ Application reloaded"
else
  echo "   First launch — starting..."
  pm2 start ecosystem.config.js
  echo "   ✔ Application started"
fi
pm2 save
echo ""

# --------------------------------------------------------------------------
# 9. Health check
# --------------------------------------------------------------------------
echo "[ 9/9 ] Health check..."

APP_PORT=3000
if [ -f ".env" ]; then
  PARSED_PORT="$(grep -E '^PORT=' .env | head -1 | cut -d'=' -f2 | tr -d '[:space:]')"
  [ -n "${PARSED_PORT:-}" ] && APP_PORT="$PARSED_PORT"
fi

HEALTH_URL="http://localhost:${APP_PORT}/api/health"
ATTEMPTS=0
MAX_ATTEMPTS=15   # 15 × 2 s = 30 s max
until curl -sf "$HEALTH_URL" > /dev/null 2>&1; do
  ATTEMPTS=$((ATTEMPTS + 1))
  if [ "$ATTEMPTS" -ge "$MAX_ATTEMPTS" ]; then
    echo ""
    echo "   ❌ Health check failed after ${MAX_ATTEMPTS} attempts."
    echo "   Check logs: pm2 logs nevesty-models"
    exit 1
  fi
  echo -n "."
  sleep 2
done
echo ""
echo "   ✔ Health check passed (${HEALTH_URL})"
echo ""

# --------------------------------------------------------------------------
# Success banner
# --------------------------------------------------------------------------
SITE_URL_VAL="http://localhost:${APP_PORT}"
if [ -f ".env" ]; then
  PARSED_SITE="$(grep -E '^SITE_URL=' .env | head -1 | cut -d'=' -f2 | tr -d '[:space:]')"
  [ -n "$PARSED_SITE" ] && SITE_URL_VAL="$PARSED_SITE"
fi

echo "============================================="
echo "  ✅  Deploy complete!"
echo "============================================="
echo ""
echo "  Server URL   : ${SITE_URL_VAL}"
echo "  Admin panel  : ${SITE_URL_VAL}/admin/login.html"
echo "  Agent dash   : ${SITE_URL_VAL}/dashboard/"
echo ""
echo "  PM2 commands:"
echo "    pm2 status                      — process list"
echo "    pm2 logs nevesty-models         — live logs"
echo "    pm2 reload ecosystem.config.js  — zero-downtime reload"
echo "    pm2 startup && pm2 save         — auto-start on reboot"
echo ""
