"""Autonomous Growth Agent — сервис управления целями роста.

Пользователь ставит цель (например, '+10K подписчиков в нише крипты за 30 дней'),
AI строит стратегию, ставит операции в очередь через Op Engine,
ежедневно корректирует курс и записывает отчёты.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Any

import asyncpg

from services import autonomous_engine, operation_bus

log = logging.getLogger(__name__)

# ── DB helpers ────────────────────────────────────────────────────────────────


async def create_goal(
    pool: asyncpg.Pool,
    owner_id: int,
    description: str,
    target_metric: str,
    target_value: int,
    deadline_days: int,
) -> int:
    """Создать новую цель роста. Возвращает goal_id."""
    deadline = datetime.now(timezone.utc) + timedelta(days=deadline_days)
    row = await pool.fetchrow(
        """
        INSERT INTO growth_goals
            (owner_id, description, target_metric, target_value, deadline_at, status)
        VALUES ($1, $2, $3, $4, $5, 'active')
        RETURNING id
        """,
        owner_id,
        description,
        target_metric,
        target_value,
        deadline,
    )
    goal_id: int = row["id"]
    log.info(
        "growth_agent: created goal_id=%d owner=%d metric=%s target=%d deadline=%s",
        goal_id, owner_id, target_metric, target_value, deadline.date(),
    )
    return goal_id


async def get_goal_status(pool: asyncpg.Pool, goal_id: int) -> dict[str, Any] | None:
    """Вернуть dict с прогрессом цели или None если не найдена."""
    row = await pool.fetchrow(
        "SELECT * FROM growth_goals WHERE id = $1",
        goal_id,
    )
    if not row:
        return None

    goal = dict(row)
    target = int(goal["target_value"] or 1)
    current = int(goal["current_value"] or 0)
    progress_pct = round(min(100.0, current / target * 100), 1)

    # Прогноз: сколько дней осталось до дедлайна
    now = datetime.now(timezone.utc)
    deadline = goal["deadline_at"]
    days_left: int | None = None
    if deadline:
        delta = deadline.replace(tzinfo=timezone.utc) - now
        days_left = max(0, delta.days)

    # Последние 5 действий
    actions = await pool.fetch(
        """
        SELECT action_type, description, outcome, delta_value, executed_at
        FROM growth_actions
        WHERE goal_id = $1
        ORDER BY executed_at DESC
        LIMIT 5
        """,
        goal_id,
    )

    # Последний отчёт
    report = await pool.fetchrow(
        """
        SELECT progress_pct, actions_count, delta_value, ai_commentary, report_date
        FROM growth_reports
        WHERE goal_id = $1
        ORDER BY report_date DESC
        LIMIT 1
        """,
        goal_id,
    )

    return {
        **goal,
        "progress_pct": progress_pct,
        "days_left": days_left,
        "recent_actions": [dict(a) for a in actions],
        "last_report": dict(report) if report else None,
    }


async def pause_goal(pool: asyncpg.Pool, goal_id: int, owner_id: int) -> bool:
    """Поставить цель на паузу. True если успешно."""
    result = await pool.execute(
        """
        UPDATE growth_goals
        SET status = 'paused', updated_at = NOW()
        WHERE id = $1 AND owner_id = $2 AND status = 'active'
        """,
        goal_id, owner_id,
    )
    return str(result).endswith("1")


async def resume_goal(pool: asyncpg.Pool, goal_id: int, owner_id: int) -> bool:
    """Возобновить цель. True если успешно."""
    result = await pool.execute(
        """
        UPDATE growth_goals
        SET status = 'active', updated_at = NOW()
        WHERE id = $1 AND owner_id = $2 AND status = 'paused'
        """,
        goal_id, owner_id,
    )
    return str(result).endswith("1")


async def delete_goal(pool: asyncpg.Pool, goal_id: int, owner_id: int) -> bool:
    """Удалить цель (CASCADE удалит actions и reports). True если успешно."""
    result = await pool.execute(
        "DELETE FROM growth_goals WHERE id = $1 AND owner_id = $2",
        goal_id, owner_id,
    )
    return str(result).endswith("1")


async def list_goals(
    pool: asyncpg.Pool,
    owner_id: int,
    status: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Список целей пользователя."""
    if status:
        rows = await pool.fetch(
            """
            SELECT * FROM growth_goals
            WHERE owner_id = $1 AND status = $2
            ORDER BY created_at DESC
            LIMIT $3
            """,
            owner_id, status, limit,
        )
    else:
        rows = await pool.fetch(
            """
            SELECT * FROM growth_goals
            WHERE owner_id = $1
            ORDER BY created_at DESC
            LIMIT $2
            """,
            owner_id, limit,
        )
    return [dict(r) for r in rows]


