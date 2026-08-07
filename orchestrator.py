"""Main orchestrator — coordinates all BASIC.FOOD AI agents."""
from __future__ import annotations
import logging
import time
from typing import Any

from agents.customer_support import CustomerSupportAgent
from agents.order_manager import OrderManagerAgent
from agents.analytics import AnalyticsAgent
from agents.inventory import InventoryAgent
from agents.child_safety import ChildSafetyAgent
from tools.telegram_tools import get_updates, process_update

logger = logging.getLogger(__name__)


class Pablo:
    """Top-level coordinator for all BASIC.FOOD AI agents."""

    def __init__(self) -> None:
        self.support = CustomerSupportAgent()
        self.orders = OrderManagerAgent()
        self.analytics = AnalyticsAgent()
        self.inventory = InventoryAgent()
        self.guardian = ChildSafetyAgent()
        self._telegram_offset = 0

    # ------------------------------------------------------------------
    # Telegram polling loop
    # ------------------------------------------------------------------

    def run_telegram_loop(self, poll_interval: int = 2) -> None:
        """Poll Telegram for new messages and route them to the support agent."""
        logger.info("Pablo Telegram loop started")
        while True:
            try:
                updates = get_updates(offset=self._telegram_offset)
                for update in updates:
                    self._telegram_offset = update["update_id"] + 1
                    ctx = process_update(update)
                    if ctx and ctx.get("text"):
                        # Guardian проверяет ПЕРВЫМ. Если сообщение заблокировано
                        # как высокорисковое — в поддержку оно не уходит.
                        if self._guardian_screen(update, ctx):
                            continue
                        self._handle_telegram_message(ctx)
            except Exception as e:
                logger.error("Telegram poll error: %s", e)
            time.sleep(poll_interval)

    def _guardian_screen(self, update: dict, ctx: dict) -> bool:
        """
        Прогнать сообщение через Guardian. Возвращает True, если сообщение
        было заблокировано (дальше по конвейеру его не пускаем).
        """
        msg = update.get("message") or update.get("edited_message") or {}
        from_user = msg.get("from", {})
        screen_ctx = {
            "chat_id": ctx["chat_id"],
            "message_id": msg.get("message_id"),
            "user_id": from_user.get("id"),
            "username": ctx.get("username", ""),
            "text": ctx.get("text", ""),
        }
        try:
            outcome = self.guardian.screen_message(screen_ctx)
        except Exception as e:
            logger.error("Guardian screen error for chat_id=%s: %s", ctx["chat_id"], e)
            return False

        action = outcome.get("action")
        if action in ("blocked", "recorded"):
            logger.warning(
                "Guardian %s chat_id=%s risk=%s score=%s",
                action, ctx["chat_id"], outcome["scan"]["risk"], outcome["scan"]["score"],
            )
        return action == "blocked"

    def _handle_telegram_message(self, ctx: dict) -> None:
        chat_id = ctx["chat_id"]
        text = ctx["text"]
        customer = ctx.get("customer")
        logger.info("Telegram message from chat_id=%s: %s", chat_id, text[:80])
        try:
            self.support.handle_telegram(chat_id, text, customer=customer)
        except Exception as e:
            logger.error("Support agent error for chat_id=%s: %s", chat_id, e)

    # ------------------------------------------------------------------
    # Scheduled tasks (call from cron / scheduler)
    # ------------------------------------------------------------------

    def morning_briefing(self) -> str:
        """Daily report — run at 09:00."""
        report = self.analytics.daily_report()
        stock_alert = self.inventory.check_stock_levels()
        return f"=== РАНКОВИЙ БРИФІНГ ===\n\n{report}\n\n=== СКЛАД ===\n\n{stock_alert}"

    def process_new_orders(self) -> str:
        """Confirm and notify on new orders — run every 30 min."""
        return self.orders.process_new_orders()

    def weekly_report(self) -> str:
        """Weekly analytics — run every Monday at 09:00."""
        return self.analytics.weekly_report()

    # ------------------------------------------------------------------
    # Direct queries (for CLI / admin interface)
    # ------------------------------------------------------------------

    def ask_analytics(self, question: str) -> str:
        return self.analytics.ask(question)

    def add_tracking(self, order_number: str, tracking: str) -> str:
        return self.orders.add_tracking(order_number, tracking)

    def receive_stock(self, product_id: str, quantity: int, reason: str = "") -> str:
        return self.inventory.receive_stock(product_id, quantity, reason)

    # ------------------------------------------------------------------
    # Guardian — child safety
    # ------------------------------------------------------------------

    def guardian_queue(self) -> str:
        """Обзор очереди доказательств, ожидающих проверки человеком."""
        return self.guardian.summarize_queue()

    def guardian_report(self, report_id: str) -> str:
        """Собрать пакет для ручной подачи в органы по подтверждённому случаю."""
        from tools.child_safety_tools import build_authority_report
        bundle = build_authority_report(report_id)
        if "error" in bundle:
            return f"Ошибка: {bundle['error']}"
        lines = [bundle["report_text"], "", "=== КУДА ПОДАВАТЬ (вручную) ==="]
        for key, link in bundle["submission_links"].items():
            lines.append(f"- {key}: {link}")
        return "\n".join(lines)
