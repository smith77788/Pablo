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
ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")  # оставлен для совместимости
OPENROUTER_API_KEY: str = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL: str = os.getenv("OPENROUTER_MODEL", "anthropic/claude-sonnet-4-5")
ADMIN_SECRET: str = os.getenv("ADMIN_SECRET", "")
TG_API_ID: int = int(os.getenv("TG_API_ID", "0") or "0")
TG_API_HASH: str = os.getenv("TG_API_HASH", "")
# Optional SOCKS5 proxy for Telethon (needed on datacenter IPs like Railway)
# Format: socks5://user:pass@host:port  or  socks5://host:port
TG_PROXY: str = os.getenv("TG_PROXY", "")

PLAN_PRICES_USD: dict[str, int] = {"starter": 9, "pro": 25, "enterprise": 69}
PERIOD_DISCOUNTS: dict[int, int] = {1: 0, 3: 10, 6: 15, 12: 20}
