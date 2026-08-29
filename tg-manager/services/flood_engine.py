"""Flood Intelligence Engine — centralized FloodWait tracking, adaptive pacing,
per-account cooldown management, and operation risk scoring.

Integrates with ``account_flood_log`` and ``flood_intelligence`` tables.
Used by ``op_worker``, ``account_manager``, and any bulk operation handler.

Usage:
    from services.flood_engine import (
        recommended_delay, record_flood, is_account_cooling,
        account_risk_score, action_allowed,
    )

    # Before executing an action
    if is_account_cooling(account_id):
        wait = seconds_until_ready(account_id)
        await asyncio.sleep(wait)

    delay = recommended_delay(account_id, "invite")
    await asyncio.sleep(delay)
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Optional

import asyncpg
from services.logger import log_exc_swallow

log = logging.getLogger(__name__)

# In-memory per-account flood state (supplements DB for hot-path queries)
_flood_state: dict[int, "_AccountFloodState"] = {}
_ACTION_BASELINES: dict[str, float] = {
    "default": 6.0,
    "join": 55.0,
    "leave": 35.0,
    "invite": 90.0,
    "strike": 240.0,  # Strike is the most intensive op — long inter-action baseline
    "message": 12.0,
    "mass_publish": 14.0,
}
_ACTION_MIN_TRUST: dict[str, float] = {
    "invite": 0.50,
    "dm": 0.50,
    "dm_campaign": 0.50,
    "message": 0.50,
    "join": 0.35,
    "create_channel": 0.35,
    "create_bot": 0.35,
    "strike": 0.30,  # Strike is heavy — require at least basic trust to protect accounts
    "mass_publish": 0.30,  # Match critical trust threshold — accounts below 0.3 show warning
    "post": 0.25,
    "parse": 0.20,
    "default": 0.0,
}


@dataclass
class _AccountFloodState:
    """In-memory flood state for a single account.

    Attributes:
        account_id: Telegram account numeric ID.
        consecutive_floods: Number of consecutive FloodWait errors.
        total_floods_24h: Flood events in the last 24 hours.
        last_flood_at: Monotonic timestamp of the last flood.
        cooldown_until: Monotonic timestamp when cooldown expires.
        risk_score: Risk score from 0.0 (safe) to 1.0 (very risky).
        action_delays: Per-action learned delays in seconds.
    """

    account_id: int
    consecutive_floods: int = 0
    total_floods_24h: int = 0
    last_flood_at: float = 0.0
    cooldown_until: float = 0.0
    risk_score: float = 0.0  # 0.0 = safe, 1.0 = very risky
    action_delays: dict[str, float] = field(
        default_factory=dict
    )  # action_type → delay_s


def get_account_state(account_id: int) -> _AccountFloodState:
    """Get or create in-memory flood state for an account.

    Args:
        account_id: Telegram account numeric ID.

    Returns:
        The account's flood state, creating a new one if needed.
    """
    if account_id not in _flood_state:
        _flood_state[account_id] = _AccountFloodState(account_id=account_id)
    return _flood_state[account_id]


# Аккаунты, чьё состояние уже гидрировано из БД в этом процессе — чтобы не читать
# БД повторно на каждый вызов операции.
_hydrated: set[int] = set()


async def hydrate_states(pool, account_ids) -> int:
    """Восстановить in-memory flood-state из ДОЛГОВЕЧНЫХ полей БД после рестарта.

    Проблема (аудит #5): _flood_state держит cooldown_until/consecutive_floods в
    памяти на monotonic-таймере. После редеплоя они обнулялись → «умный темп»
    (накопленная осторожность по прошлым флудам) терялся, и первые действия шли
    базовым темпом. Здесь поднимаем durable-сигналы: остаток cooldown из
    tg_accounts.cooldown_until и число флудов за сутки из account_flood_log.
    Возвращает, сколько аккаунтов гидрировано. Идемпотентно (по _hydrated).
    """
    if not pool or not account_ids:
        return 0
    ids = [int(a) for a in account_ids if int(a) not in _hydrated]
    if not ids:
        return 0
    now_mono = time.monotonic()
    try:
        rows = await pool.fetch(
            "SELECT id, EXTRACT(EPOCH FROM (cooldown_until - NOW())) AS cd_left "
            "FROM tg_accounts WHERE id = ANY($1::bigint[])", ids)
    except Exception as e:
        log_exc_swallow(log, f"flood_engine.hydrate_states cooldown read: {e}")
        rows = []
    for r in rows:
        st = get_account_state(int(r["id"]))
        cd_left = r["cd_left"]
        if cd_left is not None and float(cd_left) > 0:
            # берём строжайший: не занижаем уже стоящий в памяти cooldown
            st.cooldown_until = max(st.cooldown_until, now_mono + float(cd_left))
    try:
        frows = await pool.fetch(
            "SELECT account_id, COUNT(*) AS n FROM account_flood_log "
            "WHERE account_id = ANY($1::bigint[]) AND created_at > NOW() - INTERVAL '24 hours' "
            "GROUP BY account_id", ids)
        for r in frows:
            st = get_account_state(int(r["account_id"]))
            n = int(r["n"] or 0)
            st.total_floods_24h = max(st.total_floods_24h, n)
            # осторожность после рестарта: подтягиваем risk по недавним флудам
            st.risk_score = min(1.0, max(st.risk_score, 0.15 * n))
    except Exception as e:
        log_exc_swallow(log, f"flood_engine.hydrate_states log read: {e}")
    _hydrated.update(ids)
    return len(ids)


def clear_account_cooldown(account_id: int) -> None:
    """Clear in-memory cooldown for one account (call after DB reset).

    Args:
        account_id: Telegram account numeric ID.
    """
    state = get_account_state(account_id)
    state.cooldown_until = 0.0


def clear_all_cooldowns(account_ids: list[int]) -> int:
    """Clear in-memory cooldowns for a list of accounts.

    Args:
        account_ids: List of Telegram account IDs to reset.

    Returns:
        Number of accounts whose cooldowns were actually cleared.
    """
    cleared = 0
    for aid in account_ids:
        if aid in _flood_state and _flood_state[aid].cooldown_until > time.monotonic():
            _flood_state[aid].cooldown_until = 0.0
            cleared += 1
    return cleared


def is_account_cooling(account_id: int) -> bool:
    """Check if an account is currently in cooldown.

    Args:
        account_id: Telegram account numeric ID.

    Returns:
        True if the account is still cooling down from a flood.
    """
    state = get_account_state(account_id)
    return state.cooldown_until > time.monotonic()


def seconds_until_ready(account_id: int) -> float:
    """Get seconds remaining until an account's cooldown expires.

    Args:
        account_id: Telegram account numeric ID.

    Returns:
        Seconds remaining (0.0 if already ready).
    """
    state = get_account_state(account_id)
    remaining = state.cooldown_until - time.monotonic()
    return max(0.0, remaining)


def recommended_delay(account_id: int, action_type: str = "default") -> float:
    """Return recommended delay in seconds before next action for this account.

    Combines baseline delay for the action type with any learned adjustment
    from previous flood events.

    Args:
        account_id: Telegram account numeric ID.
        action_type: Type of action (e.g. 'join', 'invite', 'strike').

    Returns:
        Recommended delay in seconds.
    """
    state = get_account_state(account_id)
    baseline = _ACTION_BASELINES.get(action_type, _ACTION_BASELINES["default"])
    learned = state.action_delays.get(
        action_type,
        state.action_delays.get("default", baseline),
    )
    base = max(baseline, learned)
    cooldown_tail = max(0.0, seconds_until_ready(account_id))
    if cooldown_tail > 0:
        base = max(base, min(cooldown_tail + 15.0, cooldown_tail * 1.15))
    # Затухание штрафа по времени (read-only): record_flood обещает в
    # комментарии "decays over time", но кода не было — risk_score/
    # consecutive_floods убывали ТОЛЬКО через record_success, который пишется
    # лишь для части op-типов. Для остальных штраф держался до рестарта.
    eff_risk, eff_floods = _decayed_flood_penalty(state)
    multiplier = 1.0 + (eff_risk * 1.5)
    if eff_floods >= 2:
        multiplier += min(1.5, eff_floods * 0.25)
    # Глобальный ML-темп: если по флоту растут флуды/баны — замедляем реальные
    # операции, а не только показываем множитель в админке. Для action_type,
    # совпадающего со словарём op_type (напр. "strike"), учитывается точечно,
    # иначе — глобальный тренд. Ошибки движка не должны ломать hot-path.
    try:
        from services.pacing_engine import get_pacing_engine

        pacing_mult = get_pacing_engine().get_multiplier(action_type)
    except Exception:
        log.debug('pacing_engine unavailable, using default')
        pacing_mult = 1.0
    return min(base * multiplier * pacing_mult, 900.0)


# Дневные ориентиры инвайта: консервативный старт для аккаунта без истории и
# жёсткий потолок, выше которого не поднимаемся даже при идеальной статистике.
_INVITE_LIMIT_COLD_START = 15
_INVITE_LIMIT_FLOOR = 5
_INVITE_LIMIT_CEILING = 50


def progressive_cap(age_hours: float | None, trust: float | None = 1.0,
                    ceiling: int = _INVITE_LIMIT_CEILING) -> int:
    """Объём инвайтов на аккаунт по его ВОЗРАСТУ (и доверию), режим «прогрессивно».

    Чистая функция: свежему аккаунту — мало (чтобы не спалить), отстоявшемуся —
    больше, вплоть до потолка. Доверие ниже нормы дополнительно срезает. Это
    середина между сверх-осторожным «Авто» (у свежих ≈2) и ручным фиксом.
    """
    a = age_hours if age_hours is not None else 0.0
    if a < 24:
        base = 5
    elif a < 72:
        base = 12
    elif a < 168:
        base = 25
    else:
        base = ceiling
    t = trust if trust is not None else 1.0
    if t < 0.3:
        base = base // 2
    elif t < 0.5:
        base = int(base * 0.75)
    return max(1, min(base, ceiling))


async def progressive_daily_cap(pool, account_id: int) -> dict:
    """Прогрессивный лимит на аккаунт с вычетом уже сделанного сегодня.

    Возвращает {cap, used_today, remaining}. Ошибка БД → холодный старт.
    """
    try:
        row = await pool.fetchrow(
            """SELECT a.added_at, a.trust_score,
                   COALESCE((SELECT SUM(invites_ok) FROM account_daily_stats
                             WHERE account_id=a.id AND stat_date=CURRENT_DATE), 0) AS today
               FROM tg_accounts a WHERE a.id=$1""",
            int(account_id))
    except Exception:
        return {"cap": _INVITE_LIMIT_COLD_START, "used_today": 0,
                "remaining": _INVITE_LIMIT_COLD_START}
    if not row:
        return {"cap": _INVITE_LIMIT_COLD_START, "used_today": 0,
                "remaining": _INVITE_LIMIT_COLD_START}
    age_hours = None
    if row["added_at"] is not None:
        import datetime as _dt
        try:
            age_hours = (_dt.datetime.now(_dt.timezone.utc)
                         - row["added_at"].replace(tzinfo=_dt.timezone.utc)
                         ).total_seconds() / 3600.0
        except (TypeError, ValueError, AttributeError):
            age_hours = None
    cap = progressive_cap(age_hours, row["trust_score"])
    today = int(row["today"] or 0)
    return {"cap": cap, "used_today": today, "remaining": max(0, cap - today)}


def _phone_country(phone) -> str | None:
    """Страна по коду номера. Отдельная обёртка, чтобы сбой импорта
    account_manager (тяжёлый модуль с telethon) не ронял расчёт риска."""
    try:
        from services.account_manager import country_code_from_phone
        return country_code_from_phone(phone)
    except Exception:
        return None


def _locale_country(lang_code) -> str | None:
    """Страна из локали вида `ru-RU`/`en-US`. Голый `ru` страны не задаёт —
    возвращаем None, чтобы не выдумать рассогласование на пустом месте."""
    raw = (lang_code or "").strip().replace("_", "-")
    if "-" not in raw:
        return None
    tail = raw.split("-")[-1].upper()
    return tail if len(tail) == 2 and tail.isalpha() else None


async def account_risk_factors(pool, account_id: int) -> dict:
    """Свойства самого аккаунта, влияющие на безопасный объём.

    История действий (флуды, конверсия) отвечает на вопрос «как он себя вёл»,
    но не на «насколько он вообще похож на живого человека». Молодой аккаунт с
    пустым профилем ограничат быстрее, чем годовалый с заполненной анкетой, —
    даже при одинаковой истории.

    Считаем ТОЛЬКО по данным, которые в системе реально есть:
      * возраст — из `reg_check_cache.reg_date` (оценка по интерполяции
        Telegram-ID, метод уже используется модулем reg_check);
      * заполненность профиля — есть ли имя и username;
      * зрелость у нас — `warmup_level`;
      * Premium и наличие аватара — `tg_accounts.is_premium/has_photo`, которые
        снимаются при обычной проверке здоровья (schema_v160). NULL здесь = «не
        проверяли»: такой аккаунт НЕ штрафуется, иначе он платил бы за пробел в
        НАШИХ данных;
      * согласованность страны — страна номера (по коду) против страны локали
        аккаунта. Штрафуем не «плохую страну», а РАССОГЛАСОВАНИЕ отпечатка:
        российский номер с en-US на устройстве — это несостыковка, которую видно
        и снаружи.
    Домысливать факторы, которых нет, нельзя: риск по выдуманным данным ведёт к
    уверенным неверным решениям и вреднее, чем не считать вовсе.

    Возвращает {multiplier, notes}: multiplier ≤ 1.0 — во сколько урезать объём,
    notes — список человекочитаемых причин.
    """
    neutral = {"multiplier": 1.0, "notes": []}
    if not pool or not account_id:
        return neutral
    try:
        row = await pool.fetchrow(
            """SELECT a.first_name, a.username, a.warmup_level, a.tg_user_id,
                      a.is_premium, a.has_photo, a.profile_checked_at,
                      a.phone, a.lang_code, a.system_lang_code,
                      r.reg_date
               FROM tg_accounts a
               LEFT JOIN reg_check_cache r
                      ON r.entity_id = a.tg_user_id AND r.entity_type = 'user'
               WHERE a.id = $1""",
            int(account_id),
        )
    except Exception:
        log.debug("account_risk_factors: query failed acc=%s", account_id, exc_info=True)
        return neutral
    if not row:
        return neutral

    mult, notes = 1.0, []

    reg_date = row.get("reg_date")
    if reg_date is not None:
        try:
            import datetime as _dt
            age_days = (_dt.datetime.now(_dt.timezone.utc) - reg_date).days
            if age_days < 14:
                mult *= 0.4
                notes.append(f"аккаунт совсем новый ({age_days} дн.)")
            elif age_days < 60:
                mult *= 0.7
                notes.append(f"молодой аккаунт ({age_days} дн.)")
            elif age_days > 365:
                mult *= 1.15
                notes.append(f"возраст {age_days // 365} г. — доверия больше")
        except Exception:
            pass

    if not (row.get("first_name") or "").strip():
        mult *= 0.6
        notes.append("пустое имя профиля")
    if not (row.get("username") or "").strip():
        mult *= 0.85
        notes.append("нет username")

    try:
        warm = int(row.get("warmup_level") or 0)
        if warm <= 0:
            mult *= 0.7
            notes.append("аккаунт не прогрет")
    except (TypeError, ValueError):
        pass

    # Premium/аватар — только если профиль ДЕЙСТВИТЕЛЬНО снимали. Без отметки
    # времени False неотличим от «не спрашивали», и аккаунт получил бы штраф за
    # то, что мы его не проверили.
    if row.get("profile_checked_at") is not None:
        if row.get("is_premium"):
            mult *= 1.2
            notes.append("Telegram Premium — доверия больше")
        if row.get("has_photo") is False:
            mult *= 0.75
            notes.append("нет аватара")

    # Рассогласование отпечатка: страна номера против страны локали.
    country = _phone_country(row.get("phone"))
    locale_country = _locale_country(row.get("system_lang_code") or row.get("lang_code"))
    if country and locale_country and country != locale_country:
        mult *= 0.8
        notes.append(f"номер {country}, а локаль {locale_country} — отпечаток не сходится")

    return {"multiplier": round(max(0.2, min(mult, 1.3)), 3), "notes": notes}


async def auto_strategy(pool, owner_id: int) -> dict:
    """Автоматический темп по состоянию ВСЕГО флота за сегодня.

    Ручные slow/normal/fast — это три числа, выбранные вслепую: пользователь не
    знает, сколько флудов флот словил за последний час, и не пересматривает
    выбор по ходу. Хуже того, выбор делается на аккаунт, а Telegram смотрит на
    аккаунты как на ГРУППУ: несколько флудов за сутки — сигнал по всему флоту, и
    замедляться должен весь флот, а не только пострадавший.

    Поэтому «авто» считает не по аккаунту (это уже делает
    `recommended_daily_limit`), а по общей картине владельца за сегодня:
      * флот чист и объём набран — можно идти быстрее базового;
      * появились флуды — тормозим, тем сильнее, чем их больше, и режем батч,
        чтобы обрыв стоил меньше целей;
      * много отказов при заметном объёме — гнать бессмысленно и рискованно;
      * данных за сегодня нет — базовый темп, ничего не выдумываем.

    Возвращает {pace_mult, batch_size, reason}. `reason` — человеческий текст:
    автоматика, которая не объясняет решение, неотличима от произвола.
    Ошибка расчёта → нейтральный базовый темп: «авто» не имеет права быть
    опаснее обычного режима.
    """
    neutral = {"pace_mult": 1.0, "batch_size": None,
               "reason": "нет данных за сегодня — базовый темп"}
    if not pool or not owner_id:
        return neutral
    try:
        row = await pool.fetchrow(
            """SELECT COALESCE(SUM(s.invites_ok), 0)   AS ok,
                      COALESCE(SUM(s.actions_fail), 0) AS fails,
                      COALESCE(SUM(s.flood_events), 0) AS floods
               FROM account_daily_stats s
               JOIN tg_accounts a ON a.id = s.account_id
               WHERE a.owner_id = $1 AND s.stat_date = CURRENT_DATE""",
            int(owner_id),
        )
    except Exception:
        log.debug("auto_strategy: query failed owner=%s", owner_id, exc_info=True)
        return neutral
    if not row:
        return neutral

    ok = int(row.get("ok") or 0)
    fails = int(row.get("fails") or 0)
    floods = int(row.get("floods") or 0)
    attempts = ok + fails

    if floods:
        # Каждый флуд по флоту — общий сигнал, а не частный случай пострадавшего.
        mult = min(4.0, 1.0 + 0.8 * floods)
        batch = 1 if floods >= 3 else 3
        return {"pace_mult": round(mult, 2), "batch_size": batch,
                "reason": f"флудов за сегодня: {floods} — замедляемся всем флотом"}

    if attempts >= 10 and ok / attempts < 0.5:
        return {"pace_mult": 1.8, "batch_size": 3,
                "reason": f"конверсия {round(ok / attempts * 100)}% — гнать объём "
                          f"бессмысленно и рискованно"}

    if attempts >= 20 and ok / attempts >= 0.8:
        return {"pace_mult": 0.75, "batch_size": None,
                "reason": f"флот сегодня чист ({ok} успешных, флудов нет) — "
                          f"можно быстрее базового"}

    return neutral


async def recommended_daily_limit(pool, account_id: int) -> dict:
    """Сколько инвайтов в сутки безопасно для ЭТОГО аккаунта — по его истории.

    Раньше лимит задавался вручную одним числом на всю операцию: «по 20 с
    каждого», независимо от того, что один аккаунт год работает без единого
    флуда, а другой словил его вчера. Здесь лимит выводится из фактов
    `account_daily_stats` за последние 7 суток:

      * растим лимит, если аккаунт стабильно отрабатывал БЕЗ флудов;
      * режем, если флуды были — тем сильнее, чем их больше;
      * учитываем долю успеха: много отказов = цель/аккаунт «не заходят»,
        гнать объём бессмысленно и рискованно.

    Возвращает {limit, used_today, remaining, basis} — `basis` объясняет решение
    человеку, чтобы число не выглядело магией. Ошибка БД → консервативный
    холодный старт (лучше недобрать, чем спалить аккаунт).
    """
    cold = {
        "limit": _INVITE_LIMIT_COLD_START,
        "used_today": 0,
        "remaining": _INVITE_LIMIT_COLD_START,
        "basis": "нет истории — консервативный старт",
    }
    if not pool or not account_id:
        return cold
    try:
        row = await pool.fetchrow(
            """SELECT
                   COALESCE(SUM(invites_ok), 0)                        AS inv_ok,
                   COALESCE(SUM(actions_fail), 0)                      AS fails,
                   COALESCE(SUM(flood_events), 0)                      AS floods,
                   COUNT(*) FILTER (WHERE invites_ok > 0)              AS active_days,
                   COALESCE(MAX(invites_ok), 0)                        AS best_day,
                   COALESCE(SUM(invites_ok) FILTER (
                       WHERE stat_date = CURRENT_DATE), 0)             AS today
               FROM account_daily_stats
               WHERE account_id = $1
                 AND stat_date >= CURRENT_DATE - INTERVAL '7 days'""",
            int(account_id),
        )
    except Exception:
        log.debug("recommended_daily_limit: query failed acc=%s", account_id, exc_info=True)
        return cold
    if not row or not int(row["active_days"] or 0):
        return cold

    inv_ok = int(row["inv_ok"] or 0)
    fails = int(row["fails"] or 0)
    floods = int(row["floods"] or 0)
    best_day = int(row["best_day"] or 0)
    today = int(row["today"] or 0)

    # База — лучший спокойный день, но не ниже холодного старта.
    limit = max(_INVITE_LIMIT_COLD_START, best_day)
    if floods == 0:
        # Чистая неделя — осторожный рост (+30%), а не рывок.
        limit = int(limit * 1.3)
        basis = f"7 дней без флудов, лучший день {best_day}"
    else:
        # Каждый флуд срезает треть; два и больше — половину и ниже.
        limit = int(limit / (1.0 + 0.5 * floods))
        basis = f"флудов за неделю: {floods} — лимит снижен"

    total = inv_ok + fails
    if total >= 10:
        success = inv_ok / total
        if success < 0.5:
            limit = int(limit * 0.6)
            basis += f"; доля успеха {success:.0%} — объём урезан"

    # Свойства самого аккаунта (возраст/профиль/прогрев) — поверх истории:
    # история говорит «как он себя вёл», факторы — «насколько он вообще похож
    # на живого». Молодой аккаунт с пустым профилем ограничат быстрее даже при
    # идентичной истории.
    factors = await account_risk_factors(pool, account_id)
    if factors.get("notes"):
        limit = int(limit * float(factors.get("multiplier") or 1.0))
        basis += "; " + ", ".join(factors["notes"])

    limit = max(_INVITE_LIMIT_FLOOR, min(limit, _INVITE_LIMIT_CEILING))
    return {
        "limit": limit,
        "used_today": today,
        "remaining": max(0, limit - today),
        "basis": basis,
        "factors": factors,
    }


def gaussian_delay(
    mean_seconds: float,
    *,
    spread: float = 0.18,
    minimum: float = 0.5,
    maximum: float | None = None,
) -> float:
    """Return a bounded Gaussian delay for conservative load smoothing."""
    stddev = max(mean_seconds * spread, 0.05)
    sampled = random.gauss(mean_seconds, stddev)
    bounded = max(sampled, minimum)
    if maximum is not None:
        bounded = min(bounded, maximum)
    return bounded


def normalize_trust_score(value: object) -> float:
    """Normalize DB trust values to the canonical 0..1 range."""
    try:
        score = float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if score > 1.0:
        score = score / 100.0
    return max(0.0, min(1.0, score))


def min_trust_for_action(action_type: str = "default") -> float:
    return _ACTION_MIN_TRUST.get(action_type, _ACTION_MIN_TRUST["default"])


def account_rank_score(account_id: int, trust_score: object) -> float:
    """Lower is better: in-memory risk minus normalized trust weight."""
    return get_account_state(account_id).risk_score - normalize_trust_score(trust_score)


def _decayed_flood_penalty(state) -> tuple[float, float]:
    """Эффективные (risk_score, consecutive_floods) с затуханием по времени.

    Штраф после флуда убывал только через record_success, который вызывается
    лишь для части op-типов — иначе аккаунт держал завышенную задержку до
    рестарта. Здесь применяем time-decay ко времени с последнего флуда:
    risk_score — полураспад ~30 мин, consecutive_floods — −1 за каждые 15 мин.
    Read-only: состояние не мутируем (жёсткий cooldown_until не трогаем, он
    истекает сам). last_flood_at — monotonic (см. record_flood)."""
    risk = state.risk_score
    floods = state.consecutive_floods
    if state.last_flood_at > 0 and (risk > 0 or floods > 0):
        age = time.monotonic() - state.last_flood_at
        if age > 0:
            import math

            risk = risk * math.pow(0.5, age / 1800.0)
            floods = max(0, floods - int(age // 900))
    return risk, floods


def _recency_penalty(last_used: object, now_ts: float, *, max_penalty: float = 0.08,
                     half_life_s: float = 1800.0) -> float:
    """Мягкий штраф за недавнее использование (для балансировки нагрузки).

    Никогда не использованный аккаунт → 0 (предпочитаем его). Только что
    использованный → ~max_penalty, штраф экспоненциально спадает за ~half_life.
    Величина мала относительно разницы trust/risk — сдвигает выбор только среди
    почти равных аккаунтов, не жертвуя безопасностью ради ротации."""
    if not last_used:
        return 0.0
    try:
        ts = last_used.timestamp()
    except Exception:
        return 0.0
    age = now_ts - ts
    if age <= 0:
        return max_penalty
    import math

    return max_penalty * math.exp(-age / half_life_s)


async def record_flood(
    pool: Optional[asyncpg.Pool],
    account_id: int,
    wait_seconds: int,
    action_type: str = "default",
    operation_id: Optional[int] = None,
) -> float:
    """Record a FloodWait event. Returns the actual cooldown seconds applied."""
    # Метрика (аудит №6): всплеск флудов — самый ранний признак, что флот
    # перегрет. Раньше это было видно только вычитыванием логов постфактум.
    try:
        from services import metrics as _m
        _m.inc("infragram_flood_events_total", {"kind": "flood_wait",
                                                "action": str(action_type)})
    except Exception:
        pass
    state = get_account_state(account_id)
    now = time.monotonic()

    state.consecutive_floods += 1
    state.total_floods_24h += 1
    state.last_flood_at = now

    # Exponential backoff: base wait + consecutive penalty
    penalty = min(state.consecutive_floods * 30, 300)  # up to +5 min penalty
    actual_wait = wait_seconds + penalty
    state.cooldown_until = now + actual_wait

    # Update risk score (increases with floods, decays over time)
    state.risk_score = min(1.0, state.risk_score + 0.2 * state.consecutive_floods)

    # Increase action-specific delay
    current_delay = state.action_delays.get(action_type, 1.0)
    state.action_delays[action_type] = min(current_delay * 1.5, 60.0)  # max 60s delay

    log.warning(
        "FloodWait acc=%d action=%s wait=%ds consecutive=%d cooldown=%.0fs risk=%.2f",
        account_id,
        action_type,
        wait_seconds,
        state.consecutive_floods,
        actual_wait,
        state.risk_score,
    )

    # Persist to DB (non-blocking; skipped when pool is None, e.g. from account_manager)
    if pool is not None:
        # Критично: кулдаун применяем ПЕРВЫМ и отдельно — он не должен пропасть
        # из-за сбоя записи в аналитический лог (иначе аккаунт продолжит работать
        # во флуд → риск бана). Раньше INSERT в лог и этот UPDATE были в одном try.
        try:
            await pool.execute(
                """UPDATE tg_accounts
                   SET cooldown_until = NOW() + ($1 * INTERVAL '1 second'),
                       last_flood_at = NOW(),
                       acc_status = CASE
                           WHEN COALESCE(acc_status, 'active') IN ('spamblock', 'banned', 'deactivated')
                               THEN acc_status
                           ELSE 'cooldown'
                       END,
                       status_reason = $3
                   WHERE id = $2""",
                actual_wait,
                account_id,
                f"{action_type} cooldown after FloodWait ({wait_seconds}s)",
            )
        except Exception as e:
            log.warning("flood_engine cooldown update failed: %s", e)
        # Аналитический лог С ПРИВЯЗКОЙ К ОПЕРАЦИИ. operation_id раньше принимался
        # функцией, но НЕ писался — из-за этого нельзя было показать «что операция
        # сделала с аккаунтами». Фолбэк без v41-колонок, чтобы не падать на
        # неотмигрированной БД (лог не критичен).
        try:
            await pool.execute(
                """INSERT INTO account_flood_log(account_id, flood_seconds, action_type,
                                                 operation_id, actual_wait, consecutive_count)
                   VALUES ($1, $2, $3, $4, $5, $6)""",
                account_id, wait_seconds, action_type,
                operation_id, int(actual_wait), state.consecutive_floods,
            )
        except Exception:
            try:
                await pool.execute(
                    """INSERT INTO account_flood_log(account_id, flood_seconds, action_type)
                       VALUES ($1, $2, $3)""",
                    account_id, wait_seconds, action_type,
                )
            except Exception as e:
                log.debug("flood_engine log insert failed: %s", e)

        # Physics Engine telemetry (fire-and-forget, но с удержанием ссылки — класс 14)
        try:
            from services import physics_engine as _pe
            from services.bg_tasks import spawn
            spawn(
                _pe.record_telemetry(pool, account_id, None, action_type, "flood_wait", wait_seconds, 0)
            )
        except Exception as e:
            log_exc_swallow(log, "record_flood: import")

    return actual_wait


async def apply_post_action_cooldown(
    pool: Optional[asyncpg.Pool],
    account_id: int,
    cooldown_seconds: int,
    action_type: str = "default",
) -> None:
    """Apply a protective cooldown after a heavy but SUCCESSFUL action (e.g. a strike).

    Unlike record_flood this does NOT inflate flood/risk counters — it just
    parks the account so it can't be re-used for another heavy op too soon.
    This is the per-account rate-limit (Survival Contract §4/§6): a strike does
    100+ write actions, so the same account must rest before the next one.
    """
    now = time.monotonic()
    state = get_account_state(account_id)
    # Only extend the cooldown, never shorten an existing (e.g. flood) cooldown.
    state.cooldown_until = max(state.cooldown_until, now + cooldown_seconds)
    if pool is not None:
        try:
            await pool.execute(
                """UPDATE tg_accounts
                   SET cooldown_until = GREATEST(
                           COALESCE(cooldown_until, NOW()),
                           NOW() + ($1 * INTERVAL '1 second')
                       ),
                       acc_status = CASE
                           WHEN COALESCE(acc_status, 'active')
                               IN ('spamblock', 'banned', 'deactivated') THEN acc_status
                           ELSE 'cooldown'
                       END,
                       status_reason = $3
                   WHERE id = $2""",
                cooldown_seconds,
                account_id,
                f"rest after {action_type}",
            )
        except Exception as e:
            log.warning("flood_engine apply_post_action_cooldown DB write failed: %s", e)


async def record_success(account_id: int, action_type: str = "default") -> None:
    """Record a successful action — gradually reduce risk score and action delay."""
    state = get_account_state(account_id)
    # Decay risk score on success
    state.risk_score = max(0.0, state.risk_score - 0.05)
    state.consecutive_floods = max(0, state.consecutive_floods - 1)

    # Reduce action delay slightly
    if action_type in state.action_delays:
        state.action_delays[action_type] = max(
            state.action_delays[action_type] * 0.9, 0.5
        )


async def record_peer_flood(
    pool: Optional[asyncpg.Pool],
    account_id: int,
    action_type: str = "default",
    operation_id: Optional[int] = None,
    cooldown_seconds: int = 48 * 3600,
) -> float:
    """Isolate outbound workflows for an account after a peer flood penalty."""
    try:
        from services import metrics as _m
        _m.inc("infragram_flood_events_total", {"kind": "peer_flood",
                                                "action": str(action_type)})
    except Exception:
        pass
    applied = await record_flood(
        pool=pool,
        account_id=account_id,
        wait_seconds=cooldown_seconds,
        action_type=action_type,
        operation_id=operation_id,
    )
    if pool is not None:
        try:
            await pool.execute(
                """UPDATE tg_accounts
                   SET acc_status = 'spamblock',
                       status_reason = $2
                   WHERE id = $1""",
                account_id,
                f"{action_type} blocked after PeerFlood; outbound workflows paused for 48h",
            )
        except Exception as e:
            log.warning("peer_flood DB write failed: %s", e)
    return applied


async def get_best_account(
    pool: asyncpg.Pool,
    owner_id: int,
    action_type: str = "default",
    exclude_ids: list[int] | None = None,
    pool_name: str | None = None,
    tags: list[str] | None = None,
    min_trust_score: float | None = None,
) -> dict | None:
    """Select the best available account for an action, considering flood state and risk.

    Optional filters:
      pool_name — restrict to accounts in this pool
      tags      — restrict to accounts having ALL of these tags
    """
    exclude = exclude_ids or []

    conditions = [
        "a.owner_id = $1",
        "a.is_active = TRUE",
        "a.session_str IS NOT NULL",
        "(a.cooldown_until IS NULL OR a.cooldown_until < NOW())",
        # Мёртвые по статусу не годятся для действия. Раньше исполнители несли
        # этот фильтр каждый у себя (NOT IN banned/deactivated/session_expired);
        # переносим его в умный слой, чтобы «одна дверь» была строго безопаснее
        # сырого выбора, а не мягче.
        "COALESCE(a.acc_status, 'active') NOT IN ('banned', 'deactivated', 'session_expired')",
        "a.id != ALL($2::bigint[])",
    ]
    params: list = [owner_id, exclude]

    if pool_name is not None:
        params.append(pool_name)
        conditions.append(f"a.pool = ${len(params)}")

    if tags:
        params.append(tags)
        conditions.append(f"a.tags @> ${len(params)}::text[]")

    min_trust = (
        min_trust_for_action(action_type)
        if min_trust_score is None
        else min_trust_score
    )
    if min_trust > 0:
        params.append(min_trust)
        conditions.append(f"COALESCE(a.trust_score, 0) >= ${len(params)}")

    where = " AND ".join(conditions)
    rows = await pool.fetch(
        f"""SELECT a.id, a.owner_id, a.session_str, a.first_name, a.phone,
                   a.device_model, a.system_version, a.app_version,
                   a.lang_code, a.system_lang_code, a.proxy_id, a.cf_relay_url,
                   a.trust_score, a.cooldown_until, a.tags, a.pool, a.last_used,
                   p.proxy_url, p.geo_country,
                   r.ban_probability AS physics_ban_probability
            FROM tg_accounts a
            LEFT JOIN user_proxies p ON p.id = a.proxy_id AND p.is_active = TRUE
            LEFT JOIN account_risk_scores r ON r.account_id = a.id
            WHERE {where}
            ORDER BY a.trust_score DESC NULLS LAST, a.last_used ASC NULLS FIRST
            LIMIT 10""",
        *params,
    )
    if not rows:
        return None

    # From DB candidates, pick the one with lowest combined risk: in-memory
    # flood-engine risk (this-process, reactive) blended with Physics
    # Engine's persisted ban_probability (cross-restart, 7-day-history-based).
    # Previously only the in-memory signal was consulted here, so Physics
    # Engine's risk scoring never actually influenced which account got
    # picked for an operation — it was purely a dashboard number.
    candidates = [r for r in rows if not is_account_cooling(r["id"])] or list(rows)
    safe = [r for r in candidates if (r["physics_ban_probability"] or 0.0) < 0.85]
    pool_rows = safe or candidates  # if everything is high-risk, still return the least-bad one

    # Load balancing (Account Rotation 2C): среди почти равных по риску аккаунтов
    # предпочитаем наименее недавно использованный. Без этого при ≤10 кандидатах
    # финальный выбор игнорировал last_used и один и тот же аккаунт брался снова
    # и снова → неравномерный износ и повышенный риск бана «любимого» аккаунта.
    now_ts = time.time()
    best = None
    best_score = float("inf")
    for row in pool_rows:
        combined = account_rank_score(row["id"], row["trust_score"])
        combined += float(row["physics_ban_probability"] or 0.0) * 1.5
        combined += _recency_penalty(row.get("last_used"), now_ts)
        if combined < best_score:
            best_score = combined
            best = dict(row)

    best = best or dict(rows[0])

    # Замкнуть петлю ротации: пометить выбранный аккаунт использованным, чтобы
    # last_used ASC в следующем выборе реально сдвигался. Best-effort — сбой
    # записи не должен ломать выбор аккаунта.
    try:
        await pool.execute(
            "UPDATE tg_accounts SET last_used=now() WHERE id=$1", best["id"]
        )
    except Exception:
        log_exc_swallow(log, "get_best_account: last_used update acc=%s", best.get("id"))

    return best


async def get_active_accounts(
    pool: asyncpg.Pool,
    owner_id: int,
    account_ids: list[int] | None = None,
    pool_name: str | None = None,
    tags: list[str] | None = None,
    action_type: str = "default",
    min_trust_score: float | None = None,
) -> list[dict]:
    """Return all active, non-cooling accounts ranked by combined trust/risk score.

    For mass operations that need to cycle through multiple accounts.
    Optional filters: account_ids (restrict to subset), pool_name, tags.
    """
    conditions = [
        "a.owner_id = $1",
        "a.is_active = TRUE",
        "a.session_str IS NOT NULL",
        "(a.cooldown_until IS NULL OR a.cooldown_until < NOW())",
    ]
    params: list = [owner_id]

    if account_ids:
        params.append(account_ids)
        conditions.append(f"a.id = ANY(${len(params)}::bigint[])")

    if pool_name is not None:
        params.append(pool_name)
        conditions.append(f"a.pool = ${len(params)}")

    if tags:
        params.append(tags)
        conditions.append(f"a.tags @> ${len(params)}::text[]")

    min_trust = (
        min_trust_for_action(action_type)
        if min_trust_score is None
        else min_trust_score
    )
    if min_trust > 0:
        params.append(min_trust)
        conditions.append(f"COALESCE(a.trust_score, 0) >= ${len(params)}")

    where = " AND ".join(conditions)
    rows = await pool.fetch(
        f"""SELECT a.id, a.session_str, a.first_name, a.phone,
                   a.device_model, a.system_version, a.app_version,
                   a.lang_code, a.system_lang_code, a.proxy_id,
                   a.trust_score, a.cooldown_until, a.tags, a.pool,
                   p.proxy_url, p.geo_country,
                   r.ban_probability AS physics_ban_probability
            FROM tg_accounts a
            LEFT JOIN user_proxies p ON p.id = a.proxy_id AND p.is_active = TRUE
            LEFT JOIN account_risk_scores r ON r.account_id = a.id
            WHERE {where}
            ORDER BY a.trust_score DESC NULLS LAST, a.last_used ASC NULLS FIRST""",
        *params,
    )

    # Exclude in-memory cooling accounts, then re-sort by combined score —
    # in-memory flood-engine risk blended with Physics Engine's persisted
    # ban_probability (see get_best_account for why this join was added).
    result = [dict(r) for r in rows if not is_account_cooling(r["id"])]
    result.sort(
        key=lambda r: account_rank_score(r["id"], r.get("trust_score"))
        + float(r.get("physics_ban_probability") or 0.0) * 1.5
    )
    return result


async def wait_if_cooling(account_id: int, action_type: str = "default") -> None:
    """Async wait if account is in cooldown, then apply recommended delay."""
    cool_secs = seconds_until_ready(account_id)
    if cool_secs > 0:
        log.info(
            "flood_engine: acc=%d cooling %.0fs for %s",
            account_id,
            cool_secs,
            action_type,
        )
        await asyncio.sleep(min(cool_secs, 300))  # cap at 5 min wait

    delay = recommended_delay(account_id, action_type)
    if delay > 0.1:
        await asyncio.sleep(
            gaussian_delay(delay, spread=0.12, minimum=0.25, maximum=delay * 1.35)
        )


async def load_state_from_db(pool: asyncpg.Pool, owner_id: int) -> None:
    """Load flood state from DB on startup (for recovery after restart)."""
    rows = await pool.fetch(
        """SELECT a.id, a.trust_score,
                  EXTRACT(EPOCH FROM (a.cooldown_until - NOW())) AS cooldown_remaining,
                  COUNT(fl.id) FILTER (WHERE fl.created_at > NOW() - INTERVAL '24h') AS floods_24h
           FROM tg_accounts a
           LEFT JOIN account_flood_log fl ON fl.account_id = a.id
           WHERE a.owner_id = $1
           GROUP BY a.id, a.trust_score, a.cooldown_until""",
        owner_id,
    )
    now = time.monotonic()
    for row in rows:
        state = get_account_state(row["id"])
        state.total_floods_24h = row["floods_24h"] or 0
        remaining = row["cooldown_remaining"] or 0
        if remaining > 0:
            state.cooldown_until = now + remaining
        # Estimate risk from 24h flood count
        state.risk_score = min(1.0, (state.total_floods_24h * 0.1))
    log.info(
        "flood_engine: loaded state for %d accounts (owner=%d)", len(rows), owner_id
    )


def get_risk_summary(account_ids: list[int]) -> dict[int, dict]:
    """Return risk summary for a list of accounts."""
    result = {}
    for acc_id in account_ids:
        state = get_account_state(acc_id)
        result[acc_id] = {
            "risk_score": round(state.risk_score, 2),
            "consecutive_floods": state.consecutive_floods,
            "total_floods_24h": state.total_floods_24h,
            "is_cooling": is_account_cooling(acc_id),
            "seconds_until_ready": round(seconds_until_ready(acc_id), 0),
        }
    return result
