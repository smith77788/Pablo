"""Physics Engine — телеметрия операций и оценка риска бана по аккаунтам.

Риск пересчитывается раз в час для всех аккаунтов с сессией по факторам
(см. «Модель риска» ниже); щит (`account_shield`) пересчитывает владельца
и перед своей проверкой, поэтому решения стоят на свежей оценке.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math

import asyncpg

log = logging.getLogger(__name__)

_LOOP_INTERVAL = 3600  # recompute every hour


# ─── Telemetry write (fire-and-forget safe) ───────────────────────────────────


async def record_telemetry(
    pool: asyncpg.Pool,
    account_id: int,
    owner_id: int | None,
    op_type: str,
    outcome: str,
    flood_wait_s: int = 0,
    duration_ms: int = 0,
) -> None:
    """Record one operation outcome. Never raises."""
    try:
        await pool.execute(
            """INSERT INTO op_telemetry
               (account_id, owner_id, op_type, outcome, flood_wait_s, duration_ms)
               VALUES ($1, $2, $3, $4, $5, $6)""",
            account_id,
            owner_id,
            op_type,
            outcome,
            max(0, flood_wait_s or 0),
            max(0, duration_ms or 0),
        )
    except Exception as e:
        log.debug("physics_engine.record_telemetry: %s", e)


# ─── Risk query ───────────────────────────────────────────────────────────────


def _parse_factors(raw) -> list:
    """factors лежит в jsonb, а asyncpg отдаёт jsonb СТРОКОЙ (кодек не ставится)."""
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        return list(raw)
    try:
        val = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return val if isinstance(val, list) else []


async def get_account_risk(pool: asyncpg.Pool, account_id: int) -> dict:
    """Returns risk dict for one account. Never raises."""
    try:
        row = await pool.fetchrow(
            """SELECT risk_score, ban_probability, flood_rate_1h, ops_24h,
                      last_flood_at, factors, computed_at
               FROM account_risk_scores WHERE account_id=$1""",
            account_id,
        )
        if row:
            return {
                "risk_score": row["risk_score"],
                "ban_probability": row["ban_probability"],
                "flood_rate_1h": row["flood_rate_1h"],
                "ops_24h": row["ops_24h"],
                "last_flood_at": row["last_flood_at"],
                "factors": _parse_factors(row.get("factors")),
                "computed_at": row.get("computed_at"),
            }
    except Exception as e:
        log.debug("physics_engine.get_account_risk: %s", e)
    return {
        "risk_score": 0.0,
        "ban_probability": 0.0,
        "flood_rate_1h": 0.0,
        "ops_24h": 0,
        "last_flood_at": None,
        "factors": [],
        "computed_at": None,
    }


def risk_label(score: float) -> str:
    if score < 0.25:
        return "🟢 Безопасен"
    if score < 0.5:
        return "🟡 Умеренный риск"
    if score < 0.75:
        return "🟠 Высокий риск"
    return "🔴 Критический"


def safe_ops_per_hour(risk_score: float, base: int = 60) -> int:
    """Estimate max safe ops/hour for a given risk score."""
    factor = max(0.05, 1.0 - risk_score)
    return max(1, int(base * factor))


# ─── Модель риска ─────────────────────────────────────────────────────────────
#
# Прежняя оценка смотрела в один журнал (`op_telemetry`) и ждала в нём исход
# «ban», которого не пишет никто: op_worker кладёт success/partial/failed, а
# flood_engine — flood_wait. Поэтому у всего флота стояло одно и то же «риск 0%,
# бан 5%» (сигмоида от −3), ограничения Telegram, спам-блок, траст, возраст и
# прокси в оценку не входили вовсе, а аккаунт без операций за 7 дней не
# пересчитывался никогда — старая высокая оценка держала его в охлаждении щита
# бесконечно.
#
# Теперь риск — это «шумное ИЛИ» понятных факторов: каждый фактор даёт вклад
# w·f (w — его вес, f — насколько он выражен, 0..1), а итог
# 1 − Π(1 − вклад). Так один сильный фактор сам поднимает риск высоко, слабые
# складываются, но не перешагивают единицу, и каждый вклад можно показать
# владельцу отдельной строкой «почему». Вероятность бана — монотонная функция
# риска с честной базой ~2% у чистого аккаунта.
#
# Сигналы берутся из тех же журналов, что сводит единый пульс
# (`infra_memory.get_account_health`): restriction_events, account_flood_log,
# acc_status, trust_score, и с тем же уважением к risk_cleared_at («владелец
# снял риск вручную»). Второй правды о здоровье здесь не заводится: это та же
# правда, разложенная на вклады.

# код → (вес, подпись для владельца)
RISK_FACTORS: dict[str, tuple[float, str]] = {
    "dead_status":   (0.97, "Статус аккаунта"),
    "severe":        (0.85, "Серьёзное ограничение Telegram"),
    "restrictions":  (0.40, "Предупреждения Telegram"),
    "floods_24h":    (0.40, "FloodWait за сутки"),
    "floods_7d":     (0.35, "FloodWait за 7 дней"),
    "long_flood":    (0.25, "Длинные паузы Telegram"),
    "failures":      (0.25, "Неудачные операции"),
    "load":          (0.30, "Перегрузка за сутки"),
    "young":         (0.30, "Молодой аккаунт"),
    "low_trust":     (0.30, "Низкое доверие"),
    "no_proxy":      (0.10, "Работает без прокси"),
}

# Суточный бюджет действий по «возрасту в системе»: свежий аккаунт в полном
# темпе — самая частая причина спам-блока.
_LOAD_BUDGET = ((7, 20), (30, 60))
_LOAD_BUDGET_MATURE = 150

# Вероятность бана из риска: logit = A + B·risk. Чистый аккаунт ~1.8%,
# риск 50% ≈ 30%, 70% ≈ 63%, 90% ≈ 87%.
_BAN_A = -4.0
_BAN_B = 6.0


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


def ban_probability_from_risk(risk: float) -> float:
    logit = max(-50.0, min(50.0, _BAN_A + _BAN_B * _clamp01(risk)))
    return 1.0 / (1.0 + math.exp(-logit))


def _plural_days(n: int) -> str:
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} день"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return f"{n} дня"
    return f"{n} дней"


def score_signals(sig: dict) -> dict:
    """Чистая функция: сигналы аккаунта → риск, вероятность бана и факторы.

    sig (все поля необязательны):
      acc_status, trust_score, has_proxy, age_days,
      severe_days_ago (дней с последнего серьёзного ограничения, None — не было),
      restrictions_7d, floods_24h, floods_7d, max_flood_s_7d,
      ops_24h, ops_7d, failed_7d.
    """
    from services import account_status as _acc_status

    intensities: dict[str, tuple[float, str]] = {}

    status = str(sig.get("acc_status") or "active").strip().lower()
    if _acc_status.is_dead(status):
        intensities["dead_status"] = (1.0, _acc_status.ru_label(status))

    sev = sig.get("severe_days_ago")
    if sev is not None:
        sev = float(sev)
        f = 1.0 if sev < 3 else 0.7 if sev < 7 else 0.4 if sev < 14 else 0.0
        if f:
            intensities["severe"] = (f, f"последнее — {_plural_days(int(sev))} назад"
                                     if sev >= 1 else "сегодня")

    restr = int(sig.get("restrictions_7d") or 0)
    if restr:
        intensities["restrictions"] = (min(restr / 3.0, 1.0), f"{restr} за 7 дней")

    fl24 = int(sig.get("floods_24h") or 0)
    if fl24:
        intensities["floods_24h"] = (min(fl24 / 3.0, 1.0), f"{fl24} за сутки")

    fl7 = int(sig.get("floods_7d") or 0)
    if fl7:
        intensities["floods_7d"] = (min(fl7 / 8.0, 1.0), f"{fl7} за 7 дней")

    max_s = int(sig.get("max_flood_s_7d") or 0)
    if max_s >= 300:
        mins = max_s // 60
        txt = f"до {mins // 60} ч {mins % 60} мин" if mins >= 60 else f"до {mins} мин"
        intensities["long_flood"] = (min(max_s / 3600.0, 1.0), txt)

    ops7 = int(sig.get("ops_7d") or 0)
    failed = int(sig.get("failed_7d") or 0)
    if ops7 >= 5 and failed:
        rate = failed / ops7
        if rate >= 0.1:
            intensities["failures"] = (min(rate * 2.0, 1.0),
                                       f"{round(rate * 100)}% из {ops7} за 7 дней")

    age = sig.get("age_days")
    ops24 = int(sig.get("ops_24h") or 0)
    budget = _LOAD_BUDGET_MATURE
    if age is not None:
        for limit_days, b in _LOAD_BUDGET:
            if float(age) < limit_days:
                budget = b
                break
    if ops24 > budget:
        intensities["load"] = (min((ops24 - budget) / budget, 1.0),
                               f"{ops24} действий при норме {budget}")

    if age is not None:
        a = float(age)
        f = 1.0 if a < 3 else 0.6 if a < 7 else 0.3 if a < 14 else 0.0
        if f:
            intensities["young"] = (f, f"в системе {_plural_days(int(a))}")

    trust = sig.get("trust_score")
    if trust is not None:
        try:
            t = float(trust)
        except (TypeError, ValueError):
            t = 1.0
        if t < 0.6:
            intensities["low_trust"] = ((0.6 - t) / 0.6, f"доверие {round(t * 100)}%")

    if sig.get("has_proxy") is False:
        intensities["no_proxy"] = (1.0, "общий IP сервера")

    factors = []
    keep = 1.0
    for code, (f, detail) in intensities.items():
        w, label = RISK_FACTORS[code]
        contrib = _clamp01(w * f)
        if contrib <= 0:
            continue
        keep *= (1.0 - contrib)
        factors.append({"code": code, "label": label, "detail": detail,
                        "impact": round(contrib, 4)})
    factors.sort(key=lambda x: -x["impact"])
    risk = _clamp01(1.0 - keep)
    return {
        "risk_score": round(risk, 4),
        "ban_probability": round(ban_probability_from_risk(risk), 4),
        "factors": factors,
    }


# ─── Score computation ────────────────────────────────────────────────────────

# Один проход на владельца: все сигналы всех его аккаунтов одним запросом.
_SIGNALS_SQL = """
WITH acc AS (
  SELECT a.id, COALESCE(a.acc_status, 'active') AS acc_status, a.trust_score,
         (a.proxy_id IS NOT NULL) AS has_proxy,
         EXTRACT(EPOCH FROM (NOW() - COALESCE(a.added_at, NOW()))) / 86400.0 AS age_days,
         a.risk_cleared_at
  FROM tg_accounts a
  WHERE a.owner_id = $1
    AND a.session_str IS NOT NULL AND a.session_str <> ''
),
re AS (
  SELECT r.account_id,
    COUNT(*) FILTER (WHERE r.created_at > NOW() - INTERVAL '7 days') AS restrictions_7d,
    MAX(r.created_at) FILTER (
      WHERE r.severity = 'critical' OR r.event_type ILIKE '%ban%'
         OR r.event_type ILIKE '%block%' OR r.event_type ILIKE '%restrict%'
    ) AS last_severe
  FROM restriction_events r
  JOIN acc a ON a.id = r.account_id
  WHERE r.created_at > NOW() - INTERVAL '14 days'
    AND (a.risk_cleared_at IS NULL OR r.created_at > a.risk_cleared_at)
  GROUP BY r.account_id
),
fl AS (
  SELECT f.account_id,
    COUNT(*) AS floods_7d,
    COUNT(*) FILTER (WHERE f.created_at > NOW() - INTERVAL '24 hours') AS floods_24h,
    COUNT(*) FILTER (WHERE f.created_at > NOW() - INTERVAL '1 hour') AS floods_1h,
    MAX(COALESCE(f.flood_seconds, 0)) AS max_flood_s_7d,
    MAX(f.created_at) AS last_flood_at
  FROM account_flood_log f
  JOIN acc a ON a.id = f.account_id
  WHERE f.created_at > NOW() - INTERVAL '7 days'
    AND (a.risk_cleared_at IS NULL OR f.created_at > a.risk_cleared_at)
  GROUP BY f.account_id
),
ops AS (
  SELECT t.account_id,
    COUNT(*) AS ops_7d,
    COUNT(*) FILTER (WHERE t.created_at > NOW() - INTERVAL '24 hours') AS ops_24h,
    COUNT(*) FILTER (WHERE t.created_at > NOW() - INTERVAL '1 hour') AS ops_1h,
    COUNT(*) FILTER (WHERE t.outcome = 'failed') AS failed_7d
  FROM op_telemetry t
  JOIN acc a ON a.id = t.account_id
  WHERE t.created_at > NOW() - INTERVAL '7 days'
    AND t.outcome <> 'flood_wait'
  GROUP BY t.account_id
)
SELECT a.id, a.acc_status, a.trust_score, a.has_proxy, a.age_days,
       EXTRACT(EPOCH FROM (NOW() - re.last_severe)) / 86400.0 AS severe_days_ago,
       COALESCE(re.restrictions_7d, 0) AS restrictions_7d,
       COALESCE(fl.floods_7d, 0) AS floods_7d,
       COALESCE(fl.floods_24h, 0) AS floods_24h,
       COALESCE(fl.floods_1h, 0) AS floods_1h,
       COALESCE(fl.max_flood_s_7d, 0) AS max_flood_s_7d,
       fl.last_flood_at,
       COALESCE(ops.ops_7d, 0) AS ops_7d,
       COALESCE(ops.ops_24h, 0) AS ops_24h,
       COALESCE(ops.ops_1h, 0) AS ops_1h,
       COALESCE(ops.failed_7d, 0) AS failed_7d
