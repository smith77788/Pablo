"""Защита бота от накрутки/ботов (per-bot, ВЫКЛючена по умолчанию).

Идея: детект строится на СКОРОСТИ (резкий всплеск новых подписчиков), а не на
распознавании «человек/бот» по контенту — всплеск подделать нельзя, а контент
можно. Изоляция подозрительных — структурная и в единственной точке (аудитория
рассылок через флаг suspect). Флаг выставляется ТОЛЬКО когда защита включена,
поэтому у тех, кто использует накрутку намеренно (SEO), ничего не меняется.

Режимы (bot_flood_config.mode):
  off     — ничего (поведение как раньше);
  detect  — только наблюдение + агрегированный алерт;
  protect — + пометить подписчиков всплеска suspect (вон из аудитории/статистики);
  block   — + не давать подозрительным никакой «выгоды» (автоответ/воронка/ИИ/
            зачёт диплинка) — атакующий не получает ничего.

Детектор in-memory (скорость), эпизоды и флаги — в БД (durability/аналитика/очистка).
"""
from __future__ import annotations

import logging
import time
from collections import deque

log = logging.getLogger(__name__)

# ── Детектор всплеска (in-memory, process-local) ──────────────────────────────
WINDOW_SEC = 60.0  # окно измерения скорости (новых подписчиков за минуту)
# Скользящее окно временных меток новых подписчиков по боту. Process-local,
# best-effort: потеря при рестарте безвредна (эпизод хранится в БД, окно
# наполнится заново за минуту).
_windows: dict[int, deque] = {}
# Кэш конфига по боту, чтобы не читать БД на каждое сообщение под флудом.
_config_cache: dict[int, tuple[dict, float]] = {}
_CONFIG_TTL = 30.0

_VALID_MODES = ("off", "detect", "protect", "block")


def record_new_user(bot_id: int, now: float | None = None) -> int:
    """Регистрирует нового подписчика и возвращает их число за окно (≈в минуту).
    Эффект — только in-memory счётчик процесса."""
    t = now if now is not None else time.monotonic()
    dq = _windows.get(bot_id)
    if dq is None:
        dq = deque()
        _windows[bot_id] = dq
    dq.append(t)
    cutoff = t - WINDOW_SEC
    while dq and dq[0] < cutoff:
        dq.popleft()
    return len(dq)


def is_attack(rate_per_min: int, threshold: int) -> bool:
    """Всплеск ли это. ЧИСТАЯ. threshold<=0 → защита неактивна (не всплеск)."""
    try:
        thr = int(threshold)
    except (TypeError, ValueError):
        return False
    return thr > 0 and int(rate_per_min or 0) >= thr


def reset_window(bot_id: int) -> None:
    """Сброс окна (для тестов/после снятия защиты)."""
    _windows.pop(bot_id, None)


# ── Конфиг (БД + кэш) ─────────────────────────────────────────────────────────
_DEFAULT_CONFIG = {"mode": "off", "threshold_per_min": 30}


async def get_config(pool, bot_id: int) -> dict:
    """Конфиг защиты бота (с кэшем). Нет строки → off."""
    now = time.monotonic()
    cached = _config_cache.get(bot_id)
    if cached and now - cached[1] < _CONFIG_TTL:
        return cached[0]
    try:
        r = await pool.fetchrow(
            "SELECT mode, threshold_per_min FROM bot_flood_config WHERE bot_id=$1",
            bot_id)
    except Exception:
        log.debug("flood_guard.get_config: read failed bot=%s", bot_id)
        return dict(_DEFAULT_CONFIG)
    cfg = (dict(_DEFAULT_CONFIG) if not r
           else {"mode": r["mode"] or "off",
                 "threshold_per_min": int(r["threshold_per_min"] or 30)})
    _config_cache[bot_id] = (cfg, now)
    return cfg


