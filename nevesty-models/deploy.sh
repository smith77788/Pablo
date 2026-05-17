#!/bin/bash
# =============================================================================
# Nevesty Models — Production Setup & Deploy Script
# Usage:
#   ./deploy.sh            — bare-metal deploy via PM2 (default)
#   ./deploy.sh --docker   — Docker Compose deploy
# =============================================================================
set -euo pipefail

# --------------------------------------------------------------------------
# Trap for unexpected errors
# --------------------------------------------------------------------------
trap 'echo ""; echo "❌ Deploy failed at line $LINENO. Check the output above for details." >&2; exit 1' ERR

# --------------------------------------------------------------------------
# Parse flags
# --------------------------------------------------------------------------
DEPLOY_MODE="pm2"
for arg in "$@"; do
  case "$arg" in
    --docker) DEPLOY_MODE="docker" ;;
    --help|-h)
      echo "Usage: ./deploy.sh [--docker]"
      echo "  (no flag)  — bare-metal PM2 deploy"
      echo "  --docker   — Docker Compose deploy"
      exit 0
      ;;
  esac
done

# Resolve the directory where this script lives
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# --------------------------------------------------------------------------
# PRE-DEPLOY CHECKS
# --------------------------------------------------------------------------
echo "── Pre-deploy checks ──────────────────────────────────────────────────"

# 1. Node.js version >= 18
if ! command -v node &>/dev/null; then
  echo "❌  node is not installed. Install Node.js >= 18." >&2
  exit 1
fi
NODE_MAJOR="$(node -e 'process.stdout.write(String(process.versions.node.split(".")[0]))')"
if [ "$NODE_MAJOR" -lt 18 ]; then
  echo "❌  Node.js version must be >= 18 (found: $(node --version))." >&2
  exit 1
fi
echo "   ✔ Node.js $(node --version) (>= 18 required)"

# 2. npm ci dry-run (only for bare-metal mode; Docker builds inside container)
if [ "${DEPLOY_MODE:-pm2}" != "docker" ]; then
  echo "   Verifying npm dependencies..."
  if ! npm ci --dry-run --omit=dev > /dev/null 2>&1; then
    echo "❌  'npm ci' pre-check failed. Verify package-lock.json is up to date." >&2
    exit 1
  fi
  echo "   ✔ npm ci dry-run passed"
fi

# 3. Required environment variables
if [ -f ".env" ]; then
  ENV_FILE=".env"
elif [ -f ".env.example" ]; then
  # Allow check even without .env (will warn, not abort)
  ENV_FILE=""
else
  ENV_FILE=""
fi

MISSING_VARS=()
for VAR in BOT_TOKEN JWT_SECRET ANTHROPIC_API_KEY; do
  # Check shell env first, then .env file
  VAL="${!VAR:-}"
  if [ -z "$VAL" ] && [ -n "$ENV_FILE" ]; then
    VAL="$(grep -E "^${VAR}=" "$ENV_FILE" | head -1 | cut -d'=' -f2- | tr -d '[:space:]')" || true
  fi
  if [ -z "$VAL" ] || [[ "$VAL" == *"your_"* ]] || [[ "$VAL" == *"your-"* ]]; then
    MISSING_VARS+=("$VAR")
  fi
done

if [ "${#MISSING_VARS[@]}" -gt 0 ]; then
  echo "❌  Required environment variable(s) not set or still placeholder:" >&2
  for V in "${MISSING_VARS[@]}"; do echo "      - $V" >&2; done
  echo "   Edit .env and fill in real values, then re-run deploy." >&2
  exit 1
fi
echo "   ✔ Required env vars present (BOT_TOKEN, JWT_SECRET, ANTHROPIC_API_KEY)"

echo "── Pre-deploy checks passed ────────────────────────────────────────────"
echo ""

echo "============================================="
echo "  Nevesty Models — Production Deploy"
echo "  Mode: ${DEPLOY_MODE}"
echo "============================================="
echo ""

