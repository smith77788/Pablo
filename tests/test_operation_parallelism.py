from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "tg-manager"))


def test_start_command_does_not_cancel_queued_operations() -> None:
    source = (PROJECT_ROOT / "tg-manager/bot/handlers/start.py").read_text(
        encoding="utf-8"
    )

    assert "operation_bus.cancel" not in source
    assert "UPDATE operation_queue" not in source


def test_op_worker_claims_jobs_with_owner_parallelism_cap() -> None:
    source = (PROJECT_ROOT / "tg-manager/services/op_worker.py").read_text(
        encoding="utf-8"
    )

    assert "owner_running AS" in source
    assert "pending_locked AS" in source
    assert "candidate_window" in source
    assert "LIMIT $3" in source
    assert "running_count + owner_pending_rank <= $2" in source
    assert "_MAX_PARALLEL_PER_OWNER" in source


def test_running_operation_replay_is_owned_by_worker_only() -> None:
    main_source = (PROJECT_ROOT / "tg-manager/main.py").read_text(encoding="utf-8")
    worker_source = (PROJECT_ROOT / "tg-manager/services/op_worker.py").read_text(
        encoding="utf-8"
    )

    reset_sql = "SET status = 'pending', started_at = NULL"

    assert reset_sql not in main_source
    assert reset_sql in worker_source


def test_operation_account_locks_release_after_executor_error() -> None:
    os.environ.setdefault("MANAGER_BOT_TOKEN", "123:dummy")
    os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost/db")

    from services import op_worker

    async def exercise() -> None:
        async with op_worker._accounts_lock:
            op_worker._accounts_in_use.clear()
            op_worker._operation_account_locks.clear()

        claimed = await op_worker._claim_available_accounts(
            777,
            [{"id": 101}, {"id": 102}],
        )

        assert [acc["id"] for acc in claimed] == [101, 102]
        assert op_worker.is_account_in_use(101)
        assert op_worker.is_account_in_use(102)

        await op_worker.release_operation_accounts(777)

        assert not op_worker.is_account_in_use(101)
        assert not op_worker.is_account_in_use(102)

    asyncio.run(exercise())
