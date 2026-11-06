"""Потолок списка читается общим помощником слоя, а не руками в каждой ручке.

ЗАЧЕМ ЭТО ПРАВИЛО. Рядом живёт храповик честности
(`tests/test_a_capped_list_says_it_is_capped.py`): «потолок сам по себе не
порок, порок — потолок, о котором владельцу не говорят». Он узнаёт потолок по
одному признаку — вызову `_list_limit(` из слоя мини-аппа. Ручка, которая
разбирает `?limit=` руками, этому храповику НЕ ВИДНА вовсе: она может молча
обрезать список, и общий прогон останется зелёным — не потому, что честно, а
потому, что мерка смотрит не туда.

ЧТО ПРОВЕРЕНО РУКАМИ 06.11.2026. Таких ручек одиннадцать, и все одиннадцать
сегодня честны: восемь кладут итог рядом со страницей, `inbox_list` и
`uch_contacts` — через возврат вызываемой функции (`unified_inbox.list_threads`
отдаёт `total`, `waiting`, `unread`; `contacts_hub.repository.get_contacts` —
`total` с `offset`/`limit`), а `next_actions` — не список владельца, а подсказка
«что делать дальше» на восемь позиций, и обрыва там нет по смыслу. То есть
находки сейчас нет; нет и того, что удержит двенадцатую.

ПОЧЕМУ ХРАПОВИК, А НЕ ЗАПРЕТ. Переписывать одиннадцать работающих ручек ради
формы — работа без пользы владельцу. Но новая ручка обязана брать потолок
общим помощником: тогда её видит храповик честности, то есть правило про
«назови обрыв» начинает на неё действовать само.
"""
from __future__ import annotations

import ast
import re

from tests.miniapp_source import api_files, api_source

_REG = re.compile(
    r'app\.router\.add_(?:get|post|put|delete|patch)\(\s*"([^"]+)"\s*,\s*([A-Za-z_]\w*)')
_BY_HAND = re.compile(r"""query\.get\(\s*["']limit["']""")

# Ровно те одиннадцать, что разобраны в докстринге. Список только сокращается.
_KNOWN = {
    "admin_actions", "admin_payments", "admin_reports", "broadcasts_list",
    "inbox_list", "next_actions", "operation_log", "operations", "proxies",
    "uch_contacts", "vault_messages",
}


def _uncommented(code: str) -> str:
    return "\n".join(l for l in code.splitlines()
                     if not l.lstrip().startswith("#"))


def _hand_rolled() -> dict[str, str]:
    """Имя ручки -> файл, если она разбирает `?limit=` сама."""
    registered: dict[str, list[str]] = {}
    for path, name in _REG.findall(api_source()):
        registered.setdefault(name, []).append(path)
    out: dict[str, str] = {}
    for p in api_files():
        text = p.read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(text)):
            if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.name in registered and node.name not in out):
                body = _uncommented(ast.get_source_segment(text, node) or "")
                if "_list_limit(" in body:
                    continue
                if _BY_HAND.search(body):
                    out[node.name] = p.name
    return out


def test_the_detector_still_sees_the_hand_rolled_ones():
    """Пустая выдача — сломанная мерка, а не чистый слой."""
    found = _hand_rolled()
    assert found, "ни одной ручки с самодельным разбором limit — мерка смотрит не туда"
    assert "_list_limit(" in api_source(), "помощник слоя исчез — правило без адресата"


def test_no_new_handler_reads_the_limit_by_hand():
    extra = sorted(set(_hand_rolled()) - _KNOWN)
    assert not extra, (
        "Новая ручка разбирает ?limit= сама: "
        + ", ".join(f"{n} ({_hand_rolled()[n]})" for n in extra)
        + ".\nБерите `_list_limit(request, default, maximum)` из "
        "services/mini_app_api.py — иначе храповик честности "
        "(tests/test_a_capped_list_says_it_is_capped.py) вашу ручку не видит, "
        "и молчаливый обрыв списка пройдёт зелёным.")


def test_the_known_list_only_shrinks():
    gone = sorted(_KNOWN - set(_hand_rolled()))
    assert not gone, (
        "Эти ручки перешли на общий помощник — уберите их из _KNOWN, чтобы "
        f"список не держал уже исправленное: {gone}")
