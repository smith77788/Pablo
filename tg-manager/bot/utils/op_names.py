"""Подписи операций и их статусов для ответов бота.

Экраны бота печатали код типа («mass_invite») и статус («failed») как есть.
Описание берётся из реестра operation_bus.OP_REGISTRY, статус — из
services.op_status.LABELS: один источник для бота и мини-аппа.
"""
from __future__ import annotations


def op_name(op_type, fallback: str | None = "Операция") -> str:
    """Название операции по-русски. Незнакомый тип — `fallback`
    (None — вернуть сам код: для сводок, где важно различать строки)."""
    from services import operation_bus as _ob
    code = str(op_type or "")
    desc = _ob.OP_REGISTRY.get(code, {}).get("description")
    if desc:
        return desc
    return code if fallback is None else fallback


def status_label(status) -> str:
    from services import op_status as _os
    return _os.label(status)
