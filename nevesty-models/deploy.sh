#!/bin/bash
# =============================================================================
# Nevesty Models — Production Setup & Deploy Script
# Usage: ./deploy.sh
# =============================================================================
set -euo pipefail

# --------------------------------------------------------------------------
# Trap for unexpected errors
# --------------------------------------------------------------------------
trap 'echo ""; echo "❌ Deploy failed at line $LINENO. Check the output above for details." >&2; exit 1' ERR

# Resolve the directory where this script lives
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "============================================="
echo "  Nevesty Models — Production Deploy"
echo "============================================="
echo ""

# --------------------------------------------------------------------------
# 0. Git — pull latest changes from the current branch
# --------------------------------------------------------------------------
CURRENT_BRANCH="$(git -C "$SCRIPT_DIR" rev-parse --abbrev-ref HEAD)"
echo "[ 0/9 ] Pulling latest changes (branch: ${CURRENT_BRANCH})..."
git -C "$SCRIPT_DIR" fetch origin
git -C "$SCRIPT_DIR" pull origin "$CURRENT_BRANCH"
echo "   ✔ Repository up to date ($(git -C "$SCRIPT_DIR" rev-parse --short HEAD))"
echo ""

# --------------------------------------------------------------------------
# 1. Check .env
# --------------------------------------------------------------------------
echo "[ 1/9 ] Checking .env file..."
if [ ! -f ".env" ]; then
  echo "⚠️  WARNING: .env file not found!"
  echo "   Copy .env.example and fill in your values:"
  echo "   cp .env.example .env && nano .env"
  echo ""
  echo "   Continuing anyway — app may fail to start without proper config."
  echo ""
else
  echo "   ✔ .env found"
fi

# --------------------------------------------------------------------------
# 2. npm install --production
# --------------------------------------------------------------------------
echo "[ 2/9 ] Installing production dependencies..."
npm ci --omit=dev
echo "   ✔ Dependencies installed"

# --------------------------------------------------------------------------
# 3. Create logs/ directory
# --------------------------------------------------------------------------
echo "[ 3/9 ] Ensuring logs/ directory exists..."
mkdir -p logs
echo "   ✔ logs/ ready"

# --------------------------------------------------------------------------
# 4. Initialize database
# --------------------------------------------------------------------------
echo "[ 4/9 ] Initializing database..."
node database.js
echo "   ✔ Database initialized"

# --------------------------------------------------------------------------
# 5. Run model seeder
# --------------------------------------------------------------------------
echo "[ 5/9 ] Seeding models..."
node tools/seed-models.js
echo "   ✔ Models seeded"

# --------------------------------------------------------------------------
# 6. Check / install PM2
# --------------------------------------------------------------------------
echo "[ 6/9 ] Checking PM2..."
if ! command -v pm2 &>/dev/null; then
  echo "   PM2 not found — installing globally..."
  npm install -g pm2
  echo "   ✔ PM2 installed"
else
  PM2_VERSION="$(pm2 --version)"
  echo "   ✔ PM2 already installed (v${PM2_VERSION})"
fi

# --------------------------------------------------------------------------
# 7. Start or reload via PM2
# --------------------------------------------------------------------------
echo "[ 7/9 ] Starting / reloading application..."
# Check if any process from ecosystem.config.js is already managed by PM2
if pm2 describe nevesty-models &>/dev/null 2>&1; then
  echo "   App already running in PM2 — reloading..."
  pm2 reload ecosystem.config.js
  echo "   ✔ Application reloaded"
else
  echo "   Starting application for the first time..."
  pm2 start ecosystem.config.js
  echo "   ✔ Application started"
fi

# --------------------------------------------------------------------------
# 8. Save PM2 process list
# --------------------------------------------------------------------------
echo "[ 8/9 ] Saving PM2 process list..."
pm2 save
echo "   ✔ Process list saved"

# --------------------------------------------------------------------------
# Health check — verify app is responding before declaring success
# --------------------------------------------------------------------------
echo ""
echo "   Waiting for server to be ready..."

# Detect PORT from .env (fallback to 3000) early so health check can use it
APP_PORT=3000
if [ -f ".env" ]; then
  PARSED_PORT="$(grep -E '^PORT=' .env | head -1 | cut -d'=' -f2 | tr -d '[:space:]')"
  if [ -n "${PARSED_PORT:-}" ]; then
    APP_PORT="$PARSED_PORT"
  fi
fi

HEALTH_URL="http://localhost:${APP_PORT}/api/health"
ATTEMPTS=0
MAX_ATTEMPTS=15    # 15 × 2s = 30s max wait
until curl -sf "$HEALTH_URL" > /dev/null 2>&1; do
  ATTEMPTS=$((ATTEMPTS + 1))
  if [ "$ATTEMPTS" -ge "$MAX_ATTEMPTS" ]; then
    echo ""
    echo "   ❌ Health check failed after ${MAX_ATTEMPTS} attempts: ${HEALTH_URL}"
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
# 9. PM2 startup hint
# --------------------------------------------------------------------------
echo "[ 9/9 ] PM2 startup configuration..."
echo ""
echo "   To enable PM2 to auto-start on system reboot, run the command"
echo "   printed by:"
echo ""
echo "     pm2 startup"
echo ""
echo "   Copy-paste the generated sudo command and execute it."
echo "   Then run: pm2 save"
echo ""

SITE_URL_VAL="http://localhost:${APP_PORT}"
if [ -f ".env" ]; then
  PARSED_SITE="$(grep -E '^SITE_URL=' .env | head -1 | cut -d'=' -f2 | tr -d '[:space:]')"
  if [ -n "$PARSED_SITE" ] && [ "$PARSED_SITE" != "http://localhost:3000" ]; then
    SITE_URL_VAL="$PARSED_SITE"
  fi
fi

# --------------------------------------------------------------------------
# Success banner
# --------------------------------------------------------------------------
echo "============================================="
echo "  ✅  Deploy complete!"
echo "============================================="
echo ""
echo "  Server URL   : ${SITE_URL_VAL}"
echo "  Admin panel  : ${SITE_URL_VAL}/admin/login.html"
echo "  Agent dash   : ${SITE_URL_VAL}/dashboard/"
echo ""
echo "  Useful PM2 commands:"
echo "    pm2 status                  — process list"
echo "    pm2 logs nevesty-models     — live app logs"
echo "    pm2 logs nevesty-scheduler  — live scheduler logs"
echo "    pm2 reload ecosystem.config.js  — zero-downtime reload"
echo "    pm2 restart ecosystem.config.js — hard restart"
echo "    pm2 stop ecosystem.config.js    — stop all"
echo ""
