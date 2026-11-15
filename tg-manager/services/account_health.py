"""Account Health Engine — comprehensive account health evaluation.

Tracks:
  - health_score (0-100): overall account reliability
  - load_score (0-100): current load (higher = use less)
  - warmup_state: raw/warming/ready/veteran
  - restriction_history: ban/block history
  - operation success/fail rates

Integrates with tg_accounts (trust_score, cooldown_until, acc_status),
account_flood_log, account_trust_history.

Usage:
    from services.account_health import get_health, compute_health_score

    health = get_health(account_id)
    score = compute_health_score(
        trust_score=0.8,
        flood_count_7d=0,
        spamblock=False,
        success_rate=0.95,
        days_active=30,
    )
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum

import asyncpg

from services.account_manager import (
    effective_account_status,
    should_persist_account_status,
)
from services.logger import log_exc_swallow
from services import account_status as _acc_status

_ACC_NOT_DEAD = _acc_status.sql_not_dead("a.acc_status")

log = logging.getLogger(__name__)


class WarmupState(str, Enum):
    RAW = "raw"  # новый аккаунт, никогда не использовался
    WARMING = "warming"  # в процессе разогрева
    READY = "ready"  # готов к операциям
    VETERAN = "veteran"  # проверенный, много успешных операций


@dataclass
class AccountHealth:
    account_id: int
    health_score: float = 100.0  # 0-100, выше = лучше
    load_score: float = 0.0  # 0-100, выше = более нагружен
    warmup_state: WarmupState = WarmupState.RAW
    total_ops: int = 0
    success_ops: int = 0
    fail_ops: int = 0
    flood_events_7d: int = 0
    spamblock_events: int = 0
    restriction_count: int = 0
    last_updated: float = field(default_factory=time.monotonic)
    suitability: dict[str, bool] = field(
        default_factory=lambda: {
            "invite": True,
            "dm": True,
            "create": True,
            "post": True,
            "join": True,
        }
    )


# In-memory health cache
_health_cache: dict[int, AccountHealth] = {}


def get_health(account_id: int) -> AccountHealth:
    if account_id not in _health_cache:
        _health_cache[account_id] = AccountHealth(account_id=account_id)
    return _health_cache[account_id]


# Пороги health_score, по которым продукт ПЕРЕСТАЁТ брать аккаунт. Живут здесь,
# рядом со счётчиком: раньше число 10 было выписано в риск-пульсе
# (`infra_memory.get_account_health`), а экран снятия пауз о нём не знал вовсе —
# аккаунт, которого держал только этот счётчик, в списке «кто на паузе» не
# появлялся, и кнопка отвечала «нет активных кулдаунов».
QUARANTINE_SCORE = 10.0   # ниже — пульс считает аккаунт выбывшим
RISK_SCORE = 30.0         # ниже — под риском, но ещё в работе


def local_block_reason(account_id: int) -> str | None:
    """Чем аккаунт держит ПАМЯТЬ ПРОЦЕССА, а не база. None — ничем.

    Два тормоза живут только в памяти воркера и переживают любую очистку БД:
    упавший до нуля `health_score` (его ставит `update_after_failure` на бане
    или спам-блоке) и снятые флаги `suitability`, из-за которых
    `get_sorted_accounts` молча выбрасывает аккаунт из подбора под действие.
    Оба снимает только ручной сброс (`account_reset`) или следующий цикл
    `load_from_db`, поэтому экран «снять паузы» обязан их видеть — иначе он
    говорит «все аккаунты доступны» о том, кого продукт не берёт.
    """
    if account_id not in _health_cache:
        return None                      # о нём ничего не помним — не держим
    health = _health_cache[account_id]
    if health.health_score < QUARANTINE_SCORE:
        return f"здоровье в памяти воркера {int(health.health_score)}/100"
    blocked = sorted(k for k, ok in health.suitability.items() if not ok)
    if blocked:
        return "запрет действий в памяти воркера: " + ", ".join(blocked)
    return None


def clear_local_blocks(account_id: int) -> None:
    """Забыть процесс-локальные тормоза аккаунта (ручной сброс риска).

    Возвращаем счётчики к НЕЙТРАЛЬНОМУ значению, с которым работает свежий
    воркер (100 — «ничего не знаем», именно так их читает риск-пульс), а не к
    выдуманному хорошему: настоящую оценку заново считает `load_from_db` из
    базы. История ограничений в БД при этом цела, и её фильтрует
    `risk_cleared_at` — здесь снимается только память процесса.

    Раньше сброс возвращал ровно два флага из пяти (`dm`, `invite`), а
    `health_score` не трогал вовсе: после бана, который в памяти обнуляет и
    счётчик, и ВСЕ флаги, аккаунт оставался выключенным для `create`, `post` и
    `join`, а пульс продолжал светить «на паузе» — та же жалоба «кулдаун не
    сбрасывается», только причину было не видно.
    """
    if account_id not in _health_cache:
        return
    health = _health_cache[account_id]
    health.health_score = 100.0
    health.load_score = 0.0
    for key in list(health.suitability):
        health.suitability[key] = True
    health.last_updated = time.monotonic()


def compute_health_score(
    trust_score: float,
    flood_count_7d: int,
    spamblock: bool,
    success_rate: float,  # 0.0-1.0
    days_active: int,
) -> float:
    """Вычисляет health_score (0-100) по нескольким факторам."""
    score = 100.0

    # Trust score (0-1 → 0-30 points)
    score -= (1.0 - min(1.0, trust_score)) * 30

    # Flood penalties
    score -= min(flood_count_7d * 5, 30)

    # SpamBot restriction
    if spamblock:
        score -= 40

    # Success rate factor (0-20 points)
    score -= (1.0 - success_rate) * 20

    # Age bonus (up to +10 for accounts active 30+ days)
    score += min(days_active / 3.0, 10)

    return max(0.0, min(100.0, score))


def estimate_warmup_state(
    days_active: int, total_ops: int, trust_score: float
) -> WarmupState:
    if trust_score < 0.2 or total_ops == 0:
        return WarmupState.RAW
    if days_active < 3 or total_ops < 10:
        return WarmupState.WARMING
    if trust_score >= 0.8 and total_ops >= 100:
        return WarmupState.VETERAN
    return WarmupState.READY


def update_after_success(account_id: int, action_type: str = "default") -> None:
    health = get_health(account_id)
    health.total_ops += 1
    health.success_ops += 1
    health.load_score = min(100.0, health.load_score + 1.0)
    # Small health boost on success
    health.health_score = min(100.0, health.health_score + 0.2)
    health.last_updated = time.monotonic()


def update_after_failure(
    account_id: int,
    action_type: str = "default",
    is_flood: bool = False,
    is_spamblock: bool = False,
    is_ban: bool = False,
) -> None:
    health = get_health(account_id)
    health.total_ops += 1
    health.fail_ops += 1

    if is_flood:
        health.flood_events_7d += 1
        health.health_score -= 5
        health.load_score += 10

    if is_spamblock:
        health.spamblock_events += 1
        health.health_score -= 25
        health.suitability["dm"] = False
        health.suitability["invite"] = False

    if is_ban:
        health.health_score = 0
        health.suitability = {k: False for k in health.suitability}

    health.health_score = max(0.0, health.health_score)
    health.last_updated = time.monotonic()


async def load_from_db(pool: asyncpg.Pool, owner_id: int) -> int:
    """Загружает статистику здоровья всех аккаунтов из БД.

    Объединяет данные из tg_accounts, account_flood_log, operation_audit
    и account_warmup_plans для точного определения warmup_state.
    Аккаунты с активным планом разогрева отображаются как 'warming',
    а не 'raw'/'ready' (что было неверно без проверки warmup_plans).
    """
    rows = await pool.fetch(
        """SELECT
               a.id,
               a.trust_score,
               COALESCE(a.flood_count_7d, 0) AS flood_count_7d,
               COALESCE(a.acc_status, 'active') AS acc_status,
               EXTRACT(DAY FROM NOW() - a.added_at)::int AS days_active,
               COUNT(DISTINCT fl.id) FILTER (WHERE fl.created_at > NOW() - INTERVAL '7d') AS floods_7d,
               COUNT(DISTINCT oa.id) FILTER (WHERE oa.result='success') AS ops_ok,
               COUNT(DISTINCT oa.id) FILTER (WHERE oa.result!='success' AND oa.result IS NOT NULL) AS ops_fail,
               EXISTS(
                   SELECT 1 FROM account_warmup_plans wp
                   WHERE wp.account_id = a.id AND wp.status = 'active'
               ) AS has_active_warmup
           FROM tg_accounts a
           LEFT JOIN account_flood_log fl ON fl.account_id = a.id
           LEFT JOIN operation_audit oa ON oa.account_id = a.id
           WHERE a.owner_id = $1
             AND a.is_active = TRUE
           GROUP BY a.id, a.trust_score, a.flood_count_7d, a.acc_status, a.added_at""",
        owner_id,
    )

    loaded = 0
    for row in rows:
        acc_id = row["id"]
        total = (row["ops_ok"] or 0) + (row["ops_fail"] or 0)
        success_rate = (row["ops_ok"] or 0) / max(total, 1)
        is_spamblock = row["acc_status"] in ("spamblock", "cooldown")

        score = compute_health_score(
            trust_score=float(row["trust_score"] or 1.0),
            flood_count_7d=row["floods_7d"] or 0,
            spamblock=is_spamblock,
            success_rate=success_rate,
            days_active=row["days_active"] or 0,
        )

        # Если аккаунт имеет активный план разогрева — явно ставим WARMING,
        # иначе оцениваем по операциям и возрасту.
        if row["has_active_warmup"]:
            warmup = WarmupState.WARMING
        else:
            warmup = estimate_warmup_state(
                row["days_active"] or 0,
                total,
                float(row["trust_score"] or 1.0),
            )

        health = get_health(acc_id)
        health.health_score = score
        health.warmup_state = warmup
        health.total_ops = total
        health.success_ops = row["ops_ok"] or 0
        health.fail_ops = row["ops_fail"] or 0
        health.flood_events_7d = row["floods_7d"] or 0
        health.load_score = min(100.0, (row["floods_7d"] or 0) * 5.0)

        if is_spamblock:
            health.suitability["dm"] = False
            health.suitability["invite"] = False

        loaded += 1

    log.info("account_health: loaded %d accounts for owner=%d", loaded, owner_id)
    return loaded


async def get_sorted_accounts(
    pool: asyncpg.Pool,
    owner_id: int,
    action_type: str = "default",
    limit: int = 20,
) -> list[dict]:
    """Возвращает аккаунты отсортированные по пригодности для action_type."""
    rows = await pool.fetch(
        """SELECT a.id, a.session_str, a.phone, a.first_name, a.trust_score,
                  COALESCE(a.acc_status, 'active') AS acc_status, a.is_active,
                  (a.session_str IS NOT NULL AND a.session_str != '') AS has_session,
                  a.device_model, a.system_version, a.app_version,
                  a.lang_code, a.system_lang_code, a.proxy_id,
                  p.proxy_url, p.geo_country
           FROM tg_accounts a
           LEFT JOIN user_proxies p ON p.id = a.proxy_id AND p.is_active = TRUE
           WHERE a.owner_id = $1
             AND a.is_active = TRUE
             AND (a.cooldown_until IS NULL OR a.cooldown_until < NOW())
             AND a.session_str IS NOT NULL AND a.session_str != ''
             -- Мёртвый статус отсекаем В ЗАПРОСЕ, а не после LIMIT. Раньше
             -- запрос брал limit*2 лучших по доверию и выбрасывал мёртвых уже
             -- в питоне: на флоте, где мёртвых больше половины, этого запаса
             -- не хватало, и список «кем работать» приходил короче
             -- запрошенного или пустым при живых аккаунтах ниже по доверию.
             AND """
        + _ACC_NOT_DEAD
        + """
           ORDER BY a.trust_score DESC NULLS LAST
           LIMIT $2""",
        owner_id,
        limit * 2,
    )

    scored = []
    for row in rows:
        effective_status = effective_account_status(
            row.get("acc_status"),
            has_session=bool(row.get("has_session")),
            is_active=bool(row.get("is_active", True)),
        )
        # Набор — из общего словаря: свой список из пяти значений пропускал
        # `deleted` и `frozen`, и отсортированный список «кем работать»
        # предлагал аккаунты, которых операция уже не берёт.
        if _acc_status.is_effectively_dead(effective_status):
            continue
        health = get_health(row["id"])
        # Проверяем пригодность для данного типа действия
        if action_type in health.suitability and not health.suitability[action_type]:
            continue
        scored.append((health.health_score - health.load_score / 2, dict(row)))

    scored.sort(key=lambda x: -x[0])
    return [item[1] for item in scored[:limit]]


def get_health_summary(account_ids: list[int]) -> list[dict]:
    """Сводная таблица здоровья аккаунтов для UI."""
    result = []
    for acc_id in account_ids:
        h = get_health(acc_id)
        result.append(
            {
                "account_id": acc_id,
                "health_score": round(h.health_score, 1),
                "load_score": round(h.load_score, 1),
                "warmup_state": h.warmup_state.value,
                "flood_events_7d": h.flood_events_7d,
                "success_rate": round(h.success_ops / max(h.total_ops, 1) * 100, 1),
                "suitability": h.suitability,
            }
        )
    return sorted(result, key=lambda x: -x["health_score"])


async def _persist_health_snapshots(
    pool: asyncpg.Pool, owner_id: int | None = None,
) -> int:
    """Сохраняет текущие health_score из in-memory кеша в account_health_history.

    ОДНИМ запросом, а не строкой на аккаунт. Раньше здесь был `pool.execute`
    на каждый аккаунт кеша: флот в 6000 аккаунтов — 6000 отдельных захватов
    соединения пула подряд. На стенде (12.11.2026) это давало очередь до 28
    ждущих соединения, и запись журнала целей операций (`_journal_write`)
    ждала пул до 0.5 с — защитная запись стояла за снимками для графика.

    `owner_id` — снимки только аккаунтов этого владельца. Проверка аккаунтов
    одного владельца (`_exec_check_accounts_health`) звала эту функцию без
    него и переписывала снимки ВСЕГО флота платформы: кеш общий на процесс.
    Десять проверок подряд — десять копий истории всех аккаунтов всех
    клиентов (на стенде 96 тыс. строк за минуту вместо ~2 тыс.).
    """
    if not _health_cache:
        return 0

    import json as _json

    items = list(_health_cache.items())
    acc_ids = [int(acc_id) for acc_id, _ in items]
    # owner_id и trust_score — одним запросом, и там же отбор по владельцу:
    # кеш про владельца не знает, база знает.
    acc_info = await pool.fetch(
        "SELECT id, owner_id, trust_score FROM tg_accounts "
        "WHERE id = ANY($1::int[]) AND ($2::bigint IS NULL OR owner_id = $2)",
        acc_ids, owner_id,
    )
    info_map: dict[int, tuple[int, float]] = {
        int(r["id"]): (int(r["owner_id"]), float(r["trust_score"] or 0))
        for r in acc_info
    }

    cols: list[list] = [[] for _ in range(10)]
    for acc_id, health in items:
        info = info_map.get(int(acc_id))
        if info is None:
            continue
        row = (
            int(acc_id), info[0],
            round(health.health_score, 2), round(health.load_score, 2),
            # numeric(5,4): доверие в базе — доля от 0 до 1
            max(0.0, min(info[1], 9.9999)),
            int(health.flood_events_7d or 0), int(health.success_ops or 0),
            int(health.fail_ops or 0), health.warmup_state.value,
            _json.dumps(health.suitability, ensure_ascii=False),
        )
        for i, v in enumerate(row):
            cols[i].append(v)
    if not cols[0]:
        return 0
    try:
        await pool.execute(
            """INSERT INTO account_health_history
                   (account_id, owner_id, health_score, load_score, trust_score,
                    flood_events_7d, success_ops, fail_ops, warmup_state, suitability)
               SELECT a, o, h, l, t, f, s, x, w, j::jsonb
                 FROM unnest($1::int[], $2::bigint[], $3::numeric[], $4::numeric[],
                             $5::numeric[], $6::int[], $7::int[], $8::int[],
                             $9::text[], $10::text[])
                      AS u(a, o, h, l, t, f, s, x, w, j)""",
            *cols,
        )
    except Exception:
        log_exc_swallow(
            log,
            "Сбой записи health-снапшотов — таблица может ещё не существовать",
            accounts=len(cols[0]),
        )
        return 0
    return len(cols[0])


async def _cleanup_old_health_history(pool: asyncpg.Pool) -> int:
    """Удаляет health-снапшоты старше 30 дней."""
    try:
        deleted = await pool.fetchval(
            "WITH d AS (DELETE FROM account_health_history "
            "WHERE recorded_at < NOW() - INTERVAL '30 days' RETURNING 1) "
            "SELECT COUNT(*) FROM d"
        )
        return int(deleted or 0)
    except Exception:
        log_exc_swallow(log, "Сбой очистки старых health-снапшотов")
        return 0


# Охват проверки через @SpamBot. Раньше: 10 аккаунтов раз в 6 часов на ВСЮ
# платформу (40 в сутки), и выборка шла по last_real_check_at, который каждые
# 3–6 часов освежает пинг живости сессии, — то есть спам-проверка видела только
# аккаунты, до которых пинг не дошёл. Флот из 500 аккаунтов проверялся на
# спам-блок и заморозку раз в недели, и ограниченный аккаунт всё это время
# брали в работу. Теперь: своя отметка (v340), сутки на аккаунт, размер круга
# — по флоту, несколько проверок параллельно (разные аккаунты, разные прокси).
_SPAMBOT_PERIOD_H = 24        # каждый аккаунт — раз в сутки, не чаще
_SPAMBOT_CYCLE_H = 1          # цикл здоровья — раз в час
_SPAMBOT_MIN_BATCH = 10
_SPAMBOT_MAX_BATCH = 150      # потолок круга: даже огромный флот не душит цикл
_SPAMBOT_CONCURRENCY = 4


def spambot_batch_size(active_accounts: int) -> int:
    """Сколько аккаунтов проверить за круг, чтобы флот укладывался в сутки."""
    try:
        n = int(active_accounts or 0)
    except (TypeError, ValueError):
        n = 0
    cycles = max(1, _SPAMBOT_PERIOD_H // _SPAMBOT_CYCLE_H)
    need = -(-n // cycles)
    return max(_SPAMBOT_MIN_BATCH, min(_SPAMBOT_MAX_BATCH, need))


async def _run_spambot_check_cycle(pool: asyncpg.Pool) -> None:
    """Периодическая проверка acc_status через SpamBot (раз в сутки на аккаунт).

    Обновляет acc_status, trust_score в DB. При обнаружении spamblock — снижает trust.
    Не уведомляет — уведомления идут из account_monitor.
    """
    from services.account_manager import check_account_status_full

    try:
        active = await pool.fetchval(
            "SELECT COUNT(*) FROM tg_accounts WHERE is_active=TRUE "
            "AND session_str IS NOT NULL AND session_str != ''")
    except Exception:
        active = 0
    batch = spambot_batch_size(active)

    # Аккаунты с session_str, не проверявшиеся @SpamBot больше суток.
    # Важно: JOIN user_proxies для получения proxy_url — иначе проверка идёт
    # без прокси и вызывает AUTH_KEY collision.
    accounts = await pool.fetch(
        """SELECT a.id, a.session_str, a.phone, a.first_name, a.username,
                  a.device_model, a.system_version, a.app_version,
                  a.proxy_id, p.proxy_url, p.geo_country
           FROM tg_accounts a
           LEFT JOIN user_proxies p ON p.id = a.proxy_id AND p.is_active = TRUE
           WHERE a.is_active=TRUE
             AND a.session_str IS NOT NULL AND a.session_str != ''
             -- НЕ трогаем занятые операцией: параллельный коннект одной сессии
             -- проверкой и операцией = AUTH_KEY_DUPLICATED (свежий флот с
             -- last_spambot_check_at IS NULL проверка берёт первым).
             AND COALESCE(a.in_operation, FALSE) = FALSE
             AND (a.last_spambot_check_at IS NULL
                  OR a.last_spambot_check_at < NOW() - make_interval(hours => $2))
           ORDER BY COALESCE(a.last_spambot_check_at, '2000-01-01') ASC
           LIMIT $1""",
        batch, _SPAMBOT_PERIOD_H,
    )
    if not accounts:
        return

    log.info(
        "account_health: spambot check cycle — %d accounts to check (fleet %s)",
        len(accounts), active,
    )
    sem = asyncio.Semaphore(_SPAMBOT_CONCURRENCY)

    async def _one(acc) -> None:
        async with sem:
            await _spambot_check_one(pool, acc, check_account_status_full)
            await asyncio.sleep(3)  # небольшая пауза между аккаунтами слота

    await asyncio.gather(*(_one(acc) for acc in accounts), return_exceptions=True)


async def _retry_spambot_later(pool: asyncpg.Pool, acc_id: int) -> None:
    """Проверка не удалась (таймаут, сбой) — повторить через ~6 часов.

    Без отметки неудавшийся аккаунт оставался самым «старым» и занимал голову
    очереди каждый круг: сотня аккаунтов на мёртвых прокси навсегда закрывала
    проверку остальному флоту.
    """
    try:
        await pool.execute(
            "UPDATE tg_accounts SET last_spambot_check_at = now() - make_interval(hours => $2) "
            "WHERE id=$1", acc_id, max(0, _SPAMBOT_PERIOD_H - 6))
    except Exception:
        log_exc_swallow(log, "account_health: retry mark acc=%d", acc_id)


async def _spambot_check_one(pool: asyncpg.Pool, acc, check_account_status_full) -> None:
    """Проверка одного аккаунта через @SpamBot (тело прежнего цикла)."""
    # Атомарный захват у арбитра op_worker ПЕРЕД коннектом: check_account_
    # status_full открывает ЖИВУЮ сессию. Снимок is_account_in_use оставлял
    # окно гонки — операция захватывала аккаунт между проверкой и коннектом →
    # одна сессия с двух IP = AUTH_KEY_DUPLICATED (особенно свежий флот).
    _opw = None
    _leased = True
    try:
        from services import op_worker as _opw_mod
        _opw = _opw_mod
        _leased = await _opw.try_claim_account(int(acc["id"]))
    except Exception:
        _opw = None
        _leased = True
    if not _leased:
        return
    try:
        result = await asyncio.wait_for(
            check_account_status_full(
                acc["session_str"], dict(acc), check_spambot=True
            ),
            timeout=30.0,
        )
        status = result.get("status", "active")
        auth_error = result.get("auth_error", False)

        # Не обновляем acc_status для no_session — аккаунт просто не импортирован
        if result.get("no_session"):
            return

        # Только подтверждённые статусы пишем в БД — session_expired без auth_error игнорируем
        if should_persist_account_status(
            status,
            auth_error=auth_error,
            has_session=not result.get("no_session", False),
            restriction_checked=result.get("restriction_checked", True),
        ):
            await pool.execute(
                "UPDATE tg_accounts SET acc_status=$1, last_real_check_at=now(), "
                "last_spambot_check_at=now() WHERE id=$2",
                status,
                acc["id"],
            )
        else:
            # Любой спорный статус — только обновляем время проверки, не меняем acc_status
            await pool.execute(
                "UPDATE tg_accounts SET last_real_check_at=now(), "
                "last_spambot_check_at=now() WHERE id=$1",
                acc["id"],
            )

        if status == "spamblock":
            await pool.execute(
                "UPDATE tg_accounts SET trust_score=LEAST(COALESCE(trust_score,1.0), 0.3) WHERE id=$1",
                acc["id"],
            )
            log.warning("account_health: spamblock detected acc=%d", acc["id"])
        elif status in ("banned", "deactivated", "session_expired") and auth_error:
            # Деактивируем только при явном auth_error от Telegram
            await pool.execute(
                "UPDATE tg_accounts SET is_active=FALSE WHERE id=$1", acc["id"]
            )
            log.warning(
                "account_health: acc=%d deactivated status=%s (auth_error)",
                acc["id"],
                status,
            )
    except asyncio.TimeoutError:
        log.debug("account_health: spambot check timeout acc=%d", acc["id"])
        await _retry_spambot_later(pool, acc["id"])
    except Exception as e:
        log_exc_swallow(
            log, "account_health spambot check acc=%d: %s", acc["id"], e
        )
        await _retry_spambot_later(pool, acc["id"])
    finally:
        if _opw and _leased:
            try:
                await _opw.release_accounts([int(acc["id"])])
            except Exception:
                log_exc_swallow(log, "account_health: release acc=%d", acc["id"])



async def run_health_check_loop(pool: asyncpg.Pool, interval_s: int = 3600) -> None:
    """Фоновый цикл: каждый час пересчитывает здоровье и сохраняет снапшоты.

    Использует time-anchored sleep чтобы избежать наслаивания циклов:
    если сам health check занял N секунд, следующий sleep = max(0, interval_s - N).
    """
    cycle = 0
    while True:
        started_at = time.monotonic()
        try:
            owners = await pool.fetch(
                "SELECT DISTINCT owner_id FROM tg_accounts WHERE is_active=TRUE"
            )
            total_loaded = 0
            for row in owners:
                total_loaded += await load_from_db(pool, row["owner_id"])

            # Сохраняем снапшоты health_score в БД для трендов.
            # Вызываем безусловно: _persist_health_snapshots сама проверяет
            # наличие данных в кеше, поэтому пустой вызов безопасен.
            # Ранее условие total_loaded > 0 не давало записывать снапшоты
            # для аккаунтов, обновлённых только через in-memory (record_flood
            # и update_after_success/failure) без полной перезагрузки из БД.
            written = await _persist_health_snapshots(pool)
            if written:
                log.debug("account_health: persisted %d snapshots", written)

            # Чистка старых снапшотов раз в сутки (24 цикла)
            if cycle % 24 == 0:
                cleaned = await _cleanup_old_health_history(pool)
                if cleaned:
                    log.debug("account_health: cleaned %d old snapshots", cleaned)

            # SpamBot проверка — каждый круг; темп держит сама выборка
            # (сутки на аккаунт, размер круга по флоту — spambot_batch_size).
            try:
                await _run_spambot_check_cycle(pool)
            except Exception:
                log_exc_swallow(log, "account_health: spambot check cycle failed")

            cycle += 1
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("account_health loop error")
        elapsed = time.monotonic() - started_at
        await asyncio.sleep(max(0.0, interval_s - elapsed))
