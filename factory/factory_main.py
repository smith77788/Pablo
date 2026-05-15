"""Factory entry point — runs cycle continuously or once with --once flag."""
from __future__ import annotations
import argparse
import logging
import os
import sys
import time

from factory.logging_config import configure_logging
from factory.cycle import run_cycle

configure_logging()
logger = logging.getLogger(__name__)

CYCLE_INTERVAL_SECONDS = int(os.getenv("FACTORY_CYCLE_INTERVAL", "3600"))


def main() -> None:
    parser = argparse.ArgumentParser(description="AI Factory cycle runner")
    parser.add_argument("--once", action="store_true", help="Run one cycle and exit")
    parser.add_argument("--interval", type=int, default=CYCLE_INTERVAL_SECONDS, help="Cycle interval in seconds")
    args = parser.parse_args()

    if args.once:
        logger.info("[factory_main] Running single cycle")
        result = run_cycle()
        logger.info("[factory_main] Cycle complete: health_score=%s elapsed=%.1fs",
                    result.get("health_score", "?"), result.get("elapsed_s", 0))
        sys.exit(0)

    logger.info("[factory_main] Starting continuous mode (interval=%ds)", args.interval)
    while True:
        try:
            result = run_cycle()
            logger.info("[factory_main] Cycle complete: health_score=%s", result.get("health_score", "?"))
        except Exception as e:
            logger.error("[factory_main] Cycle error: %s", e)
        logger.info("[factory_main] Sleeping %ds until next cycle", args.interval)
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
