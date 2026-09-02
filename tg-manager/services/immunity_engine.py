"""Ban Weather / Fleet Immune System — движок иммунитета флота.

Фаза 1: обработка событий смены статуса (account_status_events) →
снапшот окна признаков за 72ч, генерация автопсии и сигнатуры смерти.

Дизайн (см. docs/BAN_WEATHER_MODULE.md):
  • Захват смертей делает триггер БД (trg_immunity_capture_status) — здесь
    мы только ОБРАБАТЫВАЕМ уже записанные события, асинхронно.
  • Чистая логика (compute_signature / build_autopsy) отделена от БД —
    тестируется без PostgreSQL.
  • Fail-soft: любая ошибка обработки одного события не роняет цикл и НЕ
    влияет на исполнение операций (иммунитет — надстройка, не критический путь).

In-memory состояния нет — всё в БД, переживает рестарт и шарится между воркерами.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional

log = logging.getLogger(__name__)

DEATH_STATUSES = frozenset(
    {"banned", "spamblock", "deactivated", "session_expired", "frozen"}
)

_FEATURE_WINDOW_HOURS = 72

# ── Чистая логика (без БД) ───────────────────────────────────────────────────


def _warm_bucket(age_days: Optional[float]) -> str:
    if age_days is None:
        return "unknown"
    if age_days < 3:
        return "fresh"      # < 3 дней — самый хрупкий
    if age_days < 14:
        return "young"
    return "mature"


def _trust_bucket(score: Optional[float]) -> str:
    """Бакет доверия. Шкала trust_score в проекте — 0.1..1.0 (не 0..100):
    пороги 0.3/0.7 совпадают с остальным кодом (account_warmer, infra_advisor,
    health_dashboard). Раньше здесь стояли 40/70 — любой аккаунт попадал в
    'low', и сигнатура смерти теряла различающую силу по доверию.
    """
    if score is None:
        return "unknown"
    if score < 0.3:
        return "low"
    if score < 0.7:
        return "mid"
    return "high"


def _op_mix(op_counts: dict[str, int], top: int = 2) -> str:
    """Топ-N типов операций по частоте — детерминированная строка."""
    if not op_counts:
        return "idle"
    ordered = sorted(op_counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return "+".join(k for k, _ in ordered[:top])


def compute_signature(features: dict[str, Any]) -> str:
    """Детерминированная сигнатура смерти для группировки исходов.

    op_mix × geo × warming_bucket × trust_bucket. Чистая функция — один и тот же
    вход даёт одну и ту же строку (нужно для дедупа/роллапов/правил)."""
    op_mix = _op_mix(features.get("op_counts") or {})
    geo = (features.get("geo_country") or "??").strip() or "??"
    warm = _warm_bucket(features.get("warming_age_days"))
    trust = _trust_bucket(features.get("trust_score"))
    return f"{op_mix}|geo={geo}|warm={warm}|trust={trust}"


def _probable_cause(features: dict[str, Any]) -> str:
    """Эвристическая вероятная причина смерти из признаков окна."""
    actions = int(features.get("actions_72h") or 0)
    err_rate = float(features.get("error_rate") or 0.0)
    warm = _warm_bucket(features.get("warming_age_days"))
    proxy_dead = bool(features.get("proxy_dead"))
    op_counts = features.get("op_counts") or {}
    invites = sum(v for k, v in op_counts.items() if "invite" in k)
    rate_per_h = actions / _FEATURE_WINDOW_HOURS if actions else 0.0

    if warm == "fresh" and actions > 30:
        return "Высокая активность на «свежем» непрогретом аккаунте (< 3 дней)"
    if invites > 20:
        return f"Массовые инвайты ({invites} за 72ч) — типичный триггер спамблока"
    if rate_per_h > 8:
        return f"Слишком высокий темп действий (~{rate_per_h:.1f}/ч)"
    if err_rate > 0.4:
        return f"Высокая доля ошибок операций ({err_rate:.0%}) перед смертью"
    if proxy_dead:
        return "Прокси аккаунта был мёртв/нестабилен перед баном"
    return "Явного одиночного триггера не выделено — вероятна кумулятивная нагрузка"


def _suggested_rule(features: dict[str, Any], signature: str) -> dict[str, Any]:
    """Предлагаемое правило «не повторять» на основе признаков."""
    op_counts = features.get("op_counts") or {}
    dominant = _op_mix(op_counts, top=1)
    match: dict[str, Any] = {"signature": signature}
    if dominant and dominant != "idle":
        match["op_type"] = dominant
    if features.get("geo_country"):
        match["geo_country"] = features["geo_country"]
    warm = _warm_bucket(features.get("warming_age_days"))
    action = "quarantine" if warm == "fresh" else "throttle"
    return {
        "match": match,
        "action": action,
        "note": f"Авто-предложение из автопсии: {_probable_cause(features)}",
    }


def build_autopsy(
    event: dict[str, Any],
    features: dict[str, Any],
    survivors: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Собрать отчёт вскрытия (чистая функция, без БД).

    event — строка account_status_events (acc_id, new_status, ...).
    features — снапшот окна (_gather_features).
    survivors — агрегат по выжившим с той же гео/прокси (для сравнения)."""
    survivors = survivors or {}
    actions = int(features.get("actions_72h") or 0)
    signature = compute_signature(features)
    op_counts = features.get("op_counts") or {}

    diff = None
    surv_med = survivors.get("median_actions_72h")
    if surv_med is not None and surv_med >= 0:
        if actions > surv_med * 1.5 and actions > 10:
            diff = (f"Делал ~{actions} действий за 72ч против медианы {surv_med:.0f} "
                    f"у выживших с той же гео/прокси — перегрузка")
        elif actions <= surv_med:
            diff = "Активность не выделялась на фоне выживших — причина не в объёме"

    dominant = _op_mix(op_counts, top=1)
    summary = (
        f"Аккаунт #{event.get('acc_id')} → {event.get('new_status')}. "
        f"За 72ч: {actions} действий"
        + (f", преобладал «{dominant}»" if dominant and dominant != "idle" else "")
        + f". Гео: {features.get('geo_country') or '—'}, "
        f"возраст: {_warm_bucket(features.get('warming_age_days'))}, "
        f"trust: {features.get('trust_score') if features.get('trust_score') is not None else '—'}."
    )

    return {
        "signature": signature,
        "summary": summary,
        "probable_cause": _probable_cause(features),
        "differed_from_survivors": diff,
        "suggested_rule": _suggested_rule(features, signature),
        "features": features,
    }


