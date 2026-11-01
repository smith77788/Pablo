"""Строка лога операции открывается и говорит, ЧТО изменилось и у кого.

Жалоба владельца (Операция #771 «Профиль ботов»): «не могу нажать на
результат операции, посмотреть детали и изменения». В логе стояли строки «3»
и «4» со значком «Успешно» — и всё:

* целью в журнал писался внутренний номер строки `managed_bots.id`, по нему
  не понять, какой бот изменён;
* у успешной строки не было сообщения, хотя «как было» исполнитель снимает
  перед правкой (для отката) — то есть «было → стало» лежало под рукой;
* строку лога нельзя было нажать, а текст обрезался на 160 символах.

Теперь: цель — @username бота, сообщение — «описание: «было» → «стало»»,
строка открывается окном с полным текстом. Повтор операции, начатой ДО
правки, не меняет второй раз ботов, отмеченных старым номером.
"""
from __future__ import annotations

import os

import pytest

from services import op_worker
from tests.test_bulk_bot_profile import _Pool, _patch_common

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _capture_journal(monkeypatch):
    rows: list[tuple] = []

    async def _done(pool, op_id, step, target, message=None, **k):
        rows.append((str(target), message or ""))
        return True

    monkeypatch.setattr(op_worker, "_journal_done", _done)
    return rows


def _getters(monkeypatch, before: dict):
    from services import bot_api

    async def _name(sess, token, language_code=""):
        return before.get("name", "")

    async def _desc(sess, token, language_code=""):
        return before.get("description", "")

    monkeypatch.setattr(bot_api, "get_my_name", _name)
    monkeypatch.setattr(bot_api, "get_my_description", _desc)


@pytest.mark.asyncio
async def test_success_row_names_the_bot_and_the_change(monkeypatch):
    bots = [{"id": 3, "bot_id": 111, "token": "T1", "username": "dostavkaenbot"}]
    _patch_common(monkeypatch, bots, {"ok": True})
    _getters(monkeypatch, {"description": "Старое описание"})
    rows = _capture_journal(monkeypatch)
    res = await op_worker._exec_bulk_bot_profile(
        _Pool(), None, 771, 99, {"bot_ids": [111], "description": "Новое описание"})
    assert res["ok"] == 1
    target, msg = rows[0]
    assert target == "@dostavkaenbot", f"в журнале опять номер строки: {target!r}"
    assert "описание: «Старое описание» → «Новое описание»" in msg, msg


@pytest.mark.asyncio
async def test_bot_without_username_is_named_by_its_id(monkeypatch):
    bots = [{"id": 4, "bot_id": 222, "token": "T2", "username": None}]
    _patch_common(monkeypatch, bots, {"ok": True})
    _getters(monkeypatch, {"name": "Было"})
    rows = _capture_journal(monkeypatch)
    await op_worker._exec_bulk_bot_profile(
        _Pool(), None, 771, 99, {"bot_ids": [222], "name": "Стало"})
    assert rows[0][0] == "бот 222"


@pytest.mark.asyncio
async def test_retry_of_an_old_op_does_not_edit_a_bot_twice(monkeypatch):
    # Операция начата ДО правки: в её журнале бот отмечен старым номером «3».
    bots = [{"id": 3, "bot_id": 111, "token": "T1", "username": "dostavkaenbot"}]
    api_calls = _patch_common(monkeypatch, bots, {"ok": True})

    async def _already(pool, op_id):
        return {"3"}

    monkeypatch.setattr(op_worker, "completed_targets", _already)
    _capture_journal(monkeypatch)
    await op_worker._exec_bulk_bot_profile(
        _Pool(), None, 771, 99, {"bot_ids": [111], "description": "Новое"})
    assert not [m for _, m, _ in api_calls if m.startswith("set")], \
        "бот, уже изменённый до правки, изменён повтором второй раз"


@pytest.mark.asyncio
async def test_rollback_row_names_the_bot(monkeypatch):
    snap = [{"id": 9, "target_id": 111, "before": '{"description": "Старое"}'}]
    bots = [{"bot_id": 111, "token": "T1", "username": "dostavkaenbot"}]
    _patch_common(monkeypatch, bots, {"ok": True})
    rows = _capture_journal(monkeypatch)
    res = await op_worker._exec_bulk_bot_profile_rollback(
        _Pool(snap_rows=snap), None, 772, 99, {"source_op_id": 771})
    assert res["ok"] == 1
    assert rows[0][1].startswith("@dostavkaenbot · вернули: описание: «Старое»"), rows


def test_log_rows_open_with_full_details():
    with open(os.path.join(ROOT, "mini_app", "index.html"), encoding="utf-8") as f:
        html = f.read()
    render = html[html.index("function renderOpLog()"):html.index("function renderOpLogChips()")]
    assert 'onclick="openOpLogRow(${i})"' in render, "строку лога по-прежнему нельзя нажать"
    assert "function openOpLogRow(i)" in render
    fn = render[render.index("function openOpLogRow(i)"):]
    assert "l.message" in fn and "created_at" in fn
