"""Пустой проект не получает «всё в порядке», а список без записей — десяток
действий над пустотой.

Обход мини-аппа новым пользователем 08.11.2026: на главной стояло «Ничего не
требует внимания: операции идут, прокси и боты живы» при нуле ботов и прокси,
а вкладки «Боты» и «Аккаунты» показывали 10 и 13 кнопок и счётчиков раньше,
чем «подключите первого», — сам призыв уезжал за нижний край экрана. Главной
кнопкой пустых «Ботов» стоял скан флота, который без аккаунтов ничего не
находит.
"""
from __future__ import annotations

import asyncio
import pathlib

from services import results_summary

APP = pathlib.Path(__file__).resolve().parents[1] / "mini_app"


class _Pool:
    def __init__(self, has):
        self.has = has

    async def fetchval(self, sql, *a):
        assert "managed_bots" in sql and "tg_accounts" in sql
        if isinstance(self.has, Exception):
            raise self.has
        return self.has


def test_has_resources_reads_bots_and_accounts():
    assert asyncio.run(results_summary._has_resources(_Pool(False), 1)) is False
    assert asyncio.run(results_summary._has_resources(_Pool(True), 1)) is True
    assert asyncio.run(results_summary._has_resources(_Pool(RuntimeError("x")), 1)) is None


def test_bot_summary_for_empty_project_points_to_first_step():
    base = {"days": 7, "money": None, "leads": {}, "replies": {}, "reach": {}, "attention": []}
    empty = results_summary.bot_text({**base, "has_resources": False})
    assert "Ничего не требует внимания" not in empty and "Подключите бота" in empty
    assert "Ничего не требует внимания" in results_summary.bot_text({**base, "has_resources": True})
    # Не смогли спросить базу — поведение прежнее.
    assert "Ничего не требует внимания" in results_summary.bot_text({**base, "has_resources": None})


def test_mini_app_summary_handles_empty_project():
    src = (APP / "screens" / "summary.js").read_text(encoding="utf-8")
    assert "d.has_resources === false" in src
    i = src.index("d.has_resources === false")
    assert src.index("прокси и боты живы") > i


def test_list_tools_hide_while_list_is_empty():
    html = (APP / "index.html").read_text(encoding="utf-8")
    bots = (APP / "screens" / "bots_list.js").read_text(encoding="utf-8")
    accs = (APP / "screens" / "accounts_list.js").read_text(encoding="utf-8")
    for tools, inner in (("botTools", 'id="botSelToggle"'), ("accTools", 'id="accKpi"')):
        start = html.index(f'<div id="{tools}">')
        assert start < html.index(inner), f"{inner} должен лежать внутри {tools}"
    assert "getElementById('botTools')" in bots
    assert "_accToolsShown(" in accs
    empty_bots = bots[bots.index("if (!BOTS.length)"):]
    empty_bots = empty_bots[:empty_bots.index("return; }")]
    assert "openBotAddModal()" in empty_bots.split("+")[0], "главная кнопка — токен"