async def set_config(pool, bot_id: int, owner_id: int, *, mode: str | None = None,
                     threshold_per_min: int | None = None) -> dict:
    """Создаёт/обновляет конфиг защиты. Инвалидирует кэш."""
    cur = await pool.fetchrow(
        "SELECT mode, threshold_per_min FROM bot_flood_config WHERE bot_id=$1", bot_id)
    new_mode = (mode if mode in _VALID_MODES else
                (cur["mode"] if cur else _DEFAULT_CONFIG["mode"]))
    if threshold_per_min is None:
        new_thr = int(cur["threshold_per_min"]) if cur else _DEFAULT_CONFIG["threshold_per_min"]
    else:
        try:
            new_thr = max(1, int(threshold_per_min))
        except (TypeError, ValueError):
            new_thr = _DEFAULT_CONFIG["threshold_per_min"]
    r = await pool.fetchrow(
        """INSERT INTO bot_flood_config(bot_id, owner_id, mode, threshold_per_min)
           VALUES($1,$2,$3,$4)
           ON CONFLICT (bot_id) DO UPDATE
             SET mode=EXCLUDED.mode, threshold_per_min=EXCLUDED.threshold_per_min,
                 updated_at=now()
           RETURNING mode, threshold_per_min""",
        bot_id, owner_id, new_mode, new_thr)
    _config_cache.pop(bot_id, None)
    if new_mode == "off":
        reset_window(bot_id)
    return {"mode": r["mode"], "threshold_per_min": int(r["threshold_per_min"])}


# ── Эпизоды атак ──────────────────────────────────────────────────────────────
_EPISODE_COOLOFF_SEC = 300  # эпизод «живёт», пока всплески идут не реже, чем раз в 5 мин


async def open_or_touch_episode(pool, bot_id: int, owner_id: int, mode: str,
                                rate: int) -> int:
    """Открывает новый эпизод атаки или продлевает активный. Возвращает его id."""
    r = await pool.fetchrow(
        """SELECT id, peak_per_min FROM bot_flood_events
           WHERE bot_id=$1 AND status='active'
             AND last_at > now() - ($2 || ' seconds')::interval
           ORDER BY id DESC LIMIT 1""",
        bot_id, str(_EPISODE_COOLOFF_SEC))
    if r:
        await pool.execute(
            "UPDATE bot_flood_events SET last_at=now(), peak_per_min=GREATEST(peak_per_min,$2) "
            "WHERE id=$1", r["id"], int(rate))
        return int(r["id"])
    # закрыть возможные протухшие активные эпизоды этого бота
    await pool.execute(
        "UPDATE bot_flood_events SET status='ended' WHERE bot_id=$1 AND status='active'",
        bot_id)
    row = await pool.fetchrow(
        """INSERT INTO bot_flood_events(bot_id, owner_id, peak_per_min, mode, status)
           VALUES($1,$2,$3,$4,'active') RETURNING id""",
        bot_id, owner_id, int(rate), mode)
    return int(row["id"])


async def increment_suspected(pool, episode_id: int, n: int = 1) -> None:
    await pool.execute(
        "UPDATE bot_flood_events SET suspected_count=suspected_count+$2, last_at=now() "
        "WHERE id=$1", episode_id, int(n))


async def active_episode(pool, bot_id: int) -> dict | None:
    r = await pool.fetchrow(
        """SELECT * FROM bot_flood_events
           WHERE bot_id=$1 AND status='active'
             AND last_at > now() - ($2 || ' seconds')::interval
           ORDER BY id DESC LIMIT 1""",
        bot_id, str(_EPISODE_COOLOFF_SEC))
    return dict(r) if r else None


async def end_stale_episodes(pool) -> int:
    """Помечает протухшие активные эпизоды как ended (для фонового свипа)."""
    res = await pool.execute(
        "UPDATE bot_flood_events SET status='ended' "
        "WHERE status='active' AND last_at < now() - ($1 || ' seconds')::interval",
        str(_EPISODE_COOLOFF_SEC))
    try:
        return int(res.split()[-1])
    except (ValueError, IndexError):
        return 0


# ── Пометка/очистка подозрительных ────────────────────────────────────────────
async def flag_user(pool, bot_id: int, user_id: int) -> None:
    await pool.execute(
        "UPDATE bot_users SET suspect=TRUE, flagged_at=now() "
        "WHERE bot_id=$1 AND user_id=$2 AND suspect=FALSE", bot_id, user_id)


