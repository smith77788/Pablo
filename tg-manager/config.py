import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN: str = os.environ["MANAGER_BOT_TOKEN"]
DATABASE_URL: str = os.environ["DATABASE_URL"]
ADMIN_IDS: list[int] = [
    int(x.strip()) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()
]
BROADCAST_DELAY: float = float(os.environ.get("BROADCAST_DELAY", "0.05"))
MAX_CONCURRENT: int = int(os.environ.get("MAX_CONCURRENT", "20"))

TON_WALLET: str = os.getenv("TON_WALLET", "")
TON_API_KEY: str = os.getenv("TON_API_KEY", "")
TRON_WALLET: str = os.getenv("TRON_WALLET", "")
TRON_API_KEY: str = os.getenv("TRON_API_KEY", "")
ANTHROPIC_API_KEY: str = os.getenv(
    "ANTHROPIC_API_KEY", ""
)  # оставлен для совместимости
OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
OPENROUTER_API_KEY: str = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL: str = os.getenv("OPENROUTER_MODEL", "anthropic/claude-sonnet-4-6")
ADMIN_SECRET: str = os.getenv("ADMIN_SECRET", "")
TG_API_ID: int = int(os.getenv("TG_API_ID", "0") or "0")
TG_API_HASH: str = os.getenv("TG_API_HASH", "")
# Optional SOCKS5 proxy for Telethon (needed on datacenter IPs like Railway)
# Format: socks5://user:pass@host:port  or  socks5://host:port
TG_PROXY: str = os.getenv("TG_PROXY", "")

# SMTP for email reporting (abuse@telegram.org, NCMEC, etc.)
SMTP_HOST: str = os.getenv("SMTP_HOST", "")
SMTP_PORT: int = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER: str = os.getenv("SMTP_USER", "")
SMTP_PASS: str = os.getenv("SMTP_PASS", "")
REPORT_FROM_EMAIL: str = os.getenv("REPORT_FROM_EMAIL", "")
NCMEC_EMAIL: str = os.getenv("NCMEC_EMAIL", "cybertipline@ncmec.org")
PUBLIC_BASE_URL: str = os.getenv("PUBLIC_BASE_URL", "")
EMAIL_OAUTH_REDIRECT_URI: str = os.getenv("EMAIL_OAUTH_REDIRECT_URI", "")
EMAIL_OAUTH_STATE_SECRET: str = os.getenv("EMAIL_OAUTH_STATE_SECRET", "")
GOOGLE_OAUTH_CLIENT_ID: str = os.getenv("GOOGLE_OAUTH_CLIENT_ID", "")
GOOGLE_OAUTH_CLIENT_SECRET: str = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET", "")
MICROSOFT_OAUTH_CLIENT_ID: str = os.getenv("MICROSOFT_OAUTH_CLIENT_ID", "")
MICROSOFT_OAUTH_CLIENT_SECRET: str = os.getenv("MICROSOFT_OAUTH_CLIENT_SECRET", "")


def _price(plan: str, default: int) -> int:
    try:
        return int(os.getenv(f"PRICE_{plan.upper()}", str(default)))
    except (ValueError, TypeError):
        return default


PLAN_PRICES_USD: dict[str, int] = {
    "paid": _price("paid", 29),
    # backward compat: existing subscriptions may store old plan names
    "starter": _price("paid", 29),
    "pro": _price("paid", 29),
    "enterprise": _price("paid", 29),
}
PERIOD_DISCOUNTS: dict[int, int] = {1: 0, 3: 10, 6: 15, 12: 20}
