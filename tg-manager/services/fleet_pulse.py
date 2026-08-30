"""Пульс флота: по каждому аккаунту — состояние, причина паузы, время до готовности.

ЗАЧЕМ (ось №1 аудита). Когда операция «ничего не делает», причина сейчас видна
только в логах: аккаунт в кулдауне, в карантине риск-пульса, выбыл по статусу.
Владелец этого не видит и решает, что «продукт сломан». Пульс собирает живое
состояние флота в один экран: кто работает, кто на паузе, почему и до какого
времени. Данные — из БД (долговечные: статус, cooldown_until, доверие) плюс
in-memory flood_engine (свежий остаток кулдауна и накопленный риск в этом
процессе). Берём строжайшее из двух — не занижаем паузу.
"""
from __future__ import annotations

import logging
from typing import Any

from services.logger import log_exc_swallow

log = logging.getLogger(__name__)

# Порядок состояний для сортировки и сводки — от «требует внимания» к «в норме».
STATE_ORDER = ("dead", "quarantine", "cooling", "ready")

_STATE_LABEL = {
    "dead": "⛔️ выбыл",
    "quarantine": "🚑 карантин",
    "cooling": "⏳ пауза",
    "ready": "✅ готов",
}


def _human_left(seconds: float) -> str:
    s = int(max(0, seconds))
    if s <= 0:
        return "сейчас"
    if s < 60:
        return f"{s} с"
    if s < 3600:
        return f"{s // 60} мин"
    return f"{s // 3600} ч {(s % 3600) // 60} мин"


async def account_states(pool, owner_id: int) -> list[dict[str, Any]]:
    """Состояние каждого активного аккаунта владельца.

    Возвращает список словарей: id, phone, name, state (dead|quarantine|cooling|
    ready), reason (человеку), ready_in_sec, ready_in (строка), risk (0..1).
    """
    from services import flood_engine as fe
    try:
        from services import infra_memory as _im
    except Exception:
        _im = None

    rows = await pool.fetch(
        "SELECT id, phone, first_name, username, "
        "COALESCE(acc_status,'active') AS acc_status, cooldown_until, "
        "COALESCE(trust_score, 0.5) AS trust_score "
        "FROM tg_accounts WHERE owner_id=$1 AND is_active=TRUE "
        "ORDER BY id",
        owner_id)

    import time as _t
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    out: list[dict[str, Any]] = []
    for r in rows:
        acc_id = int(r["id"])
        status = r["acc_status"]

        # Остаток кулдауна: строжайшее из БД (долговечное) и памяти (свежее).
        cd_db = 0.0
        cu = r["cooldown_until"]
        if cu is not None:
            try:
                cd_db = max(0.0, (cu - now).total_seconds())
            except Exception:
                cd_db = 0.0
        cd_mem = 0.0
        try:
            cd_mem = float(fe.seconds_until_ready(acc_id))
        except Exception:
            cd_mem = 0.0
        cooling = max(cd_db, cd_mem)

        # Риск/карантин.
        risk = 0.0
        try:
            risk = float(fe.get_account_state(acc_id).risk_score)
        except Exception:
            risk = 0.0
        quarantined = False
        if _im is not None:
            try:
                quarantined = bool(await _im.is_account_quarantined(pool, acc_id))
            except Exception:
                log_exc_swallow(log, "fleet_pulse quarantine check")

        # Классификация состояния и человеческая причина.
        if status in ("banned", "deactivated", "session_expired", "spamblock"):
            state, reason, ready_in = "dead", _dead_reason(status), None
        elif quarantined:
            state = "quarantine"
            reason = "снят риск-пульсом (недавние ограничения) — вернётся сам"
            ready_in = cooling if cooling > 0 else None
        elif cooling > 0:
            state = "cooling"
            reason = "пауза после флуда/ограничения — бережём аккаунт"
            ready_in = cooling
        else:
            state, reason, ready_in = "ready", "готов к действиям", 0.0

        out.append({
            "id": acc_id,
            "phone": r["phone"],
            "name": r["first_name"] or r["username"] or r["phone"] or str(acc_id),
            "state": state,
            "state_label": _STATE_LABEL[state],
            "reason": reason,
            "ready_in_sec": None if ready_in is None else int(ready_in),
            "ready_in": None if ready_in is None else _human_left(ready_in),
            "risk": round(risk, 2),
            "trust": round(float(r["trust_score"]), 2),
        })

    out.sort(key=lambda a: (STATE_ORDER.index(a["state"]),
                            -(a["ready_in_sec"] or 0)))
    return out


def _dead_reason(status: str) -> str:
    return {
        "banned": "забанен Telegram — не восстановить",
        "deactivated": "деактивирован — не восстановить",
        "session_expired": "сессия истекла — перезалейте аккаунт",
        "spamblock": "спам-блок — идёт реабилитация/нужна перезаливка",
    }.get(status, status)


def summarize(states: list[dict]) -> dict[str, int]:
    """Свод по состояниям для заголовка пульса."""
    s = {k: 0 for k in STATE_ORDER}
    for a in states:
        s[a["state"]] = s.get(a["state"], 0) + 1
    s["total"] = len(states)
    return s
