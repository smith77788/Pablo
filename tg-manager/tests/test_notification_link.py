"""Уведомление ведёт туда, о чём оно.

Лента показывала текст: «Операция #412 завершена», «Аккаунт ограничен».
Открыть то, о чём уведомление, было нельзя — владелец читал номер и шёл
искать его руками через очередь или список аккаунтов. Теперь строка несёт
дверь: {"fn": "openOpDetail", "arg": 412}.

Проверяется форма двери с ОБЕИХ сторон: при записи (мусор не попадает в
базу, где строка живёт месяцами) и при чтении (чужое имя функции не
превращается в кнопку). Кнопка, которая ничего не делает, читается как
сломанное приложение, поэтому экран проверяет ещё и наличие функции.
"""
from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
JS = (ROOT / "mini_app" / "screens" / "notifications_feed.js").read_text(encoding="utf-8")


def test_only_a_well_formed_link_is_written():
    from database.db import _feed_link

    assert json.loads(_feed_link({"fn": "openOpDetail", "arg": 412})) == {
        "fn": "openOpDetail", "arg": 412}
    assert _feed_link(None) is None
    assert _feed_link({"arg": 1}) is None
    assert _feed_link({"fn": "", "arg": 1}) is None
    assert _feed_link({"fn": "open(); drop", "arg": 1}) is None, (
        "имя функции с лишними символами — это не имя функции")
    assert _feed_link({"fn": "openAccount", "arg": {"id": 1}}) is None
    assert _feed_link({"fn": "openAccount", "arg": True}) is None, (
        "булево — не идентификатор, а True прошёл бы как 1"
    )


def test_only_a_known_screen_is_a_door():
    from services.mini_app_notifications import safe_link

    assert safe_link('{"fn": "openOpDetail", "arg": 412}') == {
        "fn": "openOpDetail", "arg": 412}
    assert safe_link({"fn": "openAccount", "arg": 7}) == {"fn": "openAccount", "arg": 7}
    assert safe_link({"fn": "eval", "arg": "1"}) is None, (
        "белый список — это и есть защита: имя функции приходит из базы")
    assert safe_link({"fn": "openOpDetail"}) is None
    assert safe_link("не json") is None
    assert safe_link(None) is None


@pytest.mark.skipif(not shutil.which("node"), reason="нужен node")
def test_a_renamed_screen_is_not_a_button():
    src = """
const window = { openOpDetail: () => {} };
function esc(s) { return String(s).replace(/"/g, '&quot;'); }
""" + re.search(r"\nfunction _nfOpen\([\s\S]*?\n}\n", JS).group(0) + r"""
console.log(JSON.stringify({
  live: _nfOpen({id: 5, link: {fn: 'openOpDetail', arg: 412}}),
  gone: _nfOpen({id: 5, link: {fn: 'openRenamedAway', arg: 412}}),
  none: _nfOpen({id: 5}),
}));
"""
    out = json.loads(subprocess.check_output(["node", "-e", src], text=True))
    assert "openOpDetail(412)" in out["live"] and "nfRead(5)" in out["live"]
    assert out["gone"] == "" and out["none"] == ""


def test_the_outcome_of_an_operation_carries_its_door():
    worker = (ROOT / "services" / "op_worker.py").read_text(encoding="utf-8")
    assert '{"fn": "openOpDetail", "arg": int(op_id)}' in worker, (
        "итог операции не ведёт в саму операцию")
    assert worker.count('link={"fn": "openOpDetail", "arg": op_id}') >= 2, (
        "уведомления о ходе операции тоже обязаны вести в неё")
    # Все пути судьбы операции знают свой op_id, иначе дверь была бы пустой.
    assert worker.count("op_id=op_id,") >= 6
