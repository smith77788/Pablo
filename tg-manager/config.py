"""
Infragram Configuration — environment variables for the Telegram management platform.

Required variables must be set in .env or Railway environment.
Optional variables have sensible defaults or are empty strings.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ── Core ───────────────────────────────────────────────────────────────────────
def _require(name: str) -> str:
    """Обязательная переменная окружения с ВНЯТНОЙ ошибкой вместо KeyError.

    Без этого отсутствие MANAGER_BOT_TOKEN/DATABASE_URL роняло импорт config.py
    (и каскадом всё приложение) с непонятным `KeyError('...')` — оператор не
    понимал, что именно не настроено. Теперь — явное сообщение с действием."""
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(
            f"Не задана обязательная переменная окружения {name}. "
            "Укажите её в .env или в переменных окружения деплоя (Railway/хост)."
        )
    return val


BOT_TOKEN: str = _require("MANAGER_BOT_TOKEN")  # Telegram bot token from @BotFather
DATABASE_URL: str = _require("DATABASE_URL")  # PostgreSQL connection string
ADMIN_IDS: list[int] = [  # Comma-separated list of admin Telegram user IDs
    int(x.strip()) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()
]
BROADCAST_DELAY: float = float(os.environ.get("BROADCAST_DELAY", "0.05"))  # Seconds between broadcast messages
MAX_CONCURRENT: int = int(os.environ.get("MAX_CONCURRENT", "20"))  # Max concurrent operations

# ── Payments (TON/TRON) ────────────────────────────────────────────────────────
TON_WALLET: str = os.getenv("TON_WALLET", "")  # TON wallet address for receiving payments
TON_API_KEY: str = os.getenv("TON_API_KEY", "")  # TON API key for payment verification
TRON_WALLET: str = os.getenv("TRON_WALLET", "")  # TRON wallet address for USDT payments
TRON_API_KEY: str = os.getenv("TRON_API_KEY", "")  # TRON API key for payment verification

# ── AI Providers ───────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY: str = os.getenv(
    "ANTHROPIC_API_KEY", ""
)  # Kept for backward compatibility
OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")  # OpenAI API key
OPENROUTER_API_KEY: str = os.getenv("OPENROUTER_API_KEY", "")  # OpenRouter API key for AI assistant
OPENROUTER_MODEL: str = os.getenv("OPENROUTER_MODEL", "anthropic/claude-sonnet-4-6")  # Default AI model

# ── Security ───────────────────────────────────────────────────────────────────
ADMIN_SECRET: str = os.getenv("ADMIN_SECRET", "")  # Secret token for admin API access
# AES-256-GCM key for encrypting bot tokens and credentials at rest.
# Must be set in env. If absent, falls back to MANAGER_BOT_TOKEN (deterministic).
TOKEN_ENCRYPTION_KEY: str = os.getenv("TOKEN_ENCRYPTION_KEY", "")

# ── Telegram Client (Telethon) ────────────────────────────────────────────────
TG_API_ID: int = int(os.getenv("TG_API_ID", "0") or "0")  # Telegram API ID from my.telegram.org
TG_API_HASH: str = os.getenv("TG_API_HASH", "")  # Telegram API hash from my.telegram.org
# Optional SOCKS5 proxy for Telethon (needed on datacenter IPs like Railway)
# Format: socks5://user:pass@host:port  or  socks5://host:port
TG_PROXY: str = os.getenv("TG_PROXY", "")

# Optional Cloudflare Worker WebSocket relay for IP masking (free alternative to proxy).
# Deploy infra/cf_relay_worker.js to Cloudflare Workers, set this to the worker URL.
# Format: https://my-relay.my-name.workers.dev
# When set, accounts without individual proxies route through the CF Worker instead of
# connecting to Telegram DCs directly from the Railway datacenter IP.
CF_RELAY_URL: str = os.getenv("CF_RELAY_URL", "").rstrip("/")

# ── Email (SMTP) ───────────────────────────────────────────────────────────────
SMTP_HOST: str = os.getenv("SMTP_HOST", "")  # SMTP server hostname
SMTP_PORT: int = int(os.getenv("SMTP_PORT", "587"))  # SMTP server port (default: 587)
SMTP_USER: str = os.getenv("SMTP_USER", "")  # SMTP username
SMTP_PASS: str = os.getenv("SMTP_PASS", "")  # SMTP password
REPORT_FROM_EMAIL: str = os.getenv("REPORT_FROM_EMAIL", "")  # Sender email for reports
NCMEC_EMAIL: str = os.getenv("NCMEC_EMAIL", "cybertipline@ncmec.org")  # NCMEC reporting email

# ── Public URLs ────────────────────────────────────────────────────────────────
PUBLIC_BASE_URL: str = os.getenv("PUBLIC_BASE_URL", "")  # Public URL for webhooks/callbacks
# URL of the deployed Mini App (e.g. https://app.railway.app/miniapp/)
# Set this in Railway env vars after first deploy to enable the Mini App button in Telegram.
MINI_APP_URL: str = os.getenv("MINI_APP_URL", "")

# ── OAuth (Email Integration) ─────────────────────────────────────────────────
EMAIL_OAUTH_REDIRECT_URI: str = os.getenv("EMAIL_OAUTH_REDIRECT_URI", "")  # OAuth callback URL
EMAIL_OAUTH_STATE_SECRET: str = os.getenv("EMAIL_OAUTH_STATE_SECRET", "")  # State encryption key
GOOGLE_OAUTH_CLIENT_ID: str = os.getenv("GOOGLE_OAUTH_CLIENT_ID", "")  # Google OAuth client ID
GOOGLE_OAUTH_CLIENT_SECRET: str = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET", "")  # Google OAuth secret
MICROSOFT_OAUTH_CLIENT_ID: str = os.getenv("MICROSOFT_OAUTH_CLIENT_ID", "")  # Microsoft OAuth client ID
MICROSOFT_OAUTH_CLIENT_SECRET: str = os.getenv("MICROSOFT_OAUTH_CLIENT_SECRET", "")  # Microsoft OAuth secret


# ── Pricing ────────────────────────────────────────────────────────────────────

def _price(plan: str, default: int) -> int:
    """Get plan price from environment or use default."""
    try:
        return int(os.getenv(f"PRICE_{plan.upper()}", str(default)))
    except (ValueError, TypeError):
        return default


# Plan prices in USD (override via PRICE_PAID env var)
PLAN_PRICES_USD: dict[str, int] = {
    "paid": _price("paid", 29),
    # backward compat: existing subscriptions may store old plan names
    "starter": _price("paid", 29),
    "pro": _price("paid", 29),
    "enterprise": _price("paid", 29),
}

# Discount percentages by subscription period (months: discount%)
PERIOD_DISCOUNTS: dict[int, int] = {1: 0, 3: 10, 6: 15, 12: 20}


# ── WB Chat (аккаунтная автоматизация мессенджера Wildberries) ──────────────────
# Второй канал — по образцу Telegram-стека: пользовательские аккаунты, сессии,
# прокси, вход по номеру, очередь операций, массовые действия. У мессенджера WB
# Chat пока нет публичного API/клиента протокола, поэтому транспорт абстрактный
# (services/wb_chat/transport.py), а драйвер выбирается этой переменной.
#
# WB_CHAT_DRIVER:
#   'mock' — встроенный драйвер в памяти (безопасен, для dev/тестов) — ПО УМОЛЧАНИЮ;
#   'real' — боевой драйвер; до поставки протокола поднимает WBProtocolUnavailable.
WB_CHAT_DRIVER: str = os.getenv("WB_CHAT_DRIVER", "mock").strip().lower()

# Фоновый воркер операций WB. Включать только когда транспорт реально готов
# ('real' с реализованным протоколом) ИЛИ для отладки на моке. По умолчанию
# выключен, чтобы не крутить пустой цикл в проде до появления протокола.
WB_CHAT_WORKER_ENABLED: bool = os.getenv(
    "WB_CHAT_WORKER_ENABLED", "false"
).strip().lower() in ("1", "true", "yes", "on")

# Дневной лимит действий на аккаунт (защита от банов за перебор).
WB_CHAT_DAILY_BUDGET: int = int(os.getenv("WB_CHAT_DAILY_BUDGET", "50"))
