"""Telegram rich-message payloads, including the Bot API 10.5 quote migration.

Source: https://core.telegram.org/bots/api/types/inputrichmessage
and https://core.telegram.org/bots/api/types/inputrichblockexpandableblockquotation.
No message content is logged. Unknown format metadata is retained for forwards
compatibility; the server remains the authority for individual block types.
"""

from __future__ import annotations

import math
from typing import Any

MAX_BLOCKS = 500
MAX_DEPTH = 16
MAX_STRING = 1_048_576


def normalize_rich_message(value: dict) -> dict:
    """Return an independent JSON payload; upgrade legacy expandable quotes.

    A legacy quote's RichText becomes one paragraph, retaining formatting and
    credit. Supplying both old text and new blocks is ambiguous and rejected.
    """
    if not isinstance(value, dict):
        raise ValueError("Форматированное сообщение должно быть объектом")  # noqa: TRY004
    if sum(key in value for key in ("html", "markdown", "blocks")) != 1:
        raise ValueError("Выберите один формат: HTML, Markdown или блоки")
    for flag in ("is_rtl", "skip_entity_detection"):
        if flag in value and not isinstance(value[flag], bool):
            raise ValueError("Параметры форматирования должны быть логическими")
    for field in ("html", "markdown"):
        if field in value and (not isinstance(value[field], str) or not value[field]):
            raise ValueError("Текст форматированного сообщения не должен быть пустым")
    if "blocks" in value and (
        not isinstance(value["blocks"], list) or not value["blocks"]
    ):
        raise ValueError("Укажите непустой список блоков")
    if "media" in value and (
        not isinstance(value["media"], list) or len(value["media"]) > 50
    ):
        raise ValueError("В сообщении допускается не более 50 вложений")
    ancestors: set[int] = set()
    block_count = 0

    def copy(item: Any, depth: int = 0, block_depth: int = 0) -> Any:
        nonlocal block_count
        if depth > 64 or block_depth > MAX_DEPTH:
            raise ValueError("Слишком большая вложенность форматирования")
        if isinstance(item, str):
            if len(item) > MAX_STRING:
                raise ValueError("Слишком большое поле сообщения")
            return item
        if item is None or isinstance(item, (bool, int)):
            return item
        if isinstance(item, float):
            if not math.isfinite(item):
                raise ValueError("Недопустимое число в сообщении")
            return item
        if not isinstance(item, (dict, list)):
            raise ValueError("Сообщение содержит неподдерживаемые данные")  # noqa: TRY004
        identity = id(item)
        if identity in ancestors:
            raise ValueError("Обнаружена циклическая структура сообщения")
        ancestors.add(identity)
        try:
            if isinstance(item, list):
                return [copy(child, depth + 1, block_depth) for child in item]
            if any(not isinstance(key, str) for key in item):
                raise ValueError("Названия полей сообщения должны быть строками")
            prepared = dict(item)
            if prepared.get("type") == "expandable_blockquote":
                if "text" in prepared and "blocks" in prepared:
                    raise ValueError(
                        "Цитата не может содержать одновременно текст и блоки"
                    )
                if "text" in prepared:
                    prepared["blocks"] = [
                        {"type": "paragraph", "text": prepared.pop("text")}
                    ]
                if (
                    not isinstance(prepared.get("blocks"), list)
                    or not prepared["blocks"]
                ):
                    raise ValueError("Раскрывающаяся цитата должна содержать блоки")
            out = {}
            for key, child in prepared.items():
                if key == "blocks":
                    if not isinstance(child, list) or any(
                        not isinstance(b, dict) for b in child
                    ):
                        raise ValueError("Блоки должны быть списком объектов")
                    block_count += len(child)
                    if block_count > MAX_BLOCKS:
                        raise ValueError("В сообщении допускается не более 500 блоков")
                    out[key] = copy(child, depth + 1, block_depth + 1)
                else:
                    # Type identifiers are metadata, not displayed text.
                    out[key] = (
                        child
                        if key == "type" and isinstance(child, str)
                        else copy(child, depth + 1, block_depth)
                    )
            return out
        finally:
            ancestors.remove(identity)

    return copy(value)


def method_is_unavailable(response: dict) -> bool:
    """Only a definite unknown-method reply permits an explicit plain fallback."""
    description = str(response.get("description") or "").strip().lower()
    return response.get("error_code") == 404 and (
        description == "not found" or "method not found" in description
    )
