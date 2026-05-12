"""Order management agent for BASIC.FOOD."""
from __future__ import annotations
from agents.base import BaseAgent
from tools.database_tools import (
    get_order,
    get_orders_by_status,
    get_recent_orders,
    update_order_status,
    set_tracking_number,
    search_customers,
    add_customer_note,
)
from tools.telegram_tools import send_message


SYSTEM = """Ти — ШІ-агент управління замовленнями BASIC.FOOD.

ОБОВ'ЯЗКИ:
- Відстежуй нові замовлення і підтверджуй їх
- Перевіряй статус оплати та доставки
- Оновлюй статуси замовлень
- Додавай трекінг-номери Nova Poshta
- Сповіщай клієнтів у Telegram про зміни статусу

СТАТУСИ ЗАМОВЛЕНЬ: new → confirmed → processing → shipped → delivered (або cancelled / refunded)

ВАЖЛИВО:
- Усі суми зберігаються в копійках (÷100 = гривні)
- Доставка через Нову Пошту, трекінг формат: 59XXXXXXXXXXXX
- Повернення коштів — тільки після підтвердження менеджера
- При скасуванні — завжди вказуй причину в нотатках"""


class OrderManagerAgent(BaseAgent):
    name = "order_manager"
    system_prompt = SYSTEM

    def __init__(self) -> None:
        super().__init__()
        self._register_all_tools()

    def _register_all_tools(self) -> None:
        self.register_tool(
            {
                "name": "get_order",
                "description": "Отримати деталі замовлення за номером",
                "input_schema": {
                    "type": "object",
                    "properties": {"order_number": {"type": "string"}},
                    "required": ["order_number"],
                },
            },
            get_order,
        )
        self.register_tool(
            {
                "name": "get_orders_by_status",
                "description": "Отримати замовлення за статусом",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "status": {
                            "type": "string",
                            "enum": ["new", "confirmed", "processing", "shipped", "delivered", "cancelled", "refunded"],
                        },
                        "limit": {"type": "integer", "default": 20},
                    },
                    "required": ["status"],
                },
            },
            get_orders_by_status,
        )
        self.register_tool(
            {
                "name": "get_recent_orders",
                "description": "Отримати нещодавні замовлення за N днів",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "days": {"type": "integer", "default": 7},
                        "limit": {"type": "integer", "default": 50},
                    },
                },
            },
            get_recent_orders,
        )
        self.register_tool(
            {
                "name": "update_order_status",
                "description": "Оновити статус замовлення",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "order_id": {"type": "string"},
                        "status": {"type": "string"},
                        "notes": {"type": "string", "default": ""},
                    },
                    "required": ["order_id", "status"],
                },
            },
            update_order_status,
        )
        self.register_tool(
            {
                "name": "set_tracking_number",
                "description": "Додати трекінг-номер Нової Пошти до замовлення",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "order_id": {"type": "string"},
                        "tracking": {"type": "string"},
                    },
                    "required": ["order_id", "tracking"],
                },
            },
            set_tracking_number,
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
                "name": "notify_customer",
                "description": "Надіслати Telegram-повідомлення клієнту",
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

    def process_new_orders(self) -> str:
        """Check and process all new orders."""
        return self.run("Перевір всі нові замовлення (статус 'new'). Підтверди кожне і повідом клієнтів через Telegram якщо є chat_id.")

    def add_tracking(self, order_number: str, tracking: str) -> str:
        """Add tracking number to an order and notify customer."""
        return self.run(
            f"Додай трекінг-номер {tracking} до замовлення {order_number}. "
            f"Після оновлення статусу повідом клієнта в Telegram (якщо є telegram_chat_id) про відправку."
        )
