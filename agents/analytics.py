"""Analytics and reporting agent for BASIC.FOOD."""
from __future__ import annotations
from agents.base import BaseAgent
from tools.analytics_tools import (
    get_sales_summary,
    get_orders_by_status_count,
    get_top_products,
    get_top_customers,
    get_inventory_snapshot,
    get_customer_lifecycle_breakdown,
    get_daily_revenue,
)


SYSTEM = """Ти — ШІ-аналітик BASIC.FOOD. Аналізуєш дані бізнесу і надаєш чіткі, структуровані звіти.

ПРАВИЛА:
- Усі суми відображай у гривнях (UAH), не в копійках
- Використовуй markdown-форматування (таблиці, списки)
- Вказуй тренди: зріст/падіння порівняно з попереднім періодом якщо є дані
- Додавай конкретні рекомендації на основі даних
- Відповідай УКРАЇНСЬКОЮ мовою

МЕТРИКИ ЯКІ ВІДСТЕЖУЄШ:
- Виручка та кількість замовлень
- Популярні продукти
- Кращі клієнти (LTV)
- Запаси на складі
- Стадії life cycle клієнтів"""


class AnalyticsAgent(BaseAgent):
    name = "analytics"
    system_prompt = SYSTEM

    def __init__(self) -> None:
        super().__init__()
        self._register_all_tools()

    def _register_all_tools(self) -> None:
        self.register_tool(
            {
                "name": "get_sales_summary",
                "description": "Зведення по продажах за N останніх днів",
                "input_schema": {
                    "type": "object",
                    "properties": {"days": {"type": "integer", "default": 30}},
                },
            },
            get_sales_summary,
        )
        self.register_tool(
            {
                "name": "get_orders_by_status_count",
                "description": "Кількість замовлень по статусах",
                "input_schema": {
                    "type": "object",
                    "properties": {"days": {"type": "integer", "default": 30}},
                },
            },
            get_orders_by_status_count,
        )
        self.register_tool(
            {
                "name": "get_top_products",
                "description": "Топ продуктів за виручкою",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "days": {"type": "integer", "default": 30},
                        "limit": {"type": "integer", "default": 10},
                    },
                },
            },
            get_top_products,
        )
        self.register_tool(
            {
                "name": "get_top_customers",
                "description": "Топ клієнтів за витратами",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "days": {"type": "integer", "default": 30},
                        "limit": {"type": "integer", "default": 10},
                    },
                },
            },
            get_top_customers,
        )
        self.register_tool(
            {
                "name": "get_inventory_snapshot",
                "description": "Поточний стан складу: одиниці, вартість, залишки",
                "input_schema": {"type": "object", "properties": {}},
            },
            get_inventory_snapshot,
        )
        self.register_tool(
            {
                "name": "get_customer_lifecycle_breakdown",
                "description": "Розподіл клієнтів по стадіях life cycle",
                "input_schema": {"type": "object", "properties": {}},
            },
            get_customer_lifecycle_breakdown,
        )
        self.register_tool(
            {
                "name": "get_daily_revenue",
                "description": "Виручка по днях за останні N днів",
                "input_schema": {
                    "type": "object",
                    "properties": {"days": {"type": "integer", "default": 14}},
                },
            },
            get_daily_revenue,
        )

    def daily_report(self) -> str:
        return self.run(
            "Підготуй щоденний звіт по бізнесу: виручка за останні 7 і 30 днів, "
            "топ-5 продуктів, стан складу (особливо низькі залишки), "
            "розподіл замовлень по статусах. Додай 2-3 рекомендації."
        )

    def weekly_report(self) -> str:
        return self.run(
            "Підготуй тижневий звіт: виручка за 7 і 30 днів з динамікою по днях, "
            "топ-10 продуктів, топ-10 клієнтів по LTV, стан life cycle бази, "
            "повний огляд складу. Завершити стратегічними рекомендаціями."
        )

    def ask(self, question: str) -> str:
        return self.run(question)
