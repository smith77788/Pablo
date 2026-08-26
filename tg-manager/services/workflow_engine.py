"""Workflow Engine — automated multi-step operation orchestration.

Manages workflow definitions, execution, and status tracking.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Optional

import asyncpg

log = logging.getLogger(__name__)


async def init_workflow_tables(pool: asyncpg.Pool) -> None:
    """Create workflow tables."""
    await pool.execute('''
        CREATE TABLE IF NOT EXISTS workflow_definitions (
            id SERIAL PRIMARY KEY,
            owner_id BIGINT NOT NULL,
            name TEXT NOT NULL,
            description TEXT,
            steps JSONB NOT NULL DEFAULT '[]',
            is_active BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        );
    ''')
    await pool.execute('''
        CREATE TABLE IF NOT EXISTS workflow_runs (
            id SERIAL PRIMARY KEY,
            owner_id BIGINT NOT NULL,
            workflow_id INTEGER REFERENCES workflow_definitions(id),
            status TEXT DEFAULT 'pending',
            input_data JSONB DEFAULT '{}',
            output_data JSONB DEFAULT '{}',
            current_step INTEGER DEFAULT 0,
            total_steps INTEGER DEFAULT 0,
            error_message TEXT,
            started_at TIMESTAMPTZ,
            finished_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ DEFAULT NOW()
        );
    ''')
    log.info("Workflow tables initialized")


async def create_workflow(
    pool: asyncpg.Pool,
    owner_id: int,
    name: str,
    description: str = "",
    steps: list[dict] | None = None,
) -> dict:
    """Create a workflow definition."""
    try:
        row = await pool.fetchrow(
            '''INSERT INTO workflow_definitions (owner_id, name, description, steps)
               VALUES ($1, $2, $3, $4::jsonb)
               RETURNING id''',
            owner_id, name, description, json.dumps(steps or []))
        return {"ok": True, "id": row["id"]}
    except Exception as e:
        log.warning("create_workflow error: %s", e)
        return {"ok": False, "error": str(e)}


async def get_workflows(pool: asyncpg.Pool, owner_id: int) -> list:
    """Get all workflow definitions."""
    try:
        rows = await pool.fetch(
            'SELECT * FROM workflow_definitions WHERE owner_id = $1 ORDER BY name',
            owner_id)
        return [dict(r) for r in rows]
    except Exception as e:
        log.warning("get_workflows error: %s", e)
        return []


async def execute_workflow(
    pool: asyncpg.Pool,
    owner_id: int,
    workflow_id: int,
    input_data: dict | None = None,
) -> dict:
    """Execute a workflow run."""
    try:
        wf = await pool.fetchrow(
            'SELECT * FROM workflow_definitions WHERE id = $1 AND owner_id = $2',
            workflow_id, owner_id)
        if not wf:
            return {"ok": False, "error": "Workflow not found"}

        # steps — jsonb; без кодека asyncpg отдаёт СТРОКУ, и len() считал бы
        # символы: воркфлоу из двух шагов получал total_steps под сотню, а
        # прогресс на экране — бессмыслицу.
        _steps = wf.get("steps") or []
        if isinstance(_steps, str):
            try:
                _steps = json.loads(_steps)
            except Exception:
                _steps = []
        total_steps = len(_steps) if isinstance(_steps, list) else 0
        row = await pool.fetchrow(
            '''INSERT INTO workflow_runs
               (owner_id, workflow_id, status, input_data, current_step, total_steps, started_at)
               VALUES ($1, $2, 'running', $3::jsonb, 0, $4, NOW())
               RETURNING id''',
            owner_id, workflow_id, json.dumps(input_data or {}), total_steps)
        return {"ok": True, "run_id": row["id"], "total_steps": total_steps}
    except Exception as e:
        log.warning("execute_workflow error: %s", e)
        return {"ok": False, "error": str(e)}


async def get_workflow_status(
    pool: asyncpg.Pool,
    owner_id: int,
    run_id: int,
) -> Optional[dict]:
    """Get workflow run status."""
    try:
        row = await pool.fetchrow(
            '''SELECT wr.*, wd.name as workflow_name
               FROM workflow_runs wr
               LEFT JOIN workflow_definitions wd ON wd.id = wr.workflow_id
               WHERE wr.id = $1 AND wr.owner_id = $2''',
            run_id, owner_id)
        if not row:
            return None
        return dict(row)
    except Exception as e:
        log.warning("get_workflow_status error: %s", e)
        return None


async def _set_active(pool: asyncpg.Pool, owner_id: int, workflow_id: int,
                      active: bool) -> dict:
    """Включить/выключить воркфлоу. Нет такого у владельца → LookupError.

    Пауза — это `is_active`, ровно как в PATCH /workflows/{id}: два способа
    нажать одну кнопку не должны означать разное.
    """
    res = await pool.execute(
        "UPDATE workflow_definitions SET is_active=$1, updated_at=NOW() "
        "WHERE id=$2 AND owner_id=$3", active, workflow_id, owner_id)
    # asyncpg возвращает тег команды вида 'UPDATE 0' — ноль строк значит, что
    # воркфлоу либо не существует, либо принадлежит другому владельцу. Разницу
    # НЕ раскрываем: иначе по коду ответа можно перебирать чужие id.
    if isinstance(res, str) and res.rsplit(" ", 1)[-1] == "0":
        raise LookupError("Воркфлоу не найден")
    return {"active": active}


async def pause_workflow(pool: asyncpg.Pool, owner_id: int, workflow_id: int) -> dict:
    """Поставить воркфлоу на паузу (POST /workflow/{id}/pause)."""
    return await _set_active(pool, owner_id, workflow_id, False)


async def resume_workflow(pool: asyncpg.Pool, owner_id: int, workflow_id: int) -> dict:
    """Снять воркфлоу с паузы (POST /workflow/{id}/resume)."""
    return await _set_active(pool, owner_id, workflow_id, True)


async def delete_workflow(pool: asyncpg.Pool, owner_id: int, workflow_id: int) -> bool:
    """Удалить воркфлоу. False — не найден у этого владельца (обработчик даст 404).

    Незавершённые прогоны отменяем: строки workflow_runs ссылаются на
    определение внешним ключом, и без этого удаление упало бы, а с ON DELETE
    оставило бы «выполняющиеся» прогоны несуществующего воркфлоу.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "UPDATE workflow_runs SET status='cancelled', finished_at=NOW() "
                "WHERE workflow_id=$1 AND owner_id=$2 "
                "AND status IN ('pending','running')", workflow_id, owner_id)
            await conn.execute(
                "UPDATE workflow_runs SET workflow_id=NULL "
                "WHERE workflow_id=$1 AND owner_id=$2", workflow_id, owner_id)
            res = await conn.execute(
                "DELETE FROM workflow_definitions WHERE id=$1 AND owner_id=$2",
                workflow_id, owner_id)
    return not (isinstance(res, str) and res.rsplit(" ", 1)[-1] == "0")


async def cancel_workflow(
    pool: asyncpg.Pool,
    owner_id: int,
    run_id: int,
) -> dict:
    """Cancel a running workflow."""
    try:
        result = await pool.execute(
            '''UPDATE workflow_runs
               SET status = 'cancelled', finished_at = NOW()
               WHERE id = $1 AND owner_id = $2
                 AND status IN ('pending', 'running')''',
            run_id, owner_id)
        cancelled = "UPDATE 1" in result
        return {"ok": cancelled}
    except Exception as e:
        log.warning("cancel_workflow error: %s", e)
        return {"ok": False, "error": str(e)}
