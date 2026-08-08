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
    """Aggregate stats for user's audit log. Never raises."""
    try:
        row = await pool.fetchrow(
            """SELECT
               COUNT(*) FILTER (WHERE outcome='success')  AS ok,
               COUNT(*) FILTER (WHERE outcome='error')    AS errors,
               COUNT(*) FILTER (WHERE outcome='flood_wait') AS floods,
               COUNT(*) FILTER (WHERE outcome='ban')      AS bans,
               COUNT(*)                                   AS total,
               COUNT(DISTINCT op_type)                    AS distinct_types,
               COUNT(DISTINCT account_id)                 AS distinct_accounts,
               MIN(created_at)                            AS first_op,
               MAX(created_at)                            AS last_op
               FROM compliance_audit
               WHERE user_id=$1
                 AND created_at > NOW() - ($2 || ' days')::INTERVAL""",
            user_id,
            str(days),
        )
        if not row:
            return {}
        total = int(row["total"] or 0)
        ok    = int(row["ok"] or 0)
        return {
            "total": total,
            "ok": ok,
            "errors": int(row["errors"] or 0),
            "floods": int(row["floods"] or 0),
            "bans": int(row["bans"] or 0),
            "success_rate": round(ok / max(total, 1) * 100, 1),
            "distinct_types": int(row["distinct_types"] or 0),
            "distinct_accounts": int(row["distinct_accounts"] or 0),
            "first_op": row["first_op"],
            "last_op": row["last_op"],
            "days": days,
        }
    except Exception as e:
        log.debug("compliance_engine.get_report: %s", e)
        return {}


async def get_recent(
    pool: asyncpg.Pool,
    user_id: int,
    limit: int = 20,
    offset: int = 0,
) -> list[dict]:
    """Recent audit entries for a user. Never raises."""
    try:
        rows = await pool.fetch(
            """SELECT op_type, outcome, op_id, account_id, created_at
               FROM compliance_audit
               WHERE user_id=$1
               ORDER BY created_at DESC
               LIMIT $2 OFFSET $3""",
            user_id,
            limit,
            offset,
        )
        return [dict(r) for r in rows]
    except Exception as e:
        log.debug("compliance_engine.get_recent: %s", e)
        return []


async def export_text(pool: asyncpg.Pool, user_id: int, days: int = 30) -> str:
    """Generate plain-text compliance report. Never raises."""
    report = await get_report(pool, user_id, days)
    if not report:
        return "Нет данных за указанный период."
    lines = [
        f"COMPLIANCE REPORT — {days} дней",
        f"Всего операций: {report['total']}",
        f"Успешных: {report['ok']} ({report['success_rate']}%)",
        f"Ошибок: {report['errors']}",
        f"FloodWait: {report['floods']}",
        f"Банов: {report['bans']}",
        f"Типов операций: {report['distinct_types']}",
        f"Аккаунтов задействовано: {report['distinct_accounts']}",
    ]
    if report.get("first_op"):
        lines.append(f"Первая операция: {report['first_op'].strftime('%d.%m.%Y %H:%M')}")
    if report.get("last_op"):
        lines.append(f"Последняя операция: {report['last_op'].strftime('%d.%m.%Y %H:%M')}")
    return "\n".join(lines)