FROM acc a
LEFT JOIN re ON re.account_id = a.id
LEFT JOIN fl ON fl.account_id = a.id
LEFT JOIN ops ON ops.account_id = a.id
ORDER BY a.id
"""

_UPSERT_SQL = """INSERT INTO account_risk_scores
       (account_id, risk_score, ban_probability, flood_rate_1h, ops_24h,
        last_flood_at, factors, computed_at)
   VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb,NOW())
   ON CONFLICT (account_id) DO UPDATE
       SET risk_score=$2, ban_probability=$3, flood_rate_1h=$4,
           ops_24h=$5, last_flood_at=$6, factors=$7::jsonb, computed_at=NOW()"""


def _row_signals(r) -> dict:
    sev = r["severe_days_ago"]
    age = r["age_days"]
    return {
        "acc_status": r["acc_status"],
        "trust_score": r["trust_score"],
        "has_proxy": bool(r["has_proxy"]),
        "age_days": float(age) if age is not None else None,
        "severe_days_ago": float(sev) if sev is not None else None,
        "restrictions_7d": int(r["restrictions_7d"] or 0),
        "floods_24h": int(r["floods_24h"] or 0),
        "floods_7d": int(r["floods_7d"] or 0),
        "max_flood_s_7d": int(r["max_flood_s_7d"] or 0),
        "ops_24h": int(r["ops_24h"] or 0),
        "ops_7d": int(r["ops_7d"] or 0),
        "failed_7d": int(r["failed_7d"] or 0),
    }


async def compute_owner(pool: asyncpg.Pool, owner_id: int) -> int:
    """Пересчитать риск ВСЕХ аккаунтов владельца (с сессией). Возвращает число.

    Считаются все, а не только те, у кого были операции: иначе оценка
    аккаунта, переставшего работать, не остывает никогда.
    """
    rows = await pool.fetch(_SIGNALS_SQL, owner_id)
    n = 0
    for r in rows or []:
        res = score_signals(_row_signals(r))
        ops_1h = int(r["ops_1h"] or 0)
        floods_1h = int(r["floods_1h"] or 0)
        flood_rate_1h = floods_1h / max(ops_1h + floods_1h, 1)
        try:
            await pool.execute(
                _UPSERT_SQL, int(r["id"]), res["risk_score"],
                res["ban_probability"], round(flood_rate_1h, 4),
                int(r["ops_24h"] or 0), r["last_flood_at"],
                json.dumps(res["factors"], ensure_ascii=False),
            )
            n += 1
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.debug("physics_engine: upsert acc=%s: %s", r["id"], e)
    return n


async def _compute_one(pool: asyncpg.Pool, account_id: int) -> None:
    """Пересчитать одного (через владельца — запрос тот же)."""
    owner = await pool.fetchval("SELECT owner_id FROM tg_accounts WHERE id=$1", account_id)
    if owner is not None:
        await compute_owner(pool, int(owner))


# ─── Background worker ────────────────────────────────────────────────────────


async def run(pool: asyncpg.Pool, bot) -> None:
    log.info("Physics Engine started")
    while True:
        try:
            n = await recompute_all(pool)
            if n:
                log.info("Physics Engine: scored %d accounts", n)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error("Physics Engine loop error: %s", e)
        await asyncio.sleep(_LOOP_INTERVAL)


async def recompute_all(pool: asyncpg.Pool) -> int:
    owners = await pool.fetch(
        "SELECT DISTINCT owner_id FROM tg_accounts "
        "WHERE session_str IS NOT NULL AND session_str <> ''"
    )
    total = 0
    for o in owners or []:
        try:
            total += await compute_owner(pool, int(o["owner_id"]))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("physics_engine: owner=%s: %s", o["owner_id"], e)
        await asyncio.sleep(0.05)
    return total