# ── Слой БД ──────────────────────────────────────────────────────────────────


async def _gather_features(
    pool, acc_id: int, owner_id: Optional[int], at
) -> dict[str, Any]:
    """Снапшот окна признаков за 72ч до смерти. Fail-soft: недоступное поле → None."""
    from services.logger import log_exc_swallow

    feats: dict[str, Any] = {"op_counts": {}, "actions_72h": 0}
    try:
        acc = await pool.fetchrow(
            """SELECT a.acc_status, a.trust_score,
                      EXTRACT(EPOCH FROM (now() - a.added_at)) / 86400.0 AS age_days,
                      p.geo_country, p.is_alive AS proxy_alive
               FROM tg_accounts a
               LEFT JOIN user_proxies p ON p.id = a.proxy_id
               WHERE a.id = $1""",
            acc_id,
        )
        if acc:
            feats["trust_score"] = acc["trust_score"]
            feats["warming_age_days"] = (
                float(acc["age_days"]) if acc["age_days"] is not None else None
            )
            feats["geo_country"] = acc["geo_country"]
            feats["proxy_dead"] = (acc["proxy_alive"] is False)
    except Exception:
        log_exc_swallow(log, "immunity: gather account features failed")

    try:
        rows = await pool.fetch(
            """SELECT q.op_type,
                      COUNT(*) AS c,
                      COUNT(*) FILTER (WHERE l.status = 'error') AS errors
               FROM operation_log l
               JOIN operation_queue q ON q.id = l.op_id
               WHERE l.target = $1
                 AND l.created_at > $2::timestamptz - INTERVAL '72 hours'
                 AND l.created_at <= $2::timestamptz
               GROUP BY q.op_type""",
            str(acc_id), at,
        )
        total = 0
        errors = 0
        for r in rows:
            feats["op_counts"][r["op_type"]] = int(r["c"])
            total += int(r["c"])
            errors += int(r["errors"] or 0)
        feats["actions_72h"] = total
        feats["error_rate"] = (errors / total) if total else 0.0
    except Exception:
        log_exc_swallow(log, "immunity: gather activity window failed")

    return feats


