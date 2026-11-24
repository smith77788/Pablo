"""Поиск по своему хозяйству: экранирование, скоуп, отказ одной группы.

Поиск в мини-аппе был у каждого списка свой: чтобы найти «тот аккаунт» или
«ту рассылку», надо было вспомнить, в каком разделе искать. Экран «Поиск
Telegram» ищет ЧУЖОЕ — публичные каналы и ботов; по своему не было нигде.

Здесь — то, что не проверить на живой базе: что `%` и `_` в запросе остаются
символами, что скоуп стоит в КАЖДОМ из шести запросов и что упавшая группа не
роняет поиск. Находки и группы на настоящих данных — в
`tests/test_find_all_search_postgres.py`.
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import re
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
JS = (ROOT / "mini_app" / "screens" / "find_all.js").read_text(encoding="utf-8")
HTML = (ROOT / "mini_app" / "index.html").read_text(encoding="utf-8")


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_wildcards_in_the_query_stay_characters():
    """«100%» ищет «100%», а «_» — подчёркивание, а не любую строку."""
    from services.mini_app_global_search import like

    assert like("100%") == r"%100\%%"
    assert like("_") == r"%\_%"
    assert like(r"a\b") == r"%a\\b%"


def test_every_group_takes_the_owner_in_its_own_query():
    from services import mini_app_global_search as G

    for key, label, icon, sql, fn in G._GROUPS:
        assert re.search(r"(owner_id|added_by|b\.created_by)\s*=\s*\$1", sql), key
        assert "ILIKE $2" in sql, key
        assert "LIMIT $3" in sql, key


def test_a_broken_group_does_not_break_the_search_and_says_so():
    from services import mini_app_global_search as G

    class _Row(dict):
        def __getitem__(self, k):
            return dict.get(self, k)

    class _Pool:
        async def fetch(self, sql, uid, pattern, limit):
            if "unified_contacts" in sql:
                raise RuntimeError('column "display_name" does not exist')
            if "tg_accounts" in sql:
                return [_Row(arg=7, title="Женя", subtitle="+79990000001")]
            return []

    out = _run(G.search_all(_Pool(), 991001, "жен"))
    assert out["found"] == 1
    assert [g["key"] for g in out["groups"]] == ["accounts"]
    assert out["failed"] == ["Контакты"], (
        "экран обязан сказать, где не искали: пустая группа из-за сбоя "
        "неотличима от честного «ничего нет»")
    assert out["groups"][0]["items"][0]["fn"] == "openAccount"


@pytest.mark.skipif(not shutil.which("node"), reason="нужен node")
def test_a_hit_with_no_live_door_is_not_a_button():
    """Находка зовёт функцию по имени и только если она есть."""
    src = """
const window = { openAccount: () => {} };
""" + re.search(r"\nfunction _faClick\([\s\S]*?\n}\n", JS).group(0) + r"""
console.log(JSON.stringify({
  num: _faClick({fn: 'openAccount', arg: 12}),
  str: _faClick({fn: 'openAccount', arg: "O'Брайен"}),
  gone: _faClick({fn: 'openRenamedAway', arg: 1}),
  none: _faClick({arg: 1}),
}));
"""
    out = json.loads(subprocess.check_output(["node", "-e", src], text=True))
    assert out["num"] == "openAccount(12)"
    assert out["str"] == "openAccount('O\\'Брайен')", out["str"]
    assert out["gone"] == "" and out["none"] == ""


def test_the_screen_has_a_way_in_and_a_way_out():
    assert '<script src="screens/find_all.js"></script>' in HTML
    assert 'id="s-findall"' in HTML
    assert "openFindAll('')" in HTML, "в поиск нельзя попасть ни из каталога, ни из шапки"
    assert HTML.count("openFindAll('')") >= 2, "дверь должна быть и в шапке, и в каталоге"
    # Рассылка ведёт в «Сообщения» с тем же запросом: своего экрана у неё нет.
    assert "function openBroadcastSearch(q)" in JS
    assert "onBcastSearch(" in JS
