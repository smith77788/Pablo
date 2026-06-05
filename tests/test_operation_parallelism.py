from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


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
