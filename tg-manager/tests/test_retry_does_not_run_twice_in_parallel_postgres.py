# ruff: noqa: F811  — фикстуры pool/clean импортированы из соседнего модуля
"""Повтор одной и той же работы не идёт двумя операциями параллельно.

ЧТО БЫЛО.
  * «Перезапустить все недоведённые» (`resubmit_unfinished`) брал и упавшую
    операцию, и её упавший повтор: оба недоведённые. Получались две новые
    операции по одним и тем же нетронутым целям, и они шли параллельно —
    журнал семьи каждая читает до действия, так что цель, которую первая ещё
    не записала, брала и вторая: двойное приглашение, второе сообщение.
  * «Повторить» на исходной операции (`resubmit_one`) ставил второй повтор,
    пока первый ещё шёл: статус исходной от повтора не меняется, кнопка
    остаётся на месте, второе нажатие — та же гонка.

Фикстуры и помощники — из `test_retry_and_dead_letter_e2e_postgres.py`.
"""
from __future__ import annotations

import json

from tests.test_retry_and_dead_letter_e2e_postgres import (  # noqa: F401
    DSN, OWNER, _op, _run, clean, pool, pytestmark,
)


def _children_of(pool, op_id):
    rows = _run(pool.fetch(
        "SELECT id, params FROM operation_queue WHERE owner_id=$1", OWNER))
    out = []
    for r in rows:
        p = json.loads(r["params"]) if isinstance(r["params"], str) else r["params"]
        if (p or {}).get("retry_of_op") == op_id:
            out.append(int(r["id"]))
    return out


def test_bulk_retry_takes_only_the_last_link_of_a_chain(pool):
    from services import operation_bus as obus

    root = _run(_op(pool, status="failed", op_type="bulk_join"))
    child = _run(_op(pool, status="failed", op_type="bulk_join",
                     params={"retry_of_op": root}))
    res = _run(obus.resubmit_unfinished(pool, OWNER, hours=24))
    assert res["retried"] == 1, res
    assert _children_of(pool, child), "последнее звено не перепоставлено"
    assert _children_of(pool, root) == [child], (
        "упавшая операция с уже поставленным повтором перепоставлена второй раз")


def test_retry_is_refused_while_a_previous_retry_is_live(pool):
    from services import operation_bus as obus

    root = _run(_op(pool, status="failed", op_type="bulk_join", done=0))
    live = _run(_op(pool, status="running", op_type="bulk_join",
                    params={"retry_of_op": root}))
    res = _run(obus.resubmit_one(pool, OWNER, root))
    assert res["ok"] is False and res["code"] == 409, res
    assert res.get("live_op_id") == live
    assert _children_of(pool, root) == [live], "второй повтор встал параллельно первому"

    # Первый повтор закончился — повторять снова можно.
    _run(pool.execute("UPDATE operation_queue SET status='failed' WHERE id=$1", live))
    res = _run(obus.resubmit_one(pool, OWNER, root))
    assert res["ok"] is True, res


def test_miniapp_retry_obeys_the_same_blockers(pool, monkeypatch):
    """Мини-апп повторяет через свою ручку — запреты у неё те же, что у бота:
    повтор уже идёт → 409, журнал предка убран → 409."""
    from services import mini_app_api
    from tests.test_miniapp_mass_ops_reachable import _Req, _handler

    monkeypatch.setattr(mini_app_api, "_get_uid", lambda request: OWNER)
    retry = _handler(pool, "POST", "/api/miniapp/operation/{op_id}/retry")

    root = _run(_op(pool, status="failed", op_type="bulk_join", done=0))
    live = _run(_op(pool, status="pending", op_type="bulk_join",
                    params={"retry_of_op": root}))
    resp = _run(retry(_Req(match_info={"op_id": str(root)})))
    assert resp.status == 409, resp.body
    assert _children_of(pool, root) == [live]

    _run(pool.execute("UPDATE operation_queue SET status='failed', "
                      "journal_pruned=TRUE WHERE id=$1", live))
    resp = _run(retry(_Req(match_info={"op_id": str(root)})))
    assert resp.status == 409, resp.body
    assert "Журнал целей" in json.loads(resp.body.decode())["error"]
    assert _children_of(pool, root) == [live], (
        "мини-апп повторил операцию с убранным журналом — вся работа заново")
