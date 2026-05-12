"""Inventory monitoring and management agent for BASIC.FOOD."""
from __future__ import annotations
from agents.base import BaseAgent
from tools.database_tools import (
    get_low_stock_products,
    get_all_products,
    update_stock,
    get_product,
)
from tools.analytics_tools import get_top_products, get_inventory_snapshot


SYSTEM = """Ти — ШІ-агент управління складом BASIC.FOOD.

ОБОВ'ЯЗКИ:
- Моніторинг рівнів запасів
- Попередження про низькі залишки (< 10 одиниць)
- Рекомендації щодо поповнення запасів на основі темпів продажів
- Оновлення залишків після надходження товару

ПРОДУКТИ BASIC.FOOD — натуральні сушені ласощі:
- Одиниця виміру — пачки/упаковки
- Термін придатності ~12 місяців
- Зберігаються при кімнатній температурі

ПРАВИЛА:
- Порогове значення низького запасу: ≤10 одиниць
- Критично мало: ≤3 одиниці (потрібне термінове поповнення)
- При оновленні залишків завжди вказуй причину
- Відповідай УКРАЇНСЬКОЮ мовою"""


class InventoryAgent(BaseAgent):
    name = "inventory"
    system_prompt = SYSTEM

    def __init__(self) -> None:
        super().__init__()
        self._register_all_tools()

    def _register_all_tools(self) -> None:
        self.register_tool(
            {
                "name": "get_inventory_snapshot",
                "description": "Загальна картина складу",
                "input_schema": {"type": "object", "properties": {}},
            },
            get_inventory_snapshot,
        )
        self.register_tool(
            {
                "name": "get_low_stock_products",
                "description": "Список товарів з низьким запасом",
                "input_schema": {
                    "type": "object",
                    "properties": {"threshold": {"type": "integer", "default": 10}},
                },
            },
            get_low_stock_products,
        )
        self.register_tool(
            {
                "name": "get_all_products",
                "description": "Список усіх активних товарів з залишками",
                "input_schema": {
                    "type": "object",
                    "properties": {"active_only": {"type": "boolean", "default": True}},
                },
            },
            get_all_products,
        )
        self.register_tool(
            {
                "name": "get_product",
                "description": "Деталі конкретного товару за ID",
                "input_schema": {
                    "type": "object",
                    "properties": {"product_id": {"type": "string"}},
                    "required": ["product_id"],
                },
            },
            get_product,
        )
        self.register_tool(
            {
                "name": "update_stock",
                "description": "Оновити залишок товару на складі",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "product_id": {"type": "string"},
                        "new_quantity": {"type": "integer"},
                        "reason": {"type": "string"},
                    },
                    "required": ["product_id", "new_quantity"],
                },
            },
            update_stock,
        )
        self.register_tool(
            {
                "name": "get_top_products",
                "description": "Топ продуктів за продажами (для прогнозу попиту)",
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

    def check_stock_levels(self) -> str:
        return self.run(
            "Перевір стан складу. Визнач товари з критично низьким запасом (≤3) та низьким (≤10). "
            "Підготуй список для поповнення з рекомендованою кількістю, враховуючи темп продажів за останні 30 днів."
        )

    def receive_stock(self, product_id: str, quantity: int, reason: str = "Надходження товару") -> str:
        return self.run(
            f"Зареєструй надходження {quantity} одиниць товару з ID {product_id}. "
            f"Причина: {reason}. Оновити залишок і підтвердити."
        )