async def flag_recent(pool, bot_id: int, minutes: int) -> int:
    """Пометить подозрительными всех, кто присоединился за последние N минут
    («заблокировать волну» вручную). Возвращает число помеченных."""
    m = max(1, int(minutes))
    res = await pool.execute(
        "UPDATE bot_users SET suspect=TRUE, flagged_at=now() "
        "WHERE bot_id=$1 AND suspect=FALSE "
        "AND first_seen > now() - ($2 || ' minutes')::interval", bot_id, str(m))
    try:
        return int(res.split()[-1])
    except (ValueError, IndexError):
        return 0


async def suspect_count(pool, bot_id: int) -> int:
    return int(await pool.fetchval(
        "SELECT count(*) FROM bot_users WHERE bot_id=$1 AND suspect", bot_id) or 0)


async def unflag_all(pool, bot_id: int) -> int:
    """Снять флаги подозрительности (восстановление после ложняка)."""
    res = await pool.execute(
        "UPDATE bot_users SET suspect=FALSE, flagged_at=NULL "
        "WHERE bot_id=$1 AND suspect", bot_id)
    try:
        return int(res.split()[-1])
    except (ValueError, IndexError):
        return 0


async def purge_suspects(pool, bot_id: int, *, hard: bool = False) -> int:
    """Быстрая очистка накрученных. По умолчанию МЯГКО (деактивировать: вон из
    аудитории, но строки остаются — обратимо). hard=True — удалить строки.
    В обоих случаях отписываем от воронок. Возвращает число затронутых."""
    if hard:
        res = await pool.execute(
            "DELETE FROM bot_users WHERE bot_id=$1 AND suspect", bot_id)
    else:
        res = await pool.execute(
            "UPDATE bot_users SET is_active=FALSE WHERE bot_id=$1 AND suspect", bot_id)
    try:
        n = int(res.split()[-1])
    except (ValueError, IndexError):
        n = 0
    # снять с воронок, чтобы им не капали цепочки сообщений (связь через funnels.bot_id)
    try:
        await pool.execute(
            """DELETE FROM funnel_subscriptions fs
               USING funnels f, bot_users bu
               WHERE f.id=fs.funnel_id AND f.bot_id=$1
                 AND bu.bot_id=$1 AND bu.suspect AND bu.user_id=fs.user_id""",
            bot_id)
    except Exception:
        log.debug("flood_guard.purge_suspects: funnel cleanup skipped bot=%s", bot_id)
    return n


# ── Высокоуровневый вход: решение по новому подписчику ────────────────────────
async def handle_new_user(pool, bot_id: int, owner_id: int, user_id: int) -> dict:
    """Вызывается на КАЖДОГО нового подписчика. Возвращает решение:
      {mode, attack, suspect, block, rate}.
    off → мгновенно нейтральное решение (никаких изменений поведения).
    Fail-open: при любом сбое возвращаем нейтральное решение, не роняем поллер."""
    neutral = {"mode": "off", "attack": False, "suspect": False, "block": False,
               "rate": 0}
    try:
        cfg = await get_config(pool, bot_id)
        mode = cfg.get("mode") or "off"
        if mode == "off":
            return neutral
        rate = record_new_user(bot_id)
        attack = is_attack(rate, cfg.get("threshold_per_min") or 0)
        suspect = attack and mode in ("protect", "block")
        block = attack and mode == "block"
        if attack:
            try:
                ep = await open_or_touch_episode(pool, bot_id, owner_id, mode, rate)
                if suspect:
                    await flag_user(pool, bot_id, user_id)
                    await increment_suspected(pool, ep, 1)
            except Exception:
                log.warning("flood_guard: episode/flag failed bot=%s", bot_id)
        return {"mode": mode, "attack": attack, "suspect": suspect,
                "block": block, "rate": rate}
    except Exception:
        log.warning("flood_guard.handle_new_user failed bot=%s", bot_id, exc_info=False)
        return neutral
