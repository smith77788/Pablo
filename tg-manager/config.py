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