# ── Daily cycle ───────────────────────────────────────────────────────────────


async def run_daily_cycle(
    pool: asyncpg.Pool,
    bot: Any,
    owner_id: int,
    goal_id: int,
) -> dict[str, Any]:
    """Выполнить один цикл: построить контракт, поставить операции, записать результаты.

    Возвращает dict с итогами цикла.
    """
    goal_row = await pool.fetchrow("SELECT * FROM growth_goals WHERE id = $1", goal_id)
    if not goal_row:
        log.warning("growth_agent: goal_id=%d not found", goal_id)
        return {"ok": False, "error": "goal_not_found"}

    if goal_row["status"] not in ("active",):
        log.info("growth_agent: goal_id=%d status=%s — skip", goal_id, goal_row["status"])
        return {"ok": False, "error": "goal_not_active"}

    description = goal_row["description"]
    strategy_str = goal_row["strategy"] or "balanced"

    # Строим контракт через Autonomous Engine
    try:
        contract = await autonomous_engine.build_autonomous_contract(
            pool=pool,
            owner_id=owner_id,
            description=description,
            requested_strategy=strategy_str,  # type: ignore[arg-type]
        )
    except Exception as exc:
        log.warning("growth_agent: build_autonomous_contract failed goal=%d: %s", goal_id, exc)
        await _record_action(
            pool, goal_id, owner_id,
            action_type="plan",
            description=f"Ошибка планирования: {exc}",
            outcome="failed",
            delta_value=0,
        )
        return {"ok": False, "error": str(exc)}

    enriched = contract.enriched_plan()
    op_type = contract.resource_plan.get("op_type", "mass_publish")
    go = contract.risk_plan.get("go", False)

    # Определяем action_description для лога
    steps_summary = "; ".join(contract.execution_plan[:3]) if contract.execution_plan else description

    queued_op_id: int | None = None
    outcome = "skipped"

    if go and op_type in operation_bus.OP_REGISTRY:
        try:
            queued_op_id = await operation_bus.submit(
                pool=pool,
                owner_id=owner_id,
                op_type=op_type,
                params={
                    "goal_id": goal_id,
                    "description": description,
                    "autonomous_contract": {
                        "strategy": contract.strategy,
                        "intent_type": contract.intent_type,
                        "forecast": contract.forecast,
                        "primary_accounts": contract.resource_plan.get("primary_account_ids", []),
                    },
                    **enriched,
                },
                total_items=int(enriched.get("n_targets", 0)),
            )
            outcome = "queued"
            log.info(
                "growth_agent: goal=%d → op_id=%d op_type=%s",
                goal_id, queued_op_id, op_type,
            )
        except (ValueError, Exception) as exc:
            log.warning("growth_agent: submit failed goal=%d: %s", goal_id, exc)
            outcome = "failed"
    else:
        blockers = contract.risk_plan.get("blockers", [])
        reason = "; ".join(blockers) if blockers else f"op_type={op_type!r} не в реестре"
        log.info("growth_agent: goal=%d — skipped: %s", goal_id, reason)

    # Записываем действие
    await _record_action(
        pool, goal_id, owner_id,
        action_type=op_type,
        description=steps_summary,
        outcome=outcome,
        delta_value=0,
    )

    # Обновляем updated_at цели
    await pool.execute(
        "UPDATE growth_goals SET updated_at = NOW() WHERE id = $1",
        goal_id,
    )

    # Строим ежедневный отчёт
    target = int(goal_row["target_value"] or 1)
    current = int(goal_row["current_value"] or 0)
    progress_pct = round(min(100.0, current / target * 100), 1)

    forecast_txt = (
        f"Стратегия: {contract.strategy}. "
        f"Риск: {int(contract.forecast.get('risk_score', 0) * 100)}%. "
        f"Прогноз успеха: {int(contract.forecast.get('success_probability', 0) * 100)}%."
    )

    await _upsert_report(
        pool=pool,
        goal_id=goal_id,
        owner_id=owner_id,
        progress_pct=progress_pct,
        actions_count=1,
        delta_value=0,
        ai_commentary=forecast_txt,
    )

    # Уведомляем пользователя если операция поставлена в очередь
    if bot and queued_op_id and outcome == "queued":
        try:
            await bot.send_message(
                owner_id,
                f"🤖 <b>Growth Agent</b> — цикл выполнен\n\n"
                f"🎯 <b>Цель:</b> {description[:80]}\n"
                f"📊 <b>Прогресс:</b> {progress_pct}%\n"
                f"⚙️ <b>Операция</b> #{queued_op_id} поставлена в очередь\n"
                f"🔧 Тип: <code>{op_type}</code>\n"
                f"📝 {forecast_txt}",
                parse_mode="HTML",
            )
        except Exception as exc:
            log.warning("growth_agent: notify failed owner=%d: %s", owner_id, exc)

    return {
        "ok": True,
        "goal_id": goal_id,
        "op_id": queued_op_id,
        "outcome": outcome,
        "op_type": op_type,
        "progress_pct": progress_pct,
        "strategy": contract.strategy,
    }


