"""Наблюдатели фоновых проверок: кто смотрит на наши каналы, чаты и ботов.

Проверки «Ограничения и теневой бан» и «Гео-доступность» (разово и по
расписанию раз в 12 часов, services/mini_app_monitor) смотрят на сущности
владельца ГЛАЗАМИ АККАУНТОВ ФЛОТА: резолв @username, глобальный поиск,
и раньше ещё /start каждому боту. Владелец увидел (13.11.2026), что после
таких проверок аккаунты-наблюдатели получают от @SpamBot ограничение «жалоба
на незаконные материалы»: нельзя создавать каналы и писать в публичные группы.

Почему Telegram так решает, снаружи не видно: проверки ничего не публикуют.
Но три свойства прежней схемы делали ущерб максимальным при ЛЮБОЙ причине:

* пул наблюдателей сдвигался на каждом прогоне (`op_id % len`), то есть за
  несколько суток каждую сущность сети «трогал» весь флот — и каждый аккаунт
  становился в глазах Telegram связанным с этой сетью;
* /start каждому боту владельца уходил с аккаунта флота — это единственное
  исходящее сообщение во всей проверке, и его же с тем же успехом даёт getMe по
  токену бота, вообще без аккаунта;
* аккаунт, уже получивший ограничение, оставался наблюдателем, а расписание
  продолжало гонять проверку и дальше, сколько бы аккаунтов ни пострадало.

Здесь — общие двери для обоих исполнителей:

* `observer_where()` — наблюдатель только живой и без ограничений;
* `stable_pick()` — ПОСТОЯННЫЙ небольшой набор наблюдателей, а не ротация по
  флоту: если проверка вредит, вред остаётся на тех же двух-трёх аккаунтах;
  аккаунты, которые сами ведут каналы владельца, наблюдателями не берутся, пока
  есть другие;
* `tripped()` — предохранитель: наблюдатель прошлого прогона получил
  ограничение после него → расписание проверки останавливается, владелец
  получает объяснение;
* `mark_for_recheck()` — наблюдатели после прогона первыми идут в проверку
  @SpamBot (в течение часа), чтобы предохранитель видел ограничение сразу, а
  не через сутки.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

log = logging.getLogger(__name__)

# Ключи в params повторяющейся операции: следующий круг их наследует.
LAST_OBSERVERS_KEY = "observer_ids_last"
LAST_RUN_KEY = "observer_run_at"


def observer_where(alias: str = "a") -> str:
    """Условие «аккаунт годится в наблюдатели»: живой и без ограничений."""
    from services.account_status import sql_not_dead
    return sql_not_dead(f"{alias}.acc_status")


def stable_pick(observers: list[dict], n: int, *, avoid_ids=()) -> list[dict]:
    """Постоянный набор из n наблюдателей: одни и те же от прогона к прогону.

    Порядок — по id, то есть не зависит ни от номера операции, ни от времени.
    avoid_ids (например, аккаунты, которые ведут каналы владельца) берутся
    только если без них не набрать n.
    """
    avoid = {int(x) for x in (avoid_ids or ())}
    ordered = sorted(observers, key=lambda a: int(a["id"]))
    first = [a for a in ordered if int(a["id"]) not in avoid]
    rest = [a for a in ordered if int(a["id"]) in avoid]
    return (first + rest)[:max(0, int(n))]


def remember_run(params: dict, observer_ids) -> None:
    """Записать в params, кто смотрел в этот раз (следующий круг их проверит)."""
    params[LAST_OBSERVERS_KEY] = sorted({int(x) for x in observer_ids})
    params[LAST_RUN_KEY] = datetime.now(timezone.utc).isoformat()


def _since(params: dict):
    raw = params.get(LAST_RUN_KEY)
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None


async def tripped(pool, params: dict) -> list[dict]:
    """Наблюдатели прошлого прогона, получившие ограничение ПОСЛЕ него.

    Сбой чтения — пустой список: остановить проверку по блипу базы хуже, чем
    пропустить один круг предохранителя (следующий прочитает снова).
    """
    ids = [int(x) for x in (params.get(LAST_OBSERVERS_KEY) or [])]
    since = _since(params)
    if not ids or since is None:
        return []
    from services.account_status import DEAD_STATUSES, sql_status_list
    try:
        rows = await pool.fetch(
            "SELECT DISTINCT ON (e.acc_id) e.acc_id, e.new_status, e.reason, "
            "       a.first_name, a.username, a.phone "
            "FROM account_status_events e JOIN tg_accounts a ON a.id = e.acc_id "
            "WHERE e.acc_id = ANY($1::bigint[]) AND e.created_at > $2 "
            f"  AND e.new_status IN ({sql_status_list(DEAD_STATUSES - {'session_expired'})}) "
            "ORDER BY e.acc_id, e.created_at DESC",
            ids, since)
    except Exception as exc:
        log.warning("observer_guard.tripped: %s", exc)
        return []
    return [dict(r) for r in rows]


def trip_summary(hit: list[dict], what: str) -> str:
    from services.account_status import ru_label
    lines = [
        f"⛔ <b>{what}: проверка остановлена</b>",
        "",
        f"После прошлой проверки аккаунты-наблюдатели получили ограничение "
        f"Telegram ({len(hit)}):",
    ]
    for r in hit[:5]:
        name = (f"@{r['username']}" if r.get("username")
                else (r.get("first_name") or r.get("phone") or f"#{r['acc_id']}"))
        lines.append(f"• {name} — {ru_label(r.get('new_status'))}")
    lines += [
        "",
        "Чтобы не терять аккаунты дальше, повтор по расписанию снят. Запускайте "
        "проверку вручную и по одной, когда нужно. Наблюдатели теперь постоянные — "
        "одни и те же два-три аккаунта, а не весь флот по очереди.",
    ]
    return "\n".join(lines)


async def stop_schedule(pool, op_id: int, params: dict) -> None:
    """Снять повтор у этой операции — и в памяти, и в базе: исцеление
    расписания (recurring_chain_alive) читает params из строки и подняло бы
    круг заново, если снять только в памяти."""
    params["repeat_interval_min"] = 0
    try:
        await pool.execute(
            "UPDATE operation_queue SET params = jsonb_set(COALESCE(params, '{}'::jsonb), "
            "'{repeat_interval_min}', '0'::jsonb) WHERE id=$1", int(op_id))
    except Exception as exc:
        log.warning("observer_guard.stop_schedule op=%s: %s", op_id, exc)


async def mark_for_recheck(pool, acc_ids) -> None:
    """Поставить наблюдателей первыми в очередь проверки @SpamBot."""
    ids = sorted({int(x) for x in (acc_ids or ())})
    if not ids:
        return
    try:
        await pool.execute(
            "UPDATE tg_accounts SET last_spambot_check_at = NULL WHERE id = ANY($1::bigint[])",
            ids)
    except Exception as exc:
        log.warning("observer_guard.mark_for_recheck: %s", exc)


async def admin_account_ids(pool, owner_id: int) -> set[int]:
    """Аккаунты, которые сами ведут каналы владельца."""
    try:
        rows = await pool.fetch(
            "SELECT DISTINCT acc_id FROM managed_channels WHERE owner_id=$1 AND acc_id IS NOT NULL",
            owner_id)
        return {int(r["acc_id"]) for r in rows}
    except Exception as exc:
        log.debug("observer_guard.admin_account_ids: %s", exc)
        return set()


async def bot_alive_by_token(token: str) -> bool | None:
    """Жив ли бот — по его же токену (getMe), без аккаунта флота.

    None — не узнали (сеть, флуд). Раньше /start слал аккаунт флота: единственное
    исходящее сообщение во всей проверке.
    """
    if not token:
        return None
    try:
        import aiohttp
        from services import bot_api
        from services.token_vault import decrypt_token
        async with aiohttp.ClientSession() as http:
            state, _data = await bot_api.get_me_state(http, decrypt_token(token))
    except Exception as exc:
        log.debug("observer_guard.bot_alive_by_token: %s", exc)
        return None
    if state == "ok":
        return True
    if state == "revoked":
        return False
    return None
