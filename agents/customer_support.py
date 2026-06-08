"""Customer support agent for BASIC.FOOD."""

from __future__ import annotations
import json
from agents.base import BaseAgent
from tools.database_tools import (
    get_customer_by_telegram,
    get_customer_by_email,
    search_customers,
    get_customer_orders,
    add_customer_note,
    get_customer_notes,
    mark_message_resolved,
    search_products,
)
from tools.telegram_tools import send_message


SYSTEM = """Ти — ШІ-агент служби підтримки BASIC.FOOD, інтернет-магазину натуральних повітряно-сушених ласощів з яловичини для собак. Ми — українська компанія, працюємо в Україні.

ОСОБИСТІСТЬ:
- Дружній, турботливий, компетентний у питаннях харчування собак
- Відповідаєш ВИКЛЮЧНО українською мовою (якщо клієнт пише руcькою — тактовно переходь на українську)
- Звертайся на ім'я, якщо знаєш його
- Не обіцяй конкретних строків доставки без перевірки в системі

ПРОДУКТИ: натуральні сушені ласощі (легеня, серце, вим'я, нирки, тощо). Без консервантів. Доставка через Нову Пошту.

ІНСТРУМЕНТИ:
- Завжди спочатку перевіряй профіль клієнта і його замовлення
- Додавай нотатки після кожної важливої взаємодії
- Якщо проблема вирішена — позначай повідомлення як вирішене
- Для питань про товари — використовуй search_products

ОБМЕЖЕННЯ:
- Не робиш повернення коштів самостійно — ескалюй до менеджера
- Не змінюєш ціни та акції
- Не маєш доступу до платіжних даних"""

WELCOME_MESSAGE = """👋 Вітаємо у <b>BASIC.FOOD</b> — магазині натуральних ласощів для собак!

Ми пропонуємо повітряно-сушені ласощі з яловичини: легеня, серце, вим'я, нирки — без консервантів та барвників. 🐾

Чим можу допомогти?
• Запитання про товари
• Статус замовлення
• Доставка Новою Поштою
• Інше"""

_MAX_HISTORY = 40


class CustomerSupportAgent(BaseAgent):
    name = "customer_support"
    system_prompt = SYSTEM

    def __init__(self) -> None:
        super().__init__()
        self._chat_histories: dict[int, list[dict]] = {}
        self._register_all_tools()

    def _get_history(self, chat_id: int) -> list[dict]:
        return self._chat_histories.setdefault(chat_id, [])

    def _trim_history(self, history: list[dict]) -> None:
        """Keep the most recent _MAX_HISTORY messages to cap token usage."""
        if len(history) > _MAX_HISTORY:
            del history[: len(history) - _MAX_HISTORY]

    def _register_all_tools(self) -> None:
        self.register_tool(
            {
                "name": "get_customer_by_telegram",
                "description": "Знайти клієнта за Telegram chat_id",
                "input_schema": {
                    "type": "object",
                    "properties": {"chat_id": {"type": "integer"}},
                    "required": ["chat_id"],
                },
            },
            get_customer_by_telegram,
        )
        self.register_tool(
            {
                "name": "get_customer_by_email",
                "description": "Знайти клієнта за email",
                "input_schema": {
                    "type": "object",
                    "properties": {"email": {"type": "string"}},
                    "required": ["email"],
                },
            },
            get_customer_by_email,
        )
        self.register_tool(
            {
                "name": "search_customers",
                "description": "Пошук клієнтів за ім'ям, email або телефоном",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "limit": {"type": "integer", "default": 5},
                    },
                    "required": ["query"],
                },
            },
            search_customers,
        )
        self.register_tool(
            {
                "name": "get_customer_orders",
                "description": "Отримати список замовлень клієнта",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "customer_id": {"type": "string"},
                        "limit": {"type": "integer", "default": 10},
                    },
                    "required": ["customer_id"],
                },
            },
            get_customer_orders,
        )
        self.register_tool(
            {
                "name": "get_customer_notes",
                "description": "Отримати нотатки по клієнту",
                "input_schema": {
                    "type": "object",
                    "properties": {"customer_id": {"type": "string"}},
                    "required": ["customer_id"],
                },
            },
            get_customer_notes,
        )
        self.register_tool(
            {
                "name": "add_customer_note",
                "description": "Додати нотатку до профілю клієнта",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "customer_id": {"type": "string"},
                        "note": {"type": "string"},
                    },
                    "required": ["customer_id", "note"],
                },
            },
            add_customer_note,
        )
        self.register_tool(
            {
                "name": "mark_message_resolved",
                "description": "Позначити звернення як вирішене",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "message_id": {"type": "string"},
                        "agent_response": {"type": "string"},
                    },
                    "required": ["message_id"],
                },
            },
            mark_message_resolved,
        )
        self.register_tool(
            {
                "name": "send_telegram_message",
                "description": "Надіслати повідомлення клієнту в Telegram",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "chat_id": {"type": "integer"},
                        "text": {"type": "string"},
                    },
                    "required": ["chat_id", "text"],
                },
            },
            send_message,
        )
        self.register_tool(
            {
                "name": "search_products",
                "description": "Знайти товари за назвою або описом",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "limit": {"type": "integer", "default": 5},
                    },
                    "required": ["query"],
                },
            },
            search_products,
        )

    def handle_telegram(
        self, chat_id: int, user_text: str, customer: dict | None = None
    ) -> str:
        """Process an inbound Telegram message and send reply."""
        if user_text.strip() == "/start":
            send_message(chat_id, WELCOME_MESSAGE)
            self._chat_histories.pop(chat_id, None)
            return WELCOME_MESSAGE

        history = self._get_history(chat_id)
        self._trim_history(history)

        context = {"chat_id": chat_id, "customer": customer}
        system = (
            self.system_prompt
            + f"\n\n<context>{json.dumps(context, ensure_ascii=False)}</context>"
        )

        prompt = f"Повідомлення від клієнта (chat_id={chat_id}): {user_text}"
        reply, _ = self.run_with_history(system, history, prompt)
        send_message(chat_id, reply)
        return reply
