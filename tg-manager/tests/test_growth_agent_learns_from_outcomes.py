"""Агент роста учится на исходах: что опубликовалось, что удалили, какие
запросы ничего не дают.

НАХОДКА. `niche_growth_targets` помнила только «здесь были». Каждый запуск
начинал с нуля: запросы, по которым Telegram находил только закрытые или
нетематичные группы, пробовались снова и снова, а судьба поста — удалили ли
его админы через час — оставалась неизвестной. Теперь в журнале есть запрос,
исход и судьба поста, а порядок запросов строится по их отдаче.

ОБРАТНЫЙ КОНТРОЛЬ (прогонялся): убрать отсев в order_keywords -> красный
test_dead_keywords_are_dropped_but_not_below_minimum; не писать исход после
поста -> красный test_outcome_and_fate_are_recorded_postgres.
"""
from __future__ import annotations

import asyncio
import glob
import os
import re

import pytest

from services import growth_learning as gl

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER = 990_781


def test_keyword_stats_counts_good_and_deleted():
    rows = [
        {"keyword": "Фитнес Москва", "outcome": "posted", "alive": True},
        {"keyword": "фитнес  москва", "outcome": "posted", "alive": False},
        {"keyword": "фитнес москва", "outcome": "post_failed", "alive": None},
        {"keyword": "йога", "outcome": "posted", "alive": None},
        {"keyword": None, "outcome": "posted"},
    ]
    st = gl.keyword_stats(rows)
    assert st["фитнес москва"] == {"tries": 3, "good": 1, "deleted": 1}
    assert st["йога"] == {"tries": 1, "good": 1, "deleted": 0}


def test_dead_keywords_are_dropped_but_not_below_minimum():
    stats = {"мертвый": {"tries": 4, "good": 0, "deleted": 2},
             "тоже мертвый": {"tries": 3, "good": 0, "deleted": 0},
             "живой": {"tries": 4, "good": 3, "deleted": 0}}
    order, dead = gl.order_keywords(
        ["мертвый", "новый", "живой", "тоже мертвый", "ещё новый"], stats)
    assert dead == ["мертвый", "тоже мертвый"] or set(dead) == {"мертвый", "тоже мертвый"}
    assert order[0] == "живой", order
    assert set(order) == {"живой", "новый", "ещё новый"}
    # Не меньше MIN_KEYWORDS — лучший из отсеянных возвращается.
    order, dead = gl.order_keywords(["мертвый", "живой"], stats)
    assert len(order) == 2 and dead == []


def test_new_keyword_is_neither_best_nor_worst():
    assert gl.keyword_score(None) == 0.5
    assert gl.keyword_score({"tries": 4, "good": 4, "deleted": 0}) > 0.5
    assert gl.keyword_score({"tries": 4, "good": 0, "deleted": 0}) < 0.5


# ── живая база ──────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def pool():
    if not DSN:
        pytest.skip("нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")
    import asyncpg

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def _boot():
        conn = await asyncpg.connect(DSN)
        for f in ["schema.sql"] + sorted(glob.glob("schema_v*.sql"),
                                         key=lambda p: int(re.search(r"schema_v(\d+)", p).group(1))):
            try:
                await conn.execute(open(f, encoding="utf-8").read())
            except Exception:
                pass
        await conn.close()
        return await asyncpg.create_pool(DSN, min_size=1, max_size=2)

    p = loop.run_until_complete(_boot())
    yield loop, p
    loop.run_until_complete(p.close())
    loop.close()


def test_outcome_and_fate_are_recorded_postgres(pool, monkeypatch):
    loop, p = pool
    from services import account_manager, op_worker

    async def _go():
        await p.execute("DELETE FROM niche_growth_targets WHERE owner_id=$1", OWNER)
        for gid, kw in ((501, "фитнес"), (502, "фитнес"), (503, "йога")):
            await p.execute(
                "INSERT INTO niche_growth_targets(owner_id, group_id, title, keyword, "
                "username, acc_id, outcome) VALUES($1,$2,'g',$3,$4,7,'marked')",
                OWNER, gid, kw, f"g{gid}")
        await op_worker._growth_record_outcome(p, OWNER, 501, "posted", msg_id=11)
        await op_worker._growth_record_outcome(p, OWNER, 502, "posted", msg_id=12)
        await op_worker._growth_record_outcome(p, OWNER, 503, "post_failed",
                                               note="CHAT_WRITE_FORBIDDEN")
        await p.execute("UPDATE niche_growth_targets SET posted_at=now()-interval '7 hours' "
                        "WHERE owner_id=$1 AND outcome='posted'", OWNER)

        async def _status(session, ref, ids, _acc=None):
            return {ids[0]: {"alive": ref == "@g501", "views": 40 if ref == "@g501" else 0}}
        monkeypatch.setattr(account_manager, "get_message_status", _status)

        async def _nosleep(*a, **k):
            return None
        monkeypatch.setattr(op_worker.asyncio, "sleep", _nosleep)
        n = await op_worker._growth_check_posts_due(
            p, OWNER, [{"id": 7, "session_str": "s"}])
        assert n == 2
        # Чужой аккаунт (не захвачен запуском) не спрашивается.
        await p.execute("UPDATE niche_growth_targets SET checked_at=NULL WHERE owner_id=$1", OWNER)
        assert await op_worker._growth_check_posts_due(
            p, OWNER, [{"id": 8, "session_str": "s"}]) == 0
        rows = {r["group_id"]: dict(r) for r in await p.fetch(
            "SELECT group_id, outcome, msg_id, alive, views, outcome_note "
            "FROM niche_growth_targets WHERE owner_id=$1", OWNER)}
        assert rows[501]["alive"] is True and rows[501]["views"] == 40
        assert rows[502]["alive"] is False
        assert rows[503]["outcome"] == "post_failed"
        assert "FORBIDDEN" in rows[503]["outcome_note"]
        st = gl.keyword_stats([dict(r) for r in await p.fetch(
            "SELECT keyword, outcome, alive FROM niche_growth_targets WHERE owner_id=$1", OWNER)])
        assert st["фитнес"] == {"tries": 2, "good": 1, "deleted": 1}
        assert st["йога"] == {"tries": 1, "good": 0, "deleted": 0}

    loop.run_until_complete(_go())


def test_executor_learns_before_searching():
    import ast
    import pathlib
    src = (pathlib.Path(__file__).resolve().parents[1] / "services" / "op_worker.py"
           ).read_text(encoding="utf-8")
    tree = ast.parse(src)
    body = next(ast.get_source_segment(src, n) for n in tree.body
                if isinstance(n, ast.AsyncFunctionDef) and n.name == "_exec_niche_growth_post")
    assert body.index("order_keywords(") < body.index("search_niche_groups(")
    assert "_growth_check_posts_due(" in body
    assert '"posted"' in body and '"post_failed"' in body
