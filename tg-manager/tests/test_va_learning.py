"""Fixed-age measurements and evidence-consuming editorial learning."""
import json
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from services import channel_admin as ca
from services import va_learning as learning


def samples(pillar, scores, start=1):
    return [{"id": start + i, "pillar": pillar, "learning_views": score,
             "learning_reactions": 0, "learning_forwards": 0}
            for i, score in enumerate(scores)]


def test_learning_requires_three_new_posts_in_two_current_pillars():
    weights = {"a": 3, "b": 3}
    assert learning.decide(weights, samples("a", [100] * 10)) is None
    assert learning.decide(weights, samples("a", [100] * 3) + samples("b", [500] * 2, 20)) is None
    assert learning.decide(weights, samples("a", [100] * 3) + samples("removed", [500] * 3, 20)) is None


def test_equal_batches_and_median_ignore_a_single_viral_post():
    rows = samples("a", [100, 100, 100000, 50000]) + samples("b", [100] * 3, 20)
    decision = learning.decide({"a": 3, "b": 3}, rows)
    assert decision["weights"] == {"a": 3, "b": 3}
    assert decision["post_ids"] == [1, 2, 3, 20, 21, 22]
    assert "без изменений" in decision["explanation"]
    assert "не заявки и не продажи" in decision["explanation"]


@pytest.mark.parametrize("old, expected", [
    ({"a": 3, "b": 3}, {"a": 2, "b": 4}),
    ({"a": 1, "b": 10}, {"a": 1, "b": 10}),
])
def test_weights_move_at_most_one_step_and_keep_unobserved_pillars(old, expected):
    old["waiting"] = 5
    decision = learning.decide(old, samples("a", [100] * 3) + samples("b", [500] * 3, 20))
    assert decision["weights"] == {**expected, "waiting": 5}
    assert old["waiting"] == 5
    assert "24–30" in decision["explanation"]


def test_zero_reach_is_not_evidence_to_change_weights():
    decision = learning.decide({"a": 3, "b": 3}, samples("a", [0] * 3) + samples("b", [0] * 3, 20))
    assert decision["weights"] == {"a": 3, "b": 3}
    assert len(decision["post_ids"]) == 6


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [None, -1, True, "100", 1.5])
async def test_missing_or_invalid_counters_are_not_treated_as_measured_zero(bad):
    pool = AsyncMock()
    await learning.capture_sample(pool, 7, 8, 9, {"views": bad, "reactions": 0, "forwards": 0},
                                  datetime.now(UTC))
    pool.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_capture_uses_frozen_age_window_and_owner_scope():
    pool = AsyncMock()
    now = datetime.now(UTC)
    await learning.capture_sample(pool, 7, 8, 9, {"views": 100, "reactions": 2, "forwards": 1}, now)
    sql, *args = pool.execute.call_args.args
    assert args == [9, 7, "8", 100, 2, 1, now]
    assert "learning_sampled_at IS NULL" in sql
    assert "interval '24 hours'" in sql and "interval '30 hours'" in sql
    assert "owner_id=$2 AND channel_key=$3" in sql


def connection():
    conn = AsyncMock()
    tx = MagicMock()
    tx.__aenter__ = AsyncMock()
    tx.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=tx)
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    conn.fetchrow.side_effect = [{"auto_tune": True}, {
        "pillars": json.dumps(["a", "b"]), "mix_weights": json.dumps({"a": 3, "b": 3}),
    }]
    conn.fetch.return_value = samples("a", [100] * 3) + samples("b", [500] * 3, 20)
    return pool, conn, tx


@pytest.mark.asyncio
async def test_runtime_locks_policy_consumes_evidence_and_explains_in_one_transaction():
    pool, conn, tx = connection()
    assert await ca.autotune(pool, 7, 8) == {"a": 2, "b": 4}
    assert all("FOR UPDATE" in call.args[0] for call in conn.fetchrow.call_args_list)
    sql, owner, channel, pillars, cap = conn.fetch.call_args.args
    assert "learned_at IS NULL" in sql and "interval '30 days'" in sql
    assert (owner, channel, pillars, cap) == (7, "8", ["a", "b"], 10)
    writes = conn.execute.call_args_list
    assert len(writes) == 3
    assert "SET mix_weights=" in writes[0].args[0] and "brand_rules=" not in writes[0].args[0]
    assert writes[1].args[1:] == (7, "8", [1, 2, 3, 20, 21, 22])
    assert "va_admin_events" in writes[2].args[0]
    assert "Эти посты повторно" in writes[2].args[3]
    tx.__aexit__.assert_awaited_once_with(None, None, None)


@pytest.mark.asyncio
async def test_explanation_failure_rolls_back_weight_update_and_consumption():
    pool, conn, tx = connection()
    conn.execute.side_effect = ["UPDATE 1", "UPDATE 6", RuntimeError("event failed")]
    with pytest.raises(RuntimeError, match="event failed"):
        await learning.autotune(pool, 7, 8)
    assert tx.__aexit__.call_args.args[0] is RuntimeError


@pytest.mark.asyncio
async def test_disabled_auto_tune_does_not_read_or_consume_posts():
    pool, conn, _ = connection()
    conn.fetchrow.side_effect = [{"auto_tune": False}]
    assert await learning.autotune(pool, 7, 8) is None
    conn.fetch.assert_not_awaited()
    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_new_evidence_leaves_policy_untouched():
    pool, conn, _ = connection()
    conn.fetch.return_value = []
    assert await learning.autotune(pool, 7, 8) is None
    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_unchanged_decision_also_consumes_evidence():
    pool, conn, _ = connection()
    conn.fetch.return_value = samples("a", [100] * 3) + samples("b", [100] * 3, 20)
    assert await learning.autotune(pool, 7, 8) is None
    assert len(conn.execute.call_args_list) == 2
    assert "SET learned_at=" in conn.execute.call_args_list[0].args[0]


@pytest.mark.asyncio
async def test_stats_collection_captures_actual_snapshot_not_old_database_counters(monkeypatch):
    pool = AsyncMock()
    pool.fetch.return_value = [{"id": 1, "msg_id": 101}, {"id": 2, "msg_id": 102}]
    measured = {"views": 100, "reactions": 4, "forwards": 2}
    monkeypatch.setattr(ca, "snapshot", AsyncMock(return_value={"by_id": {101: measured}}))
    capture = AsyncMock()
    monkeypatch.setattr(learning, "capture_sample", capture)
    await ca.collect_stats(pool, 7, 8)
    capture.assert_awaited_once()
    assert capture.call_args.args[:5] == (pool, 7, 8, 1, measured)
    assert capture.call_args.args[5].tzinfo is not None


def test_report_explains_learning_only_when_enabled():
    node = shutil.which("node")
    if not node:
        pytest.skip("нужен Node.js")
    source = Path("mini_app/screens/va_admin.js").read_text(encoding="utf-8")
    function = source[source.index("function _vaReportHtml("):source.index("function _vaBriefHtml(")]
    script = "const esc=String, _vaNum=String, plural=()=>'';\n" + function + """
const report={members:null, members_delta_7d:null, posts_7d:0, posts_total:0};
const on=_vaReportHtml(report,{auto_tune:true});
const off=_vaReportHtml(report,{auto_tune:false});
if(!on.includes('24–30') || !on.includes('Причины решений доступны в журнале') ||
   off.includes('Обучение на новых постах')) process.exit(1);
"""
    result = subprocess.run([node, "-e", script], capture_output=True, text=True,
                            encoding="utf-8", check=False)
    assert result.returncode == 0, result.stderr
