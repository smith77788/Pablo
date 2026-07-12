"""Claude conversation engine for the assistant bot.

Manual tool loop over the Messages API (same shape as agents/base.py) with a
JSON-serializable history so chats survive bot restarts. Tools expose the
whole Pablo business layer: briefing, orders, stock, analytics, tracking.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

import anthropic

logger = logging.getLogger(__name__)

MAX_TOKENS = 8192
MAX_TOOL_ITERATIONS = 15

SYSTEM_PROMPT = """\
Ты — Pablo, личный ИИ-ассистент владельца BASIC.FOOD, работающий в Telegram.

Что ты умеешь:
- Отвечать на любые вопросы как полноценный Claude-ассистент.
- Управлять бизнесом BASIC.FOOD через инструменты: утренний брифинг, обработка
  новых заказов, остатки на складе, недельная аналитика, произвольные
  аналитические вопросы, добавление ТТН Новой Почты, оприходование товара.
- Анализировать присланные фото и документы (PDF, текстовые файлы).

Правила общения:
- Отвечай на языке собеседника (русский или украинский).
- Ответ уходит в Telegram обычным текстом: НЕ используй markdown-разметку
  (##, **, таблицы, ```). Структурируй списками с дефисами и эмодзи.
- Будь краток и по делу: сначала итог, потом детали.
- Суммы показывай в гривнах (UAH).
- Если инструмент вернул ошибку — скажи об этом честно и предложи, что делать.
"""

TOOLS: list[dict] = [
    {
        "name": "morning_briefing",
        "description": (
            "Утренний брифинг: дневной отчёт по продажам и алерты по складу. "
            "Вызывай, когда просят брифинг, сводку за день или 'как дела в бизнесе'."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "process_new_orders",
        "description": (
            "Обработать новые заказы: подтвердить и разослать уведомления. "
            "Вызывай по просьбе обработать/проверить новые заказы."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "check_stock",
        "description": "Проверить остатки на складе и товары на исходе.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "weekly_report",
        "description": "Недельный аналитический отчёт по бизнесу.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "ask_analytics",
        "description": (
            "Задать произвольный вопрос аналитическому агенту по данным "
            "BASIC.FOOD (продажи, клиенты, товары, LTV, тренды)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "Вопрос на естественном языке"}
            },
            "required": ["question"],
        },
    },
    {
        "name": "add_tracking",
        "description": "Добавить ТТН Новой Почты к заказу и уведомить клиента.",
        "input_schema": {
            "type": "object",
            "properties": {
                "order_number": {"type": "string", "description": "Номер заказа"},
                "tracking": {"type": "string", "description": "Номер ТТН"},
            },
            "required": ["order_number", "tracking"],
        },
    },
    {
        "name": "receive_stock",
        "description": "Оприходовать поступление товара на склад.",
        "input_schema": {
            "type": "object",
            "properties": {
                "product_id": {"type": "string", "description": "ID товара"},
                "quantity": {"type": "integer", "description": "Количество"},
                "reason": {
                    "type": "string",
                    "description": "Комментарий к поступлению",
                    "default": "Надходження товару",
                },
            },
            "required": ["product_id", "quantity"],
        },
    },
]

_pablo = None


def _get_pablo():
    """Lazily build the orchestrator so the bot works even when the business
    stack (Supabase и т.д.) не сконфигурирован — ошибка уйдёт в tool_result."""
    global _pablo
    if _pablo is None:
        from orchestrator import Pablo

        _pablo = Pablo()
    return _pablo


def _tool_handlers() -> dict[str, Callable[..., Any]]:
    return {
        "morning_briefing": lambda: _get_pablo().morning_briefing(),
        "process_new_orders": lambda: _get_pablo().process_new_orders(),
        "check_stock": lambda: _get_pablo().inventory.check_stock_levels(),
        "weekly_report": lambda: _get_pablo().weekly_report(),
        "ask_analytics": lambda question: _get_pablo().ask_analytics(question),
        "add_tracking": lambda order_number, tracking: _get_pablo().add_tracking(
            order_number, tracking
        ),
        "receive_stock": lambda product_id, quantity, reason="Надходження товару": (
            _get_pablo().receive_stock(product_id, quantity, reason)
        ),
    }


def _run_tool(name: str, inputs: dict) -> tuple[str, bool]:
    """Execute a tool; returns (result_text, is_error)."""
    handler = _tool_handlers().get(name)
    if handler is None:
        return f"Неизвестный инструмент: {name}", True
    try:
        result = handler(**inputs)
        return json.dumps(result, ensure_ascii=False, default=str), False
    except Exception as e:  # tool errors go back to Claude, not to the user
        logger.exception("Tool %s failed", name)
        return f"Ошибка инструмента {name}: {e}", True


class ClaudeChat:
    """One Claude conversation turn with tools over a persistent history."""

    def __init__(self) -> None:
        # Built lazily on first turn so a missing/invalid ANTHROPIC_API_KEY
        # never blocks the bot from coming online and answering commands.
        self._client: anthropic.Anthropic | None = None

    @property
    def client(self) -> anthropic.Anthropic:
        if self._client is None:
            self._client = anthropic.Anthropic()
        return self._client

    def run_turn(
        self,
        model: str,
        history: list[dict],
        user_content: str | list[dict],
        on_progress: Callable[[], None] | None = None,
    ) -> tuple[str, list[dict]]:
        """Append the user message, run the tool loop, return (reply, history)."""
        history = list(history)
        history.append({"role": "user", "content": user_content})

        for _ in range(MAX_TOOL_ITERATIONS):
            if on_progress:
                on_progress()

            with self.client.messages.stream(
                model=model,
                max_tokens=MAX_TOKENS,
                thinking={"type": "adaptive"},
                system=SYSTEM_PROMPT,
                tools=TOOLS,
                messages=history,
            ) as stream:
                response = stream.get_final_message()

            content = [b.model_dump(exclude_none=True) for b in response.content]
            history.append({"role": "assistant", "content": content})

            if response.stop_reason == "tool_use":
                tool_results = []
                for block in response.content:
                    if block.type == "tool_use":
                        result_text, is_error = _run_tool(block.name, block.input)
                        tool_results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": result_text,
                                "is_error": is_error,
                            }
                        )
                history.append({"role": "user", "content": tool_results})
                continue

            if response.stop_reason == "pause_turn":
                continue

            if response.stop_reason == "refusal":
                return (
                    "Я не могу помочь с этим запросом. Попробуй переформулировать.",
                    history,
                )

            text = self._extract_text(response)
            if response.stop_reason == "max_tokens":
                text += "\n\n… (ответ обрезан по лимиту токенов, напиши «продолжи»)"
            return text or "(пустой ответ)", history

        return "Слишком длинная цепочка инструментов — остановился. Попробуй ещё раз.", history

    @staticmethod
    def _extract_text(response: Any) -> str:
        return "\n".join(
            block.text for block in response.content if block.type == "text"
        ).strip()
