"""Compliance Engine — cryptographically signed audit trail for operations.

Every significant operation is recorded with an HMAC-SHA256 signature,
providing tamper-evident proof of what was done, when, and with what outcome.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
from datetime import datetime, timezone

import asyncpg

log = logging.getLogger(__name__)


def _secret() -> bytes:
    s = os.getenv("COMPLIANCE_SECRET") or os.getenv("ADMIN_SECRET") or "botmother-compliance"
    return s.encode()


def _sign(op_id: int | None, op_type: str, outcome: str, ts: float) -> str:
    """HMAC-SHA256 over key operation fields."""
    payload = f"{op_id}:{op_type}:{outcome}:{int(ts)}".encode()
    return hmac.new(_secret(), payload, hashlib.sha256).hexdigest()


def _hash_params(params: dict | None) -> str | None:
    if not params:
        return None
    try:
        serialized = json.dumps(params, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(serialized.encode()).hexdigest()[:16]
    except Exception:
        return None


# ─── Словарь исходов ─────────────────────────────────────────────────────────
#
# В `outcome` попадает НЕ абстрактный «успех/ошибка», а то, что пишут вызывающие:
# исполнитель операций кладёт статус операции (`op_status.DONE` / `PARTIAL` /
# `FAILED` / `CANCELLED`), проверка контента — `blocked`, запуск массовых жалоб
# — `abuse_report` (это вообще не исход, а запись правового основания).
#
# До 05.10.2026 и экран мини-аппа, и отчёт в боте считали `success`, `ban` и
# `flood_wait` — ни одного из них не пишет никто. Поэтому «Успешных»,
# «Рисковых» и «Успешность» были нулями ВСЕГДА, при любом журнале, а
# подписанный отчёт для аудита выдавал «Успешных: 0 (0.0%)» при сотнях
# выполненных операций. Словарь теперь один и живёт здесь: оба интерфейса
# читают его, а не держат свои копии, которые уже однажды разъехались.
OK_OUTCOMES = ("done", "success", "ok")
PARTIAL_OUTCOMES = ("partial",)
RISK_OUTCOMES = ("failed", "error", "timeout", "ban", "flood_wait", "blocked")
# Нейтральные: к качеству работы продукта отношения не имеют. Отмена — решение
# владельца, правовое основание — запись о причине, а не о результате. В
# успешность они не входят ни числителем, ни знаменателем: иначе девять записей
# основания превращали бы одну выполненную кампанию в «10% успеха».
NEUTRAL_OUTCOMES = ("cancelled", "abuse_report", "unknown")

_GROUPS = {
    "ok": OK_OUTCOMES,
    "partial": PARTIAL_OUTCOMES,
    "risk": RISK_OUTCOMES,
    "neutral": NEUTRAL_OUTCOMES,
}
_GROUP_OF = {o: g for g, outs in _GROUPS.items() for o in outs}

# Подписи для владельца — только по-русски: он английского не знает, а эти
# строки идут прямо на экран и в выгружаемый отчёт.
OUTCOME_RU = {
    "done": "✓ Выполнено",
    "success": "✓ Выполнено",
    "ok": "✓ Выполнено",
    "partial": "◐ Частично",
    "failed": "✕ Не прошло",
    "error": "✕ Ошибка",
    "timeout": "⏱ Telegram не ответил",
    "ban": "⛔ Бан",
    "flood_wait": "⏳ Лимит Telegram",
    "blocked": "🚫 Остановлено правилами",
    "cancelled": "⊘ Отменено владельцем",
    "abuse_report": "⚖️ Основание жалобы",
    "unknown": "· Без исхода",
}

GROUP_RU = {
    "ok": "Выполнено",
    "partial": "Частично",
    "risk": "Рисковых",
    "neutral": "Прочее",
}


def classify_outcome(outcome: str | None) -> str:
    """Группа исхода: ok / partial / risk / neutral, либо unknown."""
    return _GROUP_OF.get(str(outcome or "").strip().lower(), "unknown")


def outcome_ru(outcome: str | None) -> str:
    """Русская подпись исхода. Неизвестный отдаём как есть — молчать хуже."""
    key = str(outcome or "").strip().lower()
    return OUTCOME_RU.get(key) or (outcome or "—")


def group_outcomes(group: str | None) -> list[str]:
    """Значения `outcome`, попадающие в группу (для запроса со срезом)."""
    return list(_GROUPS.get(str(group or "").strip().lower(), ()))


def summarize(counts: dict) -> dict:
    """Свести счётчики по исходам в числа экрана. Чистая функция.

    Считать в SQL нельзя: словарь тогда живёт в тексте запроса и расходится с
    тем, что пишут вызывающие, — именно так экран и получил свои нули. SQL
    отдаёт сырые `outcome → сколько`, группы считаются здесь и проверяются
    тестом без базы.
    """
    groups = {"ok": 0, "partial": 0, "risk": 0, "neutral": 0, "unknown": 0}
    by_outcome: dict[str, int] = {}
    total = 0
    for outcome, n in (counts or {}).items():
        try:
            n = int(n or 0)
        except (TypeError, ValueError):
            continue
        key = str(outcome or "unknown").strip().lower()
        by_outcome[key] = by_outcome.get(key, 0) + n
        groups[classify_outcome(key)] = groups[classify_outcome(key)] + n
        total += n
    # Знаменатель успешности — только то, что говорит о качестве работы.
    judged = groups["ok"] + groups["partial"] + groups["risk"]
    out = dict(groups)
    out["total"] = total
    out["judged"] = judged
    out["success_rate"] = round(groups["ok"] / judged * 100, 1) if judged else 0.0
    out["by_outcome"] = by_outcome
    return out


# ─── Record ──────────────────────────────────────────────────────────────────


async def record(
    pool: asyncpg.Pool,
    user_id: int | None,
    account_id: int | None,
    op_type: str,
    outcome: str,
    op_id: int | None = None,
    params: dict | None = None,
) -> None:
    """Write one compliance entry. Never raises."""
    try:
        ts  = datetime.now(timezone.utc).timestamp()
        sig = _sign(op_id, op_type, outcome, ts)
        ph  = _hash_params(params)
        await pool.execute(
            """INSERT INTO compliance_audit
               (user_id, account_id, op_type, op_id, params_hash, outcome, hmac_sig)
               VALUES ($1,$2,$3,$4,$5,$6,$7)""",
            user_id,
            account_id,
            op_type,
            op_id,
            ph,
            outcome,
            sig,
        )
    except Exception as e:
        log.debug("compliance_engine.record: %s", e)


# ─── Reporting ────────────────────────────────────────────────────────────────


async def get_report(pool: asyncpg.Pool, user_id: int, days: int = 30) -> dict:
    """Сводка журнала за период. Никогда не бросает.

    SQL отдаёт сырое «исход → сколько», группы считает `summarize`: словарь
    исходов обязан быть один, и в тексте запроса его быть не должно.
    """
    try:
        rows = await pool.fetch(
            """SELECT outcome, COUNT(*) AS n
               FROM compliance_audit
               WHERE user_id=$1
                 AND created_at > NOW() - ($2 || ' days')::INTERVAL
               GROUP BY outcome""",
            user_id,
            str(days),
        )
        extra = await pool.fetchrow(
            """SELECT COUNT(DISTINCT op_type)    AS distinct_types,
                      COUNT(DISTINCT op_id)      AS distinct_ops,
                      MIN(created_at)            AS first_op,
                      MAX(created_at)            AS last_op
               FROM compliance_audit
               WHERE user_id=$1
                 AND created_at > NOW() - ($2 || ' days')::INTERVAL""",
            user_id,
            str(days),
        )
        report = summarize({r["outcome"]: r["n"] for r in (rows or [])})
        if not report["total"]:
            return {}
        ex = dict(extra) if extra else {}
        report.update({
            "distinct_types": int(ex.get("distinct_types") or 0),
            # account_id в журнале не заполняет ни один вызывающий: запись
            # пишется на уровне ОПЕРАЦИИ, а операция идёт по многим аккаунтам.
            # Поэтому «Аккаунтов задействовано» показывало ноль всегда — такую
            # строку владельцу лучше не показывать вовсе, чем врать нулём.
            "distinct_ops": int(ex.get("distinct_ops") or 0),
            "first_op": ex.get("first_op"),
            "last_op": ex.get("last_op"),
            "days": days,
        })
        return report
    except Exception as e:
        log.debug("compliance_engine.get_report: %s", e)
        return {}


async def get_totals(pool: asyncpg.Pool, user_id: int) -> dict:
    """То же, но за всё время — шапка экрана. Никогда не бросает."""
    try:
        rows = await pool.fetch(
            "SELECT outcome, COUNT(*) AS n FROM compliance_audit "
            "WHERE user_id=$1 GROUP BY outcome",
            user_id,
        )
        return summarize({r["outcome"]: r["n"] for r in (rows or [])})
    except Exception as e:
        log.debug("compliance_engine.get_totals: %s", e)
        return summarize({})


async def get_recent(
    pool: asyncpg.Pool,
    user_id: int,
    limit: int = 20,
    offset: int = 0,
    group: str | None = None,
) -> list[dict]:
    """Записи журнала, новые сверху. Никогда не бросает.

    `op_alive` — жива ли ещё операция в очереди. Без этого признака экран не
    может решить, вести ли по записи к операции: кнопка «Очистить завершённые»
    удаляет операции из очереди, а подписанные записи аудита остаются (они и
    должны оставаться — журнал неизменяемый). Тап по такой записи уводил бы в
    ошибку «операция не найдена».
    """
    try:
        where = "a.user_id=$1"
        args: list = [user_id, limit, offset]
        outs = group_outcomes(group)
        if outs:
            where += " AND a.outcome = ANY($4::text[])"
            args.append(outs)
        rows = await pool.fetch(
            f"""SELECT a.op_type, a.outcome, a.op_id, a.account_id, a.created_at,
                       (a.hmac_sig IS NOT NULL AND a.hmac_sig <> '') AS signed,
                       (q.id IS NOT NULL) AS op_alive
                FROM compliance_audit a
                LEFT JOIN operation_queue q
                       ON q.id = a.op_id AND q.owner_id = a.user_id
                WHERE {where}
                ORDER BY a.created_at DESC
                LIMIT $2 OFFSET $3""",
            *args,
        )
        out = []
        for r in rows or []:
            d = dict(r)
            d["outcome_ru"] = outcome_ru(d.get("outcome"))
            d["group"] = classify_outcome(d.get("outcome"))
            out.append(d)
        return out
    except Exception as e:
        log.debug("compliance_engine.get_recent: %s", e)
        return []


async def count_recent(
    pool: asyncpg.Pool, user_id: int, group: str | None = None
) -> int:
    """Сколько записей под срезом — чтобы «Показать ещё» знал остаток."""
    try:
        outs = group_outcomes(group)
        if outs:
            return int(await pool.fetchval(
                "SELECT COUNT(*) FROM compliance_audit "
                "WHERE user_id=$1 AND outcome = ANY($2::text[])",
                user_id, outs) or 0)
        return int(await pool.fetchval(
            "SELECT COUNT(*) FROM compliance_audit WHERE user_id=$1",
            user_id) or 0)
    except Exception as e:
        log.debug("compliance_engine.count_recent: %s", e)
        return 0


async def export_text(pool: asyncpg.Pool, user_id: int, days: int = 30) -> str:
    """Текстовый отчёт соответствия за период. Никогда не бросает.

    Отчёт читает владелец и отдаёт его дальше, поэтому он целиком по-русски:
    английская шапка в документе, который человек не читает по-английски, —
    это тот же немой экран, только в буфере обмена.
    """
    report = await get_report(pool, user_id, days)
    if not report:
        return "Нет данных за указанный период."

    def _pct(v: float) -> str:
        return f"{v}".replace(".", ",")

    lines = [
        f"ОТЧЁТ СООТВЕТСТВИЯ — {days} дней",
        "",
        f"Всего записей: {report['total']}",
        f"Выполнено: {report['ok']} ({_pct(report['success_rate'])}% от оценённых)",
        f"Частично: {report['partial']}",
        f"Рисковых: {report['risk']}",
        f"Прочее (отмены, основания): {report['neutral']}",
        f"Типов операций: {report['distinct_types']}",
        f"Операций в журнале: {report['distinct_ops']}",
    ]
    by = report.get("by_outcome") or {}
    if by:
        lines.append("")
        lines.append("Разбивка по исходам:")
        for outcome, n in sorted(by.items(), key=lambda kv: -kv[1]):
            lines.append(f"  {outcome_ru(outcome)} — {n}")
    if report.get("first_op"):
        lines.append("")
        lines.append(f"Первая запись: {report['first_op'].strftime('%d.%m.%Y %H:%M')}")
    if report.get("last_op"):
        lines.append(f"Последняя запись: {report['last_op'].strftime('%d.%m.%Y %H:%M')}")
    lines.append("")
    lines.append("Каждая запись подписана HMAC-SHA256 — подделать журнал нельзя.")
    return "\n".join(lines)
