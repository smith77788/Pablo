"""Регрессия: самая баноопасная операция спрашивала согласие, не назвав масштаба.

Что было сломано.

1. Подтверждение массового инвайта называло темп и предупреждало про лимит на
   аккаунт, но не говорило ГЛАВНОГО: скольких пригласят и сколькими аккаунтами.
   Число целей показывалось только для списка из файла, хотя для остальных
   источников оно к этому моменту уже посчитано (INV_AUDIENCE) и даже выведено
   на экран — в диалог его просто не доносили. А в момент согласия человек
   смотрит в диалог, а не в список за ним.

2. История инвайтов бралась из общей страницы очереди и отбиралась на фронте:
   стоило тридцати другим операциям вытеснить последний инвайт — и экран писал
   «Нет истории инвайтов», хотя она была.
"""
from __future__ import annotations

import re

import pytest

from tests.miniapp_source import source_of


def _fn(name: str) -> str:
    src = source_of(name)
    m = re.search(r"^(?:async )?function " + re.escape(name) + r"\s*\([^)]*\)\s*\{", src, re.M)
    assert m, f"функция {name} не найдена"
    i = m.end() - 1
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[i:j + 1]
    pytest.fail(f"не закрылось тело {name}")


def test_confirmation_names_how_many_people():
    body = _fn("submitMassInvite")
    m = re.search(r"const _tgtLine\s*=(.*?);\n", body, re.DOTALL)
    assert m, "строки с числом целей больше нет"
    tgt = m.group(1)
    assert "INV_AUDIENCE" in tgt, (
        "число целей называлось только для списка из файла, хотя для остальных "
        "источников оно уже посчитано в INV_AUDIENCE")
    assert "import_list" in tgt, "список из файла по-прежнему должен называть свои цели"


def test_confirmation_names_how_many_accounts():
    body = _fn("submitMassInvite")
    assert "_accLine" in body, (
        "подтверждение не говорит, сколькими аккаунтами будут приглашать")
    m = re.search(r"const _accLine\s*=(.*?);\n", body, re.DOTALL)
    assert m and "checked.length" in m.group(1), (
        "отмеченные аккаунты и «весь флот» — разный масштаб, и различать их "
        "должен сам диалог")


def test_both_lines_reach_the_dialog():
    """Посчитать мало — надо донести до вопроса, на который отвечают «да»."""
    body = _fn("submitMassInvite")
    m = re.search(r"askConfirm\((.*?)\)\)\)", body, re.DOTALL)
    assert m, "подтверждения инвайта нет"
    arg = m.group(1)
    assert "_tgtLine" in arg and "_accLine" in arg, (
        "масштаб посчитан, но в диалог не попал")


def test_invite_history_asks_the_server_for_its_own_type():
    body = _fn("openMassInvite")
    assert "op_type=mass_invite" in body, (
        "история отбиралась из общей страницы очереди и врала «нет истории "
        "инвайтов», когда инвайты вытеснили другие операции")
    assert "o.op_type==='mass_invite'" not in body, (
        "остался клиентский отбор поверх серверного — один из двух лишний")
