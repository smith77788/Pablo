"""Anti-Storm — защита флота от волны банов (внутренний шторм модерации).

Губернатор (`fleet_governor`) плавно замедляет темп по ДАВЛЕНИЮ флота. Но
давление — величина инерционная: оно растёт постепенно. Внезапную ВОЛНУ банов
(Telegram прокатил чистку по целому классу аккаунтов за минуты) оно замечает
поздно — когда флот уже выкосило.

Anti-Storm — быстрый событийный предохранитель поверх губернатора: он срабатывает
ровно в момент банов (хук `_on_account_banned`), считает, сколько РАЗНЫХ
аккаунтов получили критическое ограничение за короткое окно, и при всплеске
переводит весь флот в «глубокий сон» — резкий клэмп темпа на фиксированное время.
Логика: лучше переждать волну, имитируя массовое отключение интернета, чем
продолжать жечь аккаунты в шторм.

Здесь только чистый классификатор шторма (его и проверяют тесты); чтение флота и
арминг состояния — тонкие обёртки поверх organism_state и restriction_events.

Осознанные рамки, чтобы не выключать флот зря:
* шторм объявляется по числу РАЗНЫХ пострадавших аккаунтов, а не событий (один
  аккаунт с пятью ограничениями — не волна);
* на маленьком флоте (<10) срабатывает только абсолютный порог, не доля —
  иначе два бана из трёх ложно читались бы как «10%+ шторм»;
* сон держится фиксированное окно после ПОСЛЕДНЕГО события и сам истекает —
  никакого ручного снятия, чтобы забытый флаг не заморозил флот навсегда.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

# Окно наблюдения за волной (минуты): сколько назад смотрим на критические баны.
WINDOW_MIN = 30
# Пороги по числу РАЗНЫХ пострадавших аккаунтов в окне.
WATCH_ABS = 3           # настороже: темп вдвое
STORM_ABS = 5           # шторм: глубокий сон
# Либо доля флота (только если флот достаточно большой — см. MIN_FLEET_FOR_SHARE).
STORM_SHARE = 0.10
MIN_FLEET_FOR_SHARE = 10
# Множители темпа.
WATCH_MULT = 2.0
DEEP_SLEEP_MULT = 12.0
# Сколько держать «сон» после последнего критического события (минуты).
HOLD_MIN = 30

_STATE_KEY = "anti_storm"

CALM = "calm"
WATCH = "watch"
STORM = "storm"


def _now(now: datetime | None = None) -> datetime:
    return now or datetime.now(timezone.utc)


def _aware(dt) -> datetime | None:
    """Привести к timezone-aware datetime. Принимает и datetime, и ISO-строку
    (в organism_state время лежит строкой — build_state пишет .isoformat())."""
    if dt is None:
        return None
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt)
        except (ValueError, TypeError):
            return None
    if not isinstance(dt, datetime):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def classify(distinct_hit: int, fleet_size: int) -> dict:
    """Число РАЗНЫХ пострадавших аккаунтов за окно + размер флота → уровень.

    Возвращает {"level", "mult"}. Чистая функция — сердце решения.
    """
    hit = max(0, int(distinct_hit or 0))
    fleet = max(0, int(fleet_size or 0))
    share = (hit / fleet) if fleet else 0.0

    storm = hit >= STORM_ABS or (fleet >= MIN_FLEET_FOR_SHARE and share >= STORM_SHARE)
    if storm:
        return {"level": STORM, "mult": DEEP_SLEEP_MULT}
    if hit >= WATCH_ABS:
        return {"level": WATCH, "mult": WATCH_MULT}
    return {"level": CALM, "mult": 1.0}


def active_multiplier(state: dict | None, *, now: datetime | None = None) -> float:
    """Множитель темпа из АРМИРОВАННОГО состояния, если оно ещё активно.

    Истёкшее (now >= until) состояние = сон закончился → 1.0. Так забытый флаг
    не морозит флот: срок истекает сам.
    """
    if not state:
        return 1.0
    until = _aware(state.get("until"))
    if until is None or _now(now) >= until:
        return 1.0
    try:
        return max(1.0, float(state.get("mult") or 1.0))
    except (TypeError, ValueError):
        return 1.0


def build_state(decision: dict, *, now: datetime | None = None) -> dict | None:
    """Решение классификатора → состояние для записи, либо None если штиль
    (штиль не арминга требует, а снятия — это делает вызывающий)."""
    if decision.get("level") == CALM:
        return None
    now = _now(now)
    return {
        "level": decision["level"],
        "mult": float(decision["mult"]),
        "detected_at": now.isoformat(),
        "until": (now + timedelta(minutes=HOLD_MIN)).isoformat(),
    }


# ── Обёртки над БД (тонкие; логика — выше) ─────────────────────────────────

async def _distinct_hit(pool, owner_id: int) -> int:
    """Сколько РАЗНЫХ аккаунтов владельца получили критическое ограничение за
    окно. Критическое = severity='critical' или бан/блок/деактивация по типу."""
    try:
        val = await pool.fetchval(
            "SELECT COUNT(DISTINCT account_id) FROM restriction_events "
            "WHERE owner_id=$1 AND account_id IS NOT NULL "
            "AND created_at > now() - ($2 || ' minutes')::interval "
            "AND (severity='critical' OR event_type ILIKE ANY($3::text[]))",
            owner_id, str(WINDOW_MIN),
            ["%ban%", "%block%", "%deactiv%", "%account_restricted%"])
        return int(val or 0)
    except Exception:
        log.debug("anti_storm: distinct_hit failed owner=%s", owner_id, exc_info=True)
        return 0


async def _fleet_size(pool, owner_id: int) -> int:
    try:
        val = await pool.fetchval(
            "SELECT COUNT(*) FROM tg_accounts WHERE owner_id=$1 AND is_active=TRUE",
            owner_id)
        return int(val or 0)
    except Exception:
        return 0


async def current(pool, owner_id: int) -> dict:
    """Текущее состояние шторма для UI/губернатора: {level, mult, until}.
    Истёкшее возвращается как calm."""
    from services.organism import spine
    try:
        st = await spine.state_get(pool, owner_id, _STATE_KEY, None)
    except Exception:
        st = None
    mult = active_multiplier(st)
    if mult <= 1.0:
        return {"level": CALM, "mult": 1.0, "until": None}
    return {"level": st.get("level"), "mult": mult, "until": st.get("until")}


async def check_and_arm(pool, owner_id: int, *, bot=None) -> dict:
    """Оценить волну и, если шторм/настороже, армировать состояние. Возвращает
    актуальное {level, mult}. Fail-open — сбой не мешает вызывающему.

    Вызывается из хука бана: срабатывает ровно тогда, когда баны идут.
    """
    if not owner_id:
        return {"level": CALM, "mult": 1.0}
    hit = await _distinct_hit(pool, owner_id)
    fleet = await _fleet_size(pool, owner_id)
    decision = classify(hit, fleet)
    if decision["level"] == CALM:
        return decision
    state = build_state(decision)
    from services.organism import spine
    # Был ли шторм уже активен ДО этого события — чтобы не слать владельцу
    # «волна банов» на каждый бан в течение уже объявленного шторма.
    storm_was_active = False
    try:
        # Не понижаем уже армированный шторм до watch и не сбрасываем срок назад:
        # берём более сильный уровень и более поздний until.
        prev = await spine.state_get(pool, owner_id, _STATE_KEY, None)
        if prev and active_multiplier(prev) > 1.0:
            storm_was_active = prev.get("level") == STORM
        if prev and active_multiplier(prev) >= state["mult"]:
            prev_until = _aware(prev.get("until"))
            new_until = _aware(state.get("until"))
            if prev_until and new_until and prev_until > new_until:
                state["until"] = prev["until"]
                state["mult"] = max(state["mult"], float(prev.get("mult") or 1.0))
                state["level"] = prev.get("level", state["level"])
        await spine.state_set(pool, owner_id, _STATE_KEY, state)
        await spine.emit(pool, owner_id, "anti_storm_" + decision["level"],
                         {"hit": hit, "fleet": fleet, "mult": decision["mult"]})
    except Exception:
        log.debug("anti_storm: arm failed owner=%s", owner_id, exc_info=True)
    # Сбросить кэш губернатора, чтобы клэмп применился сейчас, а не через TTL.
    try:
        from services import fleet_governor
        fleet_governor.invalidate(owner_id)
    except Exception:
        pass
    if bot is not None and decision["level"] == STORM and not storm_was_active:
        try:
            await bot.send_message(
                owner_id,
                "🌩 <b>Anti-Storm: волна банов</b>\n"
                f"За {WINDOW_MIN} мин критические ограничения получили {hit} "
                f"аккаунтов. Флот переведён в глубокий сон (темп ×{int(DEEP_SLEEP_MULT)}) "
                f"на {HOLD_MIN} мин — переждём чистку, чтобы не жечь остальные.",
                parse_mode="HTML")
        except Exception:
            log.debug("anti_storm: notify failed owner=%s", owner_id)
    return decision
