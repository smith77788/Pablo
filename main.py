#!/usr/bin/env python3
"""Entry point for Pablo — BASIC.FOOD AI agent system."""
import argparse
import logging
import os
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Pablo — BASIC.FOOD AI Agents")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("telegram", help="Start Telegram polling loop")
    sub.add_parser("briefing", help="Print morning briefing")
    sub.add_parser("orders", help="Process new orders")
    sub.add_parser("stock", help="Check stock levels")
    sub.add_parser("weekly", help="Weekly analytics report")

    ask_p = sub.add_parser("ask", help="Ask analytics a question")
    ask_p.add_argument("question", nargs="+")

    tracking_p = sub.add_parser("tracking", help="Add Nova Poshta tracking to order")
    tracking_p.add_argument("order_number")
    tracking_p.add_argument("tracking")

    receive_p = sub.add_parser("receive", help="Register incoming stock")
    receive_p.add_argument("product_id")
    receive_p.add_argument("quantity", type=int)
    receive_p.add_argument("--reason", default="Надходження товару")

    args = parser.parse_args()

    from orchestrator import Pablo
    pablo = Pablo()

    if args.command == "telegram":
        pablo.run_telegram_loop()
    elif args.command == "briefing":
        print(pablo.morning_briefing())
    elif args.command == "orders":
        print(pablo.process_new_orders())
    elif args.command == "stock":
        print(pablo.inventory.check_stock_levels())
    elif args.command == "weekly":
        print(pablo.weekly_report())
    elif args.command == "ask":
        print(pablo.ask_analytics(" ".join(args.question)))
    elif args.command == "tracking":
        print(pablo.add_tracking(args.order_number, args.tracking))
    elif args.command == "receive":
        print(pablo.receive_stock(args.product_id, args.quantity, args.reason))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