# --------------------------------------------------------------------------
# DOCKER MODE
# --------------------------------------------------------------------------
if [ "$DEPLOY_MODE" = "docker" ]; then
  echo "[ 1/5 ] Pulling latest changes..."
  CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
  git fetch origin
  git pull origin "$CURRENT_BRANCH"
  echo "   ✔ Repository up to date ($(git rev-parse --short HEAD))"
  echo ""

  echo "[ 2/5 ] Checking .env file..."
  if [ ! -f ".env" ]; then
    echo "⚠️  WARNING: .env file not found!"
    echo "   cp .env.example .env && nano .env"
    echo ""
  else
    echo "   ✔ .env found"
  fi

  echo "[ 3/5 ] Building Docker images (no cache)..."
  docker-compose build --no-cache app
  echo "   ✔ Build complete"
  echo ""

  echo "[ 4/5 ] Restarting containers..."
  docker-compose down --remove-orphans
  docker-compose up -d
  echo "   ✔ Containers started"
  echo ""

  echo "[ 5/5 ] Verifying health..."
  APP_PORT=3000
  if [ -f ".env" ]; then
    PARSED_PORT="$(grep -E '^PORT=' .env | head -1 | cut -d'=' -f2 | tr -d '[:space:]')" || true
    if [ -n "${PARSED_PORT:-}" ]; then APP_PORT="$PARSED_PORT"; fi
  fi
  HEALTH_URL="http://localhost:${APP_PORT}/api/health"
  ATTEMPTS=0; MAX_ATTEMPTS=20
  until curl -sf "$HEALTH_URL" > /dev/null 2>&1; do
    ATTEMPTS=$((ATTEMPTS + 1))
    if [ "$ATTEMPTS" -ge "$MAX_ATTEMPTS" ]; then
      echo "   ❌ Health check failed after ${MAX_ATTEMPTS} attempts: ${HEALTH_URL}"
      echo "   Check logs: docker-compose logs app"
      exit 1
    fi
    echo -n "."; sleep 3
  done
  echo ""
  echo "   ✔ Health check passed"
  echo ""

  echo "============================================="
  echo "  ✅  Docker deploy complete!"
  echo "============================================="
  echo ""
  docker-compose ps
  echo ""
  echo "  Useful Docker commands:"
  echo "    docker-compose ps                     — container status"
  echo "    docker-compose logs -f app            — live app logs"
  echo "    docker-compose logs -f redis          — live redis logs"
  echo "    docker-compose exec app sh            — shell in app container"
  echo "    docker-compose down                   — stop all"
  echo "    docker-compose up -d                  — start all (detached)"
  echo ""
  exit 0
fi

# --------------------------------------------------------------------------
# PM2 MODE (original deploy logic follows below)
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# 0. Git — pull latest changes from the current branch
# --------------------------------------------------------------------------
CURRENT_BRANCH="$(git -C "$SCRIPT_DIR" rev-parse --abbrev-ref HEAD)"
echo "[ 1/10 ] Pulling latest changes (branch: ${CURRENT_BRANCH})..."
git -C "$SCRIPT_DIR" fetch origin
git -C "$SCRIPT_DIR" pull origin "$CURRENT_BRANCH"
echo "   ✔ Repository up to date ($(git -C "$SCRIPT_DIR" rev-parse --short HEAD))"
echo ""

# --------------------------------------------------------------------------
# 1. Check .env
# --------------------------------------------------------------------------
echo "[ 2/10 ] Checking .env file..."
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
echo "[ 3/10 ] Installing production dependencies..."
npm ci --omit=dev
echo "   ✔ Dependencies installed"

# --------------------------------------------------------------------------
# 3. Create logs/ directory
# --------------------------------------------------------------------------
echo "[ 4/10 ] Ensuring runtime directories exist..."
mkdir -p logs data backups uploads/thumbs uploads/models
echo "   ✔ logs/ data/ backups/ uploads/ ready"

# --------------------------------------------------------------------------
# 4. Initialize database
# --------------------------------------------------------------------------
echo "[ 5/10 ] Initializing database..."
node database.js
echo "   ✔ Database initialized"

# --------------------------------------------------------------------------
# 5. Run model seeder
# --------------------------------------------------------------------------
echo "[ 6/10 ] Seeding models..."
node tools/seed-models.js
echo "   ✔ Models seeded"

# --------------------------------------------------------------------------
# 6. Check / install PM2
# --------------------------------------------------------------------------
echo "[ 7/10 ] Checking PM2..."
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
echo "[ 8/10 ] Starting / reloading application..."
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
echo "[ 9/10 ] Saving PM2 process list..."
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
echo "[ 10/10 ] PM2 startup configuration..."
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
