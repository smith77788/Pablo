"""Агент роста повторяет пост там, где прошлый жил и читался, — и только там.

НАХОДКА. Группа отрабатывалась один раз навсегда: `niche_growth_targets`
исключала из поиска всё, где агент был, включая группы, где пост живёт
неделями и набирает просмотры. Охват каждого запуска держался только на
новых, то есть непроверенных, группах, а доказанные простаивали.

ВТОРАЯ НАХОДКА (рядом). Пометка новой группы писалась
`INSERT ... ON CONFLICT DO NOTHING` и считалась удачной при любом ответе, кроме
ошибки. «INSERT 0 0» — строку уже поставил другой запуск, то есть группа занята
им, — тоже открывал пост: два параллельных запуска постили в одну группу.

ОБРАТНЫЙ КОНТРОЛЬ (прогонялся):
  1. убрать `AND alive` из выборки -> красный test_only_proven_groups_come_back;
  2. claim без условия (`outcome='posted' AND alive AND срок`) -> красный
     test_claim_is_taken_once_postgres;
  3. вернуть `!= "ERROR"` у пометки -> красный
     test_executor_trusts_only_a_real_mark.
"""
from __future__ import annotations

import ast
import asyncio
import pathlib


from services import growth_learning as gl
from tests.test_growth_agent_learns_from_outcomes import pool  # noqa: F401 — стенд

OWNER = 990_782


def test_repost_takes_at_most_half_of_a_run():
    assert gl.repost_slots(5) == 2
    assert gl.repost_slots(10) == 5
    assert gl.repost_slots(1) == 0, "единственное место запуска — разведке"
    assert gl.REPOST_AFTER_DAYS >= 7


async def _seed(p):
    await p.execute("DELETE FROM niche_growth_targets WHERE owner_id=$1", OWNER)
    rows = [
        # gid, keyword, niche, username, outcome, alive, views, days_ago
        (601, "фитнес", "фитнес", "proven", "posted", True, 120, 20),
        (602, "фитнес", "фитнес", "deleted", "posted", False, 40, 20),
        (603, "фитнес", "фитнес", "fresh", "posted", True, 90, 3),
        (604, "фитнес", "фитнес", "unread", "posted", True, 0, 20),
        (605, "фитнес", "фитнес", "failed", "post_failed", None, None, 20),
        (606, "вязание", "вязание", "other", "posted", True, 300, 20),
        (607, "фитнес", "фитнес", None, "posted", True, 50, 20),
        (608, "йога", "фитнес", "byniche", "posted", True, 10, 30),
    ]
    for gid, kw, niche, un, outcome, alive, views, days in rows:
        await p.execute(
            "INSERT INTO niche_growth_targets(owner_id, group_id, title, keyword, niche, "
            "username, members, acc_id, outcome, msg_id, alive, views, posted_at, checked_at) "
            "VALUES($1,$2,'g',$3,$4,$5,500,7,$6,1,$7,$8,"
            "now() - make_interval(days => $9), now())",
            OWNER, gid, kw, niche, un, outcome, alive, views, days)


def test_only_proven_groups_come_back(pool):  # noqa: F811
    loop, p = pool
    from services import op_worker

    async def _go():
        await _seed(p)
        got = await op_worker._growth_proven_groups(p, OWNER, "фитнес", ["фитнес"], 10)
        assert [g["id"] for g in got] == [601, 608], got
        assert all(g["proven"] and g["join_ref"].startswith("@") for g in got)
        # Чужая тема не возвращается и по ключу, и по нише.
        other = await op_worker._growth_proven_groups(p, OWNER, "кулинария", ["торты"], 10)
        assert other == []
        assert len(await op_worker._growth_proven_groups(
            p, OWNER, "фитнес", ["фитнес"], 1)) == 1
        assert await op_worker._growth_proven_groups(
            p, OWNER, "фитнес", ["фитнес"], 0) == []

    loop.run_until_complete(_go())


def test_claim_is_taken_once_postgres(pool):  # noqa: F811
    loop, p = pool
    from services import op_worker

    async def _go():
        await _seed(p)
        a, b = await asyncio.gather(
            op_worker._growth_claim_proven(p, OWNER, 601, 7),
            op_worker._growth_claim_proven(p, OWNER, 601, 8))
        assert sorted([a, b]) == [False, True], "доказанную группу заняли оба запуска"
        row = await p.fetchrow(
            "SELECT outcome, alive, msg_id, posted_at FROM niche_growth_targets "
            "WHERE owner_id=$1 AND group_id=601", OWNER)
        assert row["outcome"] == "marked" and row["alive"] is None
        assert row["msg_id"] is None and row["posted_at"] is None
        # Занятая больше не доказанная — следующий запуск её не берёт.
        got = await op_worker._growth_proven_groups(p, OWNER, "фитнес", ["фитнес"], 10)
        assert 601 not in {g["id"] for g in got}
        # Удалённый и свежий посты занять нельзя.
        assert not await op_worker._growth_claim_proven(p, OWNER, 602, 7)
        assert not await op_worker._growth_claim_proven(p, OWNER, 603, 7)
        # После нового поста исход пишется в ту же строку — цикл замкнут.
        await op_worker._growth_record_outcome(p, OWNER, 601, "posted", msg_id=77)
        assert await p.fetchval(
            "SELECT outcome FROM niche_growth_targets WHERE owner_id=$1 AND group_id=601",
            OWNER) == "posted"

    loop.run_until_complete(_go())


def _executor_src() -> str:
    src = (pathlib.Path(__file__).resolve().parents[1] / "services" / "op_worker.py"
           ).read_text(encoding="utf-8")
    tree = ast.parse(src)
    return next(ast.get_source_segment(src, n) for n in tree.body
                if isinstance(n, ast.AsyncFunctionDef) and n.name == "_exec_niche_growth_post")


def test_executor_trusts_only_a_real_mark():
    body = _executor_src()
    assert '== "INSERT 0 1"' in body, "пометка «уже занято другим» открывает пост"
    assert "_growth_claim_proven(" in body
    assert body.index("_growth_claim_proven(") < body.index("post_to_channel(")
    assert body.index("_growth_proven_groups(") < body.index("for idx, grp in enumerate(groups)")