# ── Background loop ───────────────────────────────────────────────────────────


async def run(pool: asyncpg.Pool, bot: Any) -> None:
    """Фоновый цикл: каждые 6 часов обрабатывает все активные цели."""
    log.info("growth_agent: background loop started (interval=6h)")
    while True:
        try:
            await _process_all_active_goals(pool, bot)
        except Exception as exc:
            log.error("growth_agent: loop iteration failed: %s", exc)
        await asyncio.sleep(6 * 3600)


async def _process_all_active_goals(pool: asyncpg.Pool, bot: Any) -> None:
    """Обработать все активные цели всех пользователей."""
    try:
        rows = await pool.fetch(
            """
            SELECT id, owner_id FROM growth_goals
            WHERE status = 'active'
            ORDER BY updated_at ASC NULLS FIRST
            LIMIT 100
            """
        )
    except Exception as exc:
        log.warning("growth_agent: fetch active goals failed: %s", exc)
        return

    log.info("growth_agent: processing %d active goals", len(rows))
    for row in rows:
        try:
            await run_daily_cycle(
                pool=pool,
                bot=bot,
                owner_id=int(row["owner_id"]),
                goal_id=int(row["id"]),
            )
            # Небольшая пауза между целями
            await asyncio.sleep(2)
        except Exception as exc:
            log.warning(
                "growth_agent: cycle failed goal=%d owner=%d: %s",
                row["id"], row["owner_id"], exc,
            )


# ── Internal helpers ──────────────────────────────────────────────────────────


async def _record_action(
    pool: asyncpg.Pool,
    goal_id: int,
    owner_id: int,
    action_type: str,
    description: str,
    outcome: str,
    delta_value: int,
) -> None:
    try:
        await pool.execute(
            """
            INSERT INTO growth_actions
                (goal_id, owner_id, action_type, description, outcome, delta_value, executed_at)
            VALUES ($1, $2, $3, $4, $5, $6, NOW())
            """,
            goal_id, owner_id, action_type, description, outcome, delta_value,
        )
    except Exception as exc:
        log.warning("growth_agent: _record_action failed goal=%d: %s", goal_id, exc)


async def _upsert_report(
    pool: asyncpg.Pool,
    goal_id: int,
    owner_id: int,
    progress_pct: float,
    actions_count: int,
    delta_value: int,
    ai_commentary: str,
) -> None:
    try:
        await pool.execute(
            """
            INSERT INTO growth_reports
                (goal_id, owner_id, report_date, progress_pct, actions_count, delta_value, ai_commentary)
            VALUES ($1, $2, CURRENT_DATE, $3, $4, $5, $6)
            ON CONFLICT (goal_id, report_date)
            DO UPDATE SET
                progress_pct   = EXCLUDED.progress_pct,
                actions_count  = growth_reports.actions_count + EXCLUDED.actions_count,
                delta_value    = growth_reports.delta_value  + EXCLUDED.delta_value,
                ai_commentary  = EXCLUDED.ai_commentary
            """,
            goal_id, owner_id, progress_pct, actions_count, delta_value, ai_commentary,
        )
    except Exception as exc:
        log.warning("growth_agent: _upsert_report failed goal=%d: %s", goal_id, exc)
