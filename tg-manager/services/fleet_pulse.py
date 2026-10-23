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

from services import account_status as _acc_status
from services.logger import log_exc_swallow

log = logging.getLogger(__name__)

# Порядок состояний для сортировки и сводки — от «требует внимания» к «в норме».
#
# `blocked` — не пауза и не смерть: аккаунт цел, но работать им нельзя, пока
# владелец что-то не сделает руками (залить сессию, заменить прокси). Ждать
# бесполезно, поэтому и «пауза» тут соврала бы, и «готов» — тем более.
STATE_ORDER = ("dead", "blocked", "quarantine", "cooling", "ready")

_STATE_LABEL = {
    "dead": "⛔️ выбыл",
    "blocked": "🔧 нужна починка",
    "quarantine": "🚑 карантин",
    "cooling": "⏳ пауза",
    "ready": "✅ готов",
}


def _proxy_is_dead(row) -> bool:
    """Заведомо ли мёртв прокси аккаунта — предикат ОДИН на весь продукт.

    Своего здесь не держим: дверь выбора аккаунтов судит по
    `resource_selector._proxy_is_dead`, и разойтись им нельзя — иначе экран
    снова начнёт звать готовым то, чего операции не берут.
    """
    try:
        from services.resource_selector import _proxy_is_dead as _door
    except Exception:
        return False          # нет двери — не выдумываем свою оценку
    try:
        return bool(_door(row))
    except Exception:
        return False


