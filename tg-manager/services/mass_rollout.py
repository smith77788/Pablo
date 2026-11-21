"""Канарейка для массовых возможностей, которые выключены флагами.

Две возможности готовы в коде, но на проде не обкатаны:

* **пакетный инвайт** (`INVITE_BULK_API`, mass_inviter_engine) — один
  InviteToChannelRequest на весь батч вместо запроса на цель;
* **горизонтальный исполнитель** (`OP_SHARDED_EXECUTOR`, op_worker) — операции
  разбирают все процессы, а не один держатель аренды.

Раньше у обеих был только рубильник «всем сразу». Для самого баноопасного пути
продукта это неприемлемо: проблема видна только на живом Telegram, и узнавать
о ней по всему флоту сразу — значит платить аккаунтами. Здесь — постепенное
включение с тремя свойствами:

1. **Безопасные значения по умолчанию.** Без настроек модуль ничего не
   включает: поведение до этой правки сохраняется бит в бит.
2. **Включение по доле/объёму.** Пакетный инвайт — на доле операций (стабильно
   по op_id: одна операция не скачет между путями) или на списке владельцев.
   Горизонтальный исполнитель — потолок операций у НЕ-лидера: лидер работает как
   раньше, остальные берут по чуть-чуть.
3. **Быстрый откат без деплоя.** Ключи в `platform_settings` читаются с кэшем
   на `_SETTINGS_TTL_S` секунд, так что выключение доходит до всех процессов за
   полминуты, а не за редеплой. Для пакетного инвайта есть ещё автоматический
   предохранитель: серия флудов или откатов на поштучный путь выключает пакет в
   этом процессе на время остывания.

Каждое решение пишется в метрики (`/metrics`) и в лог — по ним видно, что
обкатка идёт штатно. Шаги обкатки и отката — docs/MASS_OPS_CANARY.md.
"""
from __future__ import annotations

import collections
import hashlib
import logging
import os
import threading
import time

from services import metrics

log = logging.getLogger(__name__)

_TRUE = ("1", "true", "yes", "on")

# Ключи platform_settings (рантайм, без деплоя). Пустое/отсутствующее значение —
# берём окружение.
KEY_BULK_KILL = "invite_bulk_kill"
KEY_BULK_PCT = "invite_bulk_canary_pct"
KEY_FOLLOWER_CAP = "op_sharded_follower_max_parallel"

_SETTINGS_TTL_S = 30.0
_settings_cache: dict[str, tuple[str | None, float]] = {}


def _env_true(name: str) -> bool:
    return str(os.getenv(name, "")).strip().lower() in _TRUE


def _clamp_int(raw, lo: int, hi: int) -> int | None:
    try:
        return max(lo, min(int(str(raw).strip()), hi))
    except (TypeError, ValueError):
        return None


async def _setting(pool, key: str) -> str | None:
    """Значение из platform_settings с коротким кэшем. Сбой БД — прошлое
    значение (или None): канарейка не повод ронять операцию, а выключатель,
    однажды прочитанный, не должен «забыться» из-за сетевого блипа."""
    now = time.monotonic()
    cached = _settings_cache.get(key)
    if cached is not None and now - cached[1] < _SETTINGS_TTL_S:
        return cached[0]
    if pool is None:
        return cached[0] if cached else None
    try:
        row = await pool.fetchrow(
            "SELECT value FROM platform_settings WHERE key=$1", key)
        val = (str(row["value"]).strip() if row and row["value"] is not None
               else None) or None
    except Exception as e:
        log.warning("mass_rollout: platform_settings[%s] не прочитан (%s)",
                    key, str(e)[:80])
        return cached[0] if cached else None
    _settings_cache[key] = (val, now)
    return val


def _bucket(op_id) -> int:
    """Стабильная корзина 0..99 по id операции (не hash(): тот солится на процесс)."""
    digest = hashlib.sha256(f"bulk:{op_id}".encode()).digest()
    return int.from_bytes(digest[:4], "big") % 100


def _canary_owners() -> frozenset[int]:
    out = set()
    for part in str(os.getenv("INVITE_BULK_CANARY_OWNERS", "")).replace(";", ",").split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            out.add(int(part))
    return frozenset(out)


# ── Пакетный инвайт: решение на операцию ─────────────────────────────────────

async def bulk_killed(pool) -> bool:
    """Выключатель пакетного пути — проверяется и на каждом батче идущей
    операции, чтобы откат доходил до неё за TTL кэша, а не на рестарте."""
    if _env_true("INVITE_BULK_KILL"):
        return True
    return ((await _setting(pool, KEY_BULK_KILL)) or "").lower() in _TRUE


async def bulk_invite_decision(pool, op_id, owner_id, explicit: bool | None
                               ) -> tuple[bool, str]:
    """Идти ли этой операции пакетным путём. Возвращает (решение, причина).

    Порядок важен: выключатель сильнее всего, даже явного bulk_api=True у
    операции — иначе откат не откатывал бы то, что включили вручную.
      1. kill   — INVITE_BULK_KILL=1 или platform_settings.invite_bulk_kill=1;
      2. explicit — у операции явно задан bulk_api;
      3. global — INVITE_BULK_API=1 (прежний рубильник «всем»);
      4. canary_owner — владелец в INVITE_BULK_CANARY_OWNERS;
      5. canary_pct — корзина op_id меньше доли (platform_settings
         invite_bulk_canary_pct, иначе INVITE_BULK_CANARY_PCT; по умолчанию 0);
      6. off.
    """
    if await bulk_killed(pool):
        decision = (False, "kill")
    elif explicit is not None:
        decision = (bool(explicit), "explicit")
    elif _env_true("INVITE_BULK_API"):
        decision = (True, "global")
    elif owner_id is not None and int(owner_id) in _canary_owners():
        decision = (True, "canary_owner")
    else:
        pct_raw = await _setting(pool, KEY_BULK_PCT)
        pct = _clamp_int(pct_raw if pct_raw is not None
                         else os.getenv("INVITE_BULK_CANARY_PCT", "0"), 0, 100) or 0
        if pct > 0 and _bucket(op_id) < pct:
            decision = (True, "canary_pct")
        else:
            decision = (False, "off")
    metrics.inc("infragram_invite_bulk_decisions_total",
                {"path": "bulk" if decision[0] else "single", "reason": decision[1]})
    if decision[0] or decision[1] == "kill":
        log.info("invite op=%s owner=%s: пакетный путь=%s (%s)",
                 op_id, owner_id, "да" if decision[0] else "нет", decision[1])
    return decision