async def _survivor_stats(pool, owner_id: Optional[int], geo: Optional[str]) -> dict[str, Any]:
    """Медиана действий за 72ч у ЖИВЫХ аккаунтов той же гео (для сравнения)."""
    from services.logger import log_exc_swallow
    try:
        row = await pool.fetchrow(
            """SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY cnt) AS med
               FROM (
                 SELECT a.id, COUNT(l.*) AS cnt
                 FROM tg_accounts a
                 LEFT JOIN user_proxies p ON p.id = a.proxy_id
                 LEFT JOIN operation_log l ON l.target = a.id::text
                      AND l.created_at > now() - INTERVAL '72 hours'
                 WHERE a.owner_id = $1 AND a.is_active = TRUE
                   AND a.acc_status = 'active'
                   AND ($2::text IS NULL OR p.geo_country = $2)
                 GROUP BY a.id
               ) t""",
            owner_id, geo,
        )
        return {"median_actions_72h": float(row["med"]) if row and row["med"] is not None else None}
    except Exception:
        log_exc_swallow(log, "immunity: survivor stats failed")
        return {"median_actions_72h": None}


async def process_event(pool, event: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Обработать одно событие: снапшот → автопсия/сигнатура → запись в строку."""
    from services.logger import log_exc_swallow
    try:
        feats = await _gather_features(
            pool, event["acc_id"], event.get("owner_id"), event["created_at"]
        )
        signature = compute_signature(feats)
        autopsy = None
        if event.get("is_death"):
            surv = await _survivor_stats(pool, event.get("owner_id"), feats.get("geo_country"))
            autopsy = build_autopsy(event, feats, surv)
        await pool.execute(
            """UPDATE account_status_events
               SET signature = $2, autopsy = $3::jsonb, processed_at = now()
               WHERE id = $1""",
            event["id"], signature,
            json.dumps(autopsy, ensure_ascii=False) if autopsy else None,
        )
        return autopsy
    except Exception:
        log_exc_swallow(log, f"immunity: process_event id={event.get('id')} failed")
        # помечаем обработанным, чтобы не зациклиться на битом событии
        try:
            await pool.execute(
                "UPDATE account_status_events SET processed_at = now() WHERE id = $1",
                event["id"],
            )
        except Exception:
            pass
        return None


async def process_pending(pool, limit: int = 100) -> int:
    """Обработать пачку необработанных событий. Возвращает число обработанных."""
    from services.logger import log_exc_swallow
    try:
        rows = await pool.fetch(
            """SELECT id, acc_id, owner_id, old_status, new_status, is_death, created_at
               FROM account_status_events
               WHERE processed_at IS NULL
               ORDER BY created_at
               LIMIT $1""",
            limit,
        )
    except Exception:
        log_exc_swallow(log, "immunity: fetch pending failed")
        return 0
    n = 0
    for r in rows:
        await process_event(pool, dict(r))
        n += 1
    return n


async def run_once(pool) -> int:
    """Один проход движка (обработка ожидающих событий)."""
    return await process_pending(pool)


async def start(pool, interval_sec: int = 120) -> None:
    """Фоновый цикл. Регистрируется из main.py (Фаза 4). Fail-soft."""
    from services.logger import log_exc_swallow
    log.info("immunity_engine: старт фонового цикла (interval=%ds)", interval_sec)
    while True:
        try:
            processed = await run_once(pool)
            if processed:
                log.info("immunity_engine: обработано событий: %d", processed)
        except Exception:
            log_exc_swallow(log, "immunity_engine: цикл упал (продолжаем)")
        await asyncio.sleep(interval_sec)
