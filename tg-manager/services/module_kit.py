"""module_kit — единый каркас модуля: гейт → постановка → событие.

Убирает копипаст «проверить давление инфраструктуры → operation_bus.submit →
записать в шину организма», который сейчас разбросан по десяткам эндпоинтов.
Любой модуль ставит массовую операцию ОДИНАКОВО:

  1. ban-safety гейт (infra_orchestrator.is_ready_for_op) — не жжём флот под давлением;
  2. единая постановка через operation_bus (прогресс/отмена/тариф/ratchet);
  3. эмит события "op_queued" в организм (память + подсказки мозга).

Это фундамент «неостровности»: каждое направление подключается сюда, а не строит
свой путь. Fail-open там, где уместно (гейт/эмит — защита, не блокер).
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


class OpGateError(Exception):
    """Инфраструктура не готова к операции (давление/нет аккаунтов). Несёт
    человекочитаемую причину для ответа 429."""


async def ready_for(pool, owner_id: int, op_name: str) -> tuple[bool, str]:
    """Готова ли инфраструктура к операции. Fail-open (сбой проверки ≠ блок)."""
    try:
        from services import infra_orchestrator
        return await infra_orchestrator.is_ready_for_op(pool, owner_id, op_name)
    except Exception:
        log.debug("module_kit.ready_for check failed op=%s", op_name, exc_info=True)
        return True, ""


async def emit(pool, owner_id: int, kind: str, payload: dict | None = None) -> None:
    """Эмит события модуля в организм (тонкая обёртка над spine, fail-open)."""
    try:
        from services.organism import spine
        await spine.emit(pool, owner_id, kind, payload or {})
    except Exception:
        pass


async def submit_guarded(pool, owner_id: int, op_type: str, params: dict, *,
                         total_items: int = 1, label: str = "",
                         gate: bool = True, gate_name: str | None = None,
                         emit_kind: str = "op_queued") -> int:
    """Поставить массовую операцию единым каркасом. Возвращает op_id.

    gate=True — сначала ban-safety гейт (по gate_name или op_type); при перегрузе
    бросает OpGateError с причиной. Постановка — через operation_bus (может бросить
    PlanRequiredError/PermissionError по тарифу — пробрасываем). После постановки —
    эмит события в организм.
    """
    if gate:
        ok, reason = await ready_for(pool, owner_id, gate_name or op_type)
        if not ok:
            raise OpGateError(reason or "Инфраструктура перегружена")
    from services import operation_bus
    op_id = await operation_bus.submit(
        pool, owner_id, op_type, params, total_items=total_items, label=label)
    await emit(pool, owner_id, emit_kind,
               {"op_id": op_id, "op_type": op_type, "label": label})
    return op_id
