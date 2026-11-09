"""Одна метка здоровья аккаунта для владельца.

Разрыв, который это закрывает. У аккаунта было шесть разных оценок здоровья
(`trust_score`, риск-пульс `infra_memory`, `account_health.health_score`,
готовность, щит, риск `flood_engine`), и экран аккаунтов собирал из них
значок сам, по своей цепочке условий: доверие в процентах рядом, «под
риском», «кулдаун», «прокси». Владелец видел набор цифр и не видел главного:
работает аккаунт или нет, и если нет, ждать или что-то делать.

Метка одна и из четырёх значений (плюс «выключен» для аккаунтов, которые
владелец выключил сам — это выбор, а не здоровье):

  • ok     — «Работает»: дверь выбора аккаунтов его возьмёт;
  • pause  — «Пауза»: не работает, но вернётся сам (кулдаун, карантин,
             дневной лимит, идущая реабилитация спам-блока) — есть время;
  • action — «Нужно действие»: сам не вернётся, нужен владелец (нет
             сессии, мёртвый прокси, спам-блок не снят, заморозка, сессия
             отозвана);
  • lost   — «Потерян»: бан или удаление, не восстановить.

Основа — `fleet_pulse.account_states`: он уже считает состояние по всему,
из-за чего операция аккаунт не возьмёт, тем же предикатом, что дверь выбора.
Здесь только сведение его состояния в метку. `verdict_for` — чистая функция.
"""
from __future__ import annotations

from services import account_status as _st

LABEL = {
    "ok": "Работает",
    "pause": "Пауза",
    "action": "Нужно действие",
    "lost": "Потерян",
    "off": "Выключен",
}

# Фазы реабилитации, в которых система работает над аккаунтом сама.
_REHAB_SELF = frozenset({"appeal", "warming", "recheck"})


def verdict_for(row: dict, pulse: dict | None) -> dict | None:
    """Метка аккаунта по строке списка и его состоянию из пульса флота.

    row — строка `tg_accounts` (is_active, acc_status). pulse — элемент
    `fleet_pulse.account_states` для этого аккаунта или None (пульс не
    посчитан). Возвращает {level, label, reason, ready_in_sec} или None,
    если судить не по чему.
    """
    status = str(row.get("acc_status") or "active").strip().lower()
    if status in _st.LOST_STATUSES:
        reason = (pulse or {}).get("reason") or f"{_st.ru_label(status)} — не восстановить"
        return {"level": "lost", "label": LABEL["lost"], "reason": reason,
                "ready_in_sec": None}
    if not row.get("is_active"):
        return {"level": "off", "label": LABEL["off"],
                "reason": "выключен вами — операции его не берут", "ready_in_sec": None}
    if not pulse:
        return None

    state = pulse.get("state")
    reason = pulse.get("reason") or ""
    ready_in = pulse.get("ready_in_sec")
    if state == "ready":
        return {"level": "ok", "label": LABEL["ok"],
                "reason": pulse.get("hint") or "готов к действиям", "ready_in_sec": 0}
    if state in ("cooling", "quarantine"):
        return {"level": "pause", "label": LABEL["pause"], "reason": reason,
                "ready_in_sec": ready_in}
    if state == "dead" and status == "spamblock" and pulse.get("rehab_phase") in _REHAB_SELF:
        return {"level": "pause", "label": LABEL["pause"], "reason": reason,
                "ready_in_sec": None}
    # blocked и восстановимые «мёртвые» статусы: сам аккаунт не вернётся.
    return {"level": "action", "label": LABEL["action"], "reason": reason,
            "ready_in_sec": None}


async def annotate(pool, owner_id: int, rows: list[dict]) -> None:
    """Проставить `verdict` строкам списка аккаунтов владельца (на месте)."""
    if not rows:
        return
    from services import fleet_pulse
    states = await fleet_pulse.account_states(pool, owner_id)
    by_id = {int(s["id"]): s for s in states}
    for r in rows:
        v = verdict_for(r, by_id.get(int(r["id"])))
        if v is not None:
            r["verdict"] = v


async def for_account(pool, owner_id: int, row: dict) -> dict | None:
    """Метка одного аккаунта — для его карточки. Пульс считается только по нему."""
    from services import fleet_pulse
    pulse = None
    if row.get("is_active"):
        states = await fleet_pulse.account_states(pool, owner_id, account_ids=[int(row["id"])])
        pulse = next((s for s in states if int(s["id"]) == int(row["id"])), None)
    return verdict_for(row, pulse)


def summarize(verdicts: list[dict | None]) -> dict:
    """Сколько аккаунтов в каждой метке."""
    out = {k: 0 for k in LABEL}
    for v in verdicts:
        if v:
            out[v["level"]] = out.get(v["level"], 0) + 1
    return out