def bulk_max_batch() -> int:
    """Потолок целей в одном пакетном запросе. Батч крупнее идёт поштучно:
    одна ошибка пакета роняет весь запрос, и на канарейке её цена должна быть
    маленькой. По умолчанию 5 — это и дефолтный batch_size инвайта."""
    return _clamp_int(os.getenv("INVITE_BULK_MAX_BATCH", "5"), 2, 200) or 5


# ── Пакетный инвайт: автоматический предохранитель (в памяти процесса) ───────
#
# Ручной выключатель срабатывает, когда человек увидел график. Предохранитель —
# когда график ещё рисуется: окно последних пакетных батчей, и если в нём
# набралось флудов или откатов на поштучный путь больше порога, пакет в ЭТОМ
# процессе выключается на время остывания. Цели при этом не теряются —
# invite_batch просто идёт поштучным путём, как до флага.

_WINDOW = 20
_lock = threading.Lock()
_recent: collections.deque[str] = collections.deque(maxlen=_WINDOW)
_open_until = 0.0


def _breaker_cfg() -> tuple[int, int, float, float]:
    flood_max = _clamp_int(os.getenv("INVITE_BULK_BREAKER_FLOODS", "3"), 1, _WINDOW) or 3
    min_samples = _clamp_int(os.getenv("INVITE_BULK_BREAKER_MIN_SAMPLES", "6"), 1, _WINDOW) or 6
    fallback_share = (_clamp_int(os.getenv("INVITE_BULK_BREAKER_FALLBACK_PCT", "50"), 1, 100)
                      or 50) / 100.0
    cooldown = float(_clamp_int(os.getenv("INVITE_BULK_BREAKER_COOLDOWN_SEC", "21600"),
                                60, 7 * 86400) or 21600)
    return flood_max, min_samples, fallback_share, cooldown


def bulk_breaker_open() -> bool:
    with _lock:
        return time.monotonic() < _open_until


def record_bulk_batch(outcome: str) -> None:
    """Исход пакетного батча: ok | fallback | peer_flood | flood_wait | fatal.

    Никогда не бросает — учёт не должен ронять инвайт.
    """
    global _open_until
    try:
        metrics.inc("infragram_invite_bulk_batches_total", {"outcome": outcome})
        flood_max, min_samples, fallback_share, cooldown = _breaker_cfg()
        with _lock:
            _recent.append(outcome)
            n = len(_recent)
            floods = sum(1 for o in _recent if o in ("peer_flood", "flood_wait"))
            fallbacks = sum(1 for o in _recent if o == "fallback")
            trip = floods >= flood_max or (
                n >= min_samples and fallbacks / n >= fallback_share)
            if trip and time.monotonic() >= _open_until:
                _open_until = time.monotonic() + cooldown
                _recent.clear()
            else:
                trip = False
        if trip:
            metrics.inc("infragram_invite_bulk_breaker_trips_total")
            log.error(
                "invite: пакетный путь ВЫКЛЮЧЕН предохранителем на %d мин "
                "(флудов %d, откатов на поштучный %d из %d последних батчей). "
                "Инвайт продолжается поштучно.",
                int(cooldown // 60), floods, fallbacks, n)
        metrics.gauge("infragram_invite_bulk_breaker_open", 1.0 if bulk_breaker_open() else 0.0)
    except Exception:
        pass


def _reset_for_tests() -> None:
    global _open_until
    with _lock:
        _recent.clear()
        _open_until = 0.0
    _settings_cache.clear()


# ── Горизонтальный исполнитель: потолок у не-лидера ─────────────────────────

_last_cap_logged: list = [None]


async def sharded_follower_cap(pool, max_parallel: int) -> int:
    """Сколько операций одновременно может держать процесс БЕЗ лидерства.

    По умолчанию 1: при включённом OP_SHARDED_EXECUTOR лидер работает как
    раньше, а каждый дополнительный процесс берёт по одной операции — канарейка
    по объёму. Поднимается ключом platform_settings
    op_sharded_follower_max_parallel (без деплоя) или OP_SHARDED_FOLLOWER_MAX_PARALLEL.
    0 — «слить»: не-лидеры не берут новых операций и доигрывают текущие. Это
    мягкий откат без рестарта; жёсткий — OP_SHARDED_EXECUTOR=0 на всех репликах.
    """
    raw = await _setting(pool, KEY_FOLLOWER_CAP)
    if raw is None:
        raw = os.getenv("OP_SHARDED_FOLLOWER_MAX_PARALLEL", "1")
    cap = _clamp_int(raw, 0, max_parallel)
    if cap is None:
        cap = min(1, max_parallel)
    metrics.gauge("infragram_executor_follower_cap", float(cap))
    if _last_cap_logged[0] != cap:
        _last_cap_logged[0] = cap
        log.info("op_worker: потолок операций не-лидера = %d (из %d)", cap, max_parallel)
    return cap
