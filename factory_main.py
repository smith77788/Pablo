"""🚀 AI Startup Factory — точка входа."""
from __future__ import annotations
import logging
import os
import sys
import time

from dotenv import load_dotenv

# Load .env files: root first (ANTHROPIC_API_KEY etc), then nevesty-models (Telegram token)
_base = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_base, ".env"))
load_dotenv(os.path.join(_base, "nevesty-models", ".env"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(os.path.join(os.path.dirname(__file__), "factory.log"), encoding="utf-8"),
    ],
)

logger = logging.getLogger(__name__)

CYCLE_INTERVAL_HOURS = float(os.getenv("FACTORY_CYCLE_HOURS", "6"))
CYCLE_INTERVAL_SECONDS = CYCLE_INTERVAL_HOURS * 3600


def main() -> None:
    logger.info("=" * 60)
    logger.info("🏭 AI STARTUP FACTORY STARTING")
    logger.info("Cycle interval: %.1f hours", CYCLE_INTERVAL_HOURS)
    logger.info("=" * 60)

    # Run first cycle immediately
    _run_one_cycle()

    # Then loop
    while True:
        logger.info("💤 Sleeping %.0f seconds until next cycle...", CYCLE_INTERVAL_SECONDS)
        time.sleep(CYCLE_INTERVAL_SECONDS)
        _run_one_cycle()


def _run_one_cycle() -> None:
    try:
        from factory.cycle import run_cycle
        result = run_cycle()
        logger.info(
            "✅ Cycle done: score=%s%%, actions=%d, experiments_concluded=%d, elapsed=%.1fs",
            result.get("health_score"),
            result.get("new_actions", 0),
            result.get("experiments_concluded", 0),
            result.get("elapsed_s", 0),
        )
    except Exception as e:
        logger.exception("❌ Cycle failed: %s", e)


if __name__ == "__main__":
    main()
