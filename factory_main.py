"""🚀 AI Startup Factory — точка входа."""
from __future__ import annotations
import argparse
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
    parser = argparse.ArgumentParser(description='Nevesty Models AI Factory')
    parser.add_argument('--once', action='store_true', help='Run one cycle and exit')
    parser.add_argument('--status', action='store_true', help='Show last cycle status and exit')
    parser.add_argument('--cycle-hours', type=float, default=None, help='Override cycle interval in hours')
    args = parser.parse_args()

    if args.status:
        _show_status()
        return

    interval_seconds = CYCLE_INTERVAL_SECONDS
    if args.cycle_hours is not None:
        interval_seconds = args.cycle_hours * 3600
        logger.info("Cycle interval overridden to %.1f hours", args.cycle_hours)

    if args.once:
        logger.info("=" * 60)
        logger.info("🏭 AI STARTUP FACTORY — ONE SHOT")
        logger.info("=" * 60)
        _run_one_cycle()
        return

    logger.info("=" * 60)
    logger.info("🏭 AI STARTUP FACTORY STARTING")
    logger.info("Cycle interval: %.1f hours", interval_seconds / 3600)
    logger.info("=" * 60)

    # Run first cycle immediately
    _run_one_cycle()

    # Then loop
    while True:
        logger.info("💤 Sleeping %.0f seconds until next cycle...", interval_seconds)
        time.sleep(interval_seconds)
        _run_one_cycle()


def _show_status() -> None:
    """Show status of last completed cycle."""
    from pathlib import Path
    import json

    history_dir = Path(__file__).parent / 'factory' / 'history'
    if not history_dir.exists():
        print("No cycle history found")
        return

    cycles = sorted(history_dir.glob("cycle_*.json"))
    if not cycles:
        print("No cycles completed yet")
        return

    with open(cycles[-1]) as f:
        last = json.load(f)

    print(f"Last cycle: {last.get('timestamp') or last.get('cycle_id', 'unknown')}")
    print(f"Duration: {last.get('duration_seconds', 0):.0f}s")
    ceo = last.get('phases', {}).get('ceo_synthesis', {})
    print(f"Health score: {ceo.get('health_score', 'N/A')}")
    print(f"Weekly focus: {ceo.get('weekly_focus', 'N/A')}")
    actions = ceo.get('growth_actions', [])
    print(f"Growth actions: {len(actions)}")


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
