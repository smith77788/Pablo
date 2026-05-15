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
    parser.add_argument('--report', action='store_true', help='Print last full cycle report in human-readable format and exit')
    parser.add_argument('--cycle-hours', type=float, default=None, help='Override cycle interval in hours')
    args = parser.parse_args()

    if args.status:
        _show_status()
        return

    if args.report:
        _show_report()
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


def _show_report() -> None:
    """Print last full cycle report in human-readable format."""
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

    ceo = last.get('phases', {}).get('ceo_synthesis', {})
    health = ceo.get('health_score', 'N/A')
    ts = last.get('timestamp') or last.get('cycle_id', 'unknown')
    duration = last.get('duration_seconds') or last.get('elapsed_s', 0)
    weekly_focus = ceo.get('weekly_focus', '—')
    growth_actions = ceo.get('growth_actions', [])
    risks = ceo.get('risks', [])
    opportunities = ceo.get('opportunities', [])
    next_exp = ceo.get('next_cycle_experiment', {})
    priority_kpi = ceo.get('priority_kpi', {})
    prev_lesson = ceo.get('prev_cycle_lesson', '')
    memo = ceo.get('ceo_memo', '')

    score_icon = "GREEN" if isinstance(health, (int, float)) and health >= 70 else "YELLOW" if isinstance(health, (int, float)) and health >= 40 else "RED"

    sep = "=" * 60
    print(sep)
    print("  AI FACTORY — ПОСЛЕДНИЙ ОТЧЁТ")
    print(sep)
    print(f"  Дата цикла : {str(ts)[:19]}")
    print(f"  Длительность: {float(duration):.0f} с")
    print(f"  Health Score: {health}/100  [{score_icon}]")
    print()

    print("--- CEO ФОКУС НЕДЕЛИ ---")
    print(f"  {weekly_focus}")
    print()

    if next_exp:
        print("--- ЭКСПЕРИМЕНТ СЛЕДУЮЩЕГО ЦИКЛА ---")
        print(f"  Гипотеза : {next_exp.get('hypothesis', '—')}")
        print(f"  Метрика  : {next_exp.get('metric', '—')}")
        print(f"  Департ.  : {next_exp.get('department', '—')}")
        print()

    if priority_kpi:
        print("--- ПРИОРИТЕТНЫЙ KPI ---")
        print(f"  {priority_kpi.get('name', '—')}: {priority_kpi.get('current', '?')} → цель {priority_kpi.get('target', '?')}")
        print()

    if prev_lesson:
        print("--- УРОК ПРОШЛОГО ЦИКЛА ---")
        print(f"  {prev_lesson}")
        print()

    if growth_actions:
        print("--- ТОП-3 GROWTH ACTIONS ---")
        for i, action in enumerate(growth_actions[:3], start=1):
            dept = action.get('department', '?')
            act = action.get('action', '—')
            impact = action.get('expected_impact', '?')
            print(f"  {i}. [{dept}] {act}")
            print(f"     Ожидаемый эффект: {impact}")
        print()

    if risks:
        print("--- РИСКИ ---")
        for r in risks[:2]:
            print(f"  - {r}")
        print()

    if opportunities:
        print("--- ВОЗМОЖНОСТИ ---")
        for o in opportunities[:2]:
            print(f"  + {o}")
        print()

    if memo:
        print("--- CEO МЕМОРАНДУМ ---")
        # Print memo with line wrapping at 70 chars
        for line in memo.split('\n'):
            if len(line) <= 70:
                print(f"  {line}")
            else:
                words = line.split()
                current = "  "
                for word in words:
                    if len(current) + len(word) + 1 > 72:
                        print(current)
                        current = f"  {word}"
                    else:
                        current = f"{current} {word}" if current.strip() else f"  {word}"
                if current.strip():
                    print(current)
        print()

    # Department summary
    phases = last.get('phases', {})
    dept_names = [k for k in phases if k not in ('ceo', 'ceo_synthesis', 'departments', 'ab_experiments')]
    if dept_names:
        print("--- ДЕПАРТАМЕНТЫ В ЦИКЛЕ ---")
        for dept in dept_names:
            dept_data = phases[dept]
            roles = dept_data.get('roles_used', [])
            roles_str = ', '.join(roles) if roles else 'нет данных'
            print(f"  {dept:20s}: {roles_str}")
        print()

    print(sep)


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