def _until_utc_midnight(now) -> float:
    """Сколько секунд до обнуления суточного лимита действий."""
    from datetime import timedelta
    tomorrow = (now + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return max(0.0, (tomorrow - now).total_seconds())


# Порог доверия, ниже которого аккаунт не берут главные операции продукта
# (инвайты, рассылки, сообщения). Берём у двери, а не вписываем числом: порог
# живёт в flood_engine._ACTION_MIN_TRUST и однажды уже разъезжался с экранами.
def _trust_hint(trust_score) -> str | None:
    """Чем доверие помешает аккаунту, который в остальном готов.

    Это не пауза: аккаунт рабочий, и часть операций его возьмёт. Но инвайт и
    рассылка — нет, и экран обязан сказать это раньше, чем владелец запустит
    операцию и увидит «обработано 0».
    """
    from services import flood_engine as fe
    try:
        score = fe.normalize_trust_score(trust_score)
    except Exception:
        return None
    need = fe.min_trust_for_action("invite")
    if score >= need:
        return None
    return (f"доверие {score:.2f} ниже {need:.2f} — инвайты и рассылки "
            "этот аккаунт не возьмут")


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
    ready), reason (человеку), ready_in_sec, ready_in (строка), risk (0..1),
    hint (чем аккаунт ограничен, хотя и готов) — либо None.

    Состояние считается по ВСЕМУ, из-за чего операция аккаунт не возьмёт, а не
    только по статусу и кулдауну. Раньше экран знал про статус, кулдаун и
    карантин, а дверь выбора отсеивала ещё по четырём причинам — отсутствию
    сессии, мёртвому прокси, дневному лимиту и доверию. Про такой аккаунт экран
    писал «✅ готов», а сводка — «🎉 весь флот готов к действиям», после чего
    операция честно обрабатывала ноль целей. Это и есть вопрос владельца «из 52
    аккаунтов работают 20, а почему непонятно»: отвечать на него сделан ровно
    этот экран.
    """
    from services import flood_engine as fe
    try:
        from services import infra_memory as _im
    except Exception:
        _im = None

    # Поля сессии и прокси — те же, что читает дверь выбора аккаунтов: экран
    # обязан знать ВСЁ, из-за чего операция аккаунт не возьмёт, иначе он пишет
    # «готов» про того, кого не берут (см. docstring модуля).
    rows = await pool.fetch(
        "SELECT a.id, a.phone, a.first_name, a.username, "
        "COALESCE(a.acc_status,'active') AS acc_status, a.cooldown_until, "
        "COALESCE(a.trust_score, 0.5) AS trust_score, "
        "(a.session_str IS NOT NULL) AS has_session, "
        "p.is_alive AS proxy_alive, "
        "COALESCE(p.consecutive_failures, 0) AS proxy_fail_streak "
        "FROM tg_accounts a "
        "LEFT JOIN user_proxies p ON p.id = a.proxy_id AND p.is_active = TRUE "
        "WHERE a.owner_id=$1 AND a.is_active=TRUE "
        "ORDER BY a.id",
        owner_id)

    # Живая фаза авто-реабилитации по спам-блокам (если таблица есть).
    rehab: dict[int, dict] = {}
    try:
        rrows = await pool.fetch(
            "SELECT acc_id, phase, attempts, next_action_at "
            "FROM account_rehab_state WHERE owner_id=$1", owner_id)
        rehab = {int(r["acc_id"]): dict(r) for r in rrows}
    except Exception:
        rehab = {}

    # Карантин всего флота — ОДНИМ запросом до цикла. Раньше запрос уходил
    # внутри цикла по каждому аккаунту: экран флота на 200 аккаунтах ждал
    # двести round-trip подряд, и каждая его отрисовка занимала пул.
    quarantined: set[int] = set()
    if _im is not None:
        try:
            quarantined = await _im.quarantined_accounts(
                pool, [int(r["id"]) for r in rows])
        except Exception:
            log_exc_swallow(log, "fleet_pulse quarantine check")

    # Дневной лимит действий — ОДНИМ запросом на весь флот. Исчерпавший лимит
    # аккаунт дверь выбора не отдаёт (respect_daily_budget), а экран называл его
    # готовым: владелец запускал операцию и получал «ничего не произошло».
    over_budget: set[int] = set()
    try:
        from services import account_budget as _ab
        _, _over = await _ab.filter_within_budget(
            pool, [int(r["id"]) for r in rows])
        over_budget = {int(i) for i in _over}
    except Exception:
        log_exc_swallow(log, "fleet_pulse daily budget check")

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

        # Классификация состояния и человеческая причина.
        if _acc_status.is_dead(status):
            state, reason, ready_in = "dead", _dead_reason(status), None
            if status == "spamblock" and acc_id in rehab:
                reason = _rehab_reason(rehab[acc_id], now)
        elif not r["has_session"]:
            # Без сессии аккаунт не берёт НИ ОДНА операция (условие двери:
            # session_str IS NOT NULL). Ждать нечего — нужна переимпортация.
            state = "blocked"
            reason = "нет сессии — переимпортируйте аккаунт"
            ready_in = None
        elif _proxy_is_dead(r):
            # Прокси не отвечает подряд: сети до Telegram у аккаунта нет, и
            # каждая попытка — гарантированная сетевая ошибка. Чинится заменой
            # прокси (дешёвое), а выглядит как «аккаунты сломались» (дорогое).
            state = "blocked"
            reason = "прокси не отвечает — замените прокси аккаунта"
            ready_in = None
        elif acc_id in quarantined:
            state = "quarantine"
            reason = "снят риск-пульсом (недавние ограничения) — вернётся сам"
            ready_in = cooling if cooling > 0 else None
        elif cooling > 0:
            state = "cooling"
            reason = "пауза после флуда/ограничения — бережём аккаунт"
            ready_in = cooling
        elif acc_id in over_budget:
            # Это именно пауза: лимит суточный и сам отпустит в полночь UTC.
            state = "cooling"
            reason = "дневной лимит действий исчерпан — бережём аккаунт"
            ready_in = _until_utc_midnight(now)
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
            "hint": _trust_hint(r["trust_score"]) if state == "ready" else None,
        })

    out.sort(key=lambda a: (STATE_ORDER.index(a["state"]),
                            -(a["ready_in_sec"] or 0)))
    return out


def _rehab_reason(r: dict, now) -> str:
    """Человеческая причина по живой фазе авто-реабилитации спам-блока."""
    phase = r.get("phase")
    if phase == "appeal":
        return "спам-блок — запрашиваем снятие у @SpamBot"
    if phase == "warming":
        return "спам-блок — идёт тихий прогрев (реабилитация)"
    if phase == "recheck":
        nxt = r.get("next_action_at")
        when = ""
        try:
            left = (nxt - now).total_seconds()
            if left > 0:
                when = f" через {_human_left(left)}"
        except Exception:
            when = ""
        return f"спам-блок — перепроверка{when} (реабилитация)"
    if phase == "stuck":
        return "спам-блок не снят автоматически — нужен ручной разбор/перезаливка"
    return _dead_reason("spamblock")


def _dead_reason(status: str) -> str:
    return {
        "banned": "забанен Telegram — не восстановить",
        "deactivated": "деактивирован — не восстановить",
        "session_expired": "сессия истекла — перезалейте аккаунт",
        "spamblock": "спам-блок — идёт реабилитация/нужна перезаливка",
        "deleted": "аккаунт удалён в Telegram — не восстановить",
        "frozen": "аккаунт заморожен Telegram — нужен разбор/перезаливка",
    }.get(status, status)


def summarize(states: list[dict]) -> dict[str, int]:
    """Свод по состояниям для заголовка пульса."""
    s = {k: 0 for k in STATE_ORDER}
    for a in states:
        s[a["state"]] = s.get(a["state"], 0) + 1
    s["total"] = len(states)
    return s
