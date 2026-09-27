"""Карта инфраструктуры называет настоящие числа, а не размер страницы.

Экран строится по первым 30 ботам и 20 каналам — в сообщение Telegram больше
не помещается, и это правильно. Но подпись внизу читала те же обрезанные
списки: «Итого: 30 ботов · 20 каналов». У владельца с полусотней ботов экран,
который называется «Карта инфраструктуры», сообщал, что инфраструктура вдвое
меньше, чем она есть, — и никак не давал понять, что показано не всё.

Теперь числа берутся COUNT-запросом по всему парку, а под итогом стоит строка
о том, сколько поместилось на карту.
"""
from __future__ import annotations

import ast
import pathlib
import re

HANDLER = pathlib.Path(__file__).resolve().parents[1] / "bot" / "handlers" / "botmother_menu.py"


def _body(name: str) -> str:
    src = HANDLER.read_text(encoding="utf-8")
    lines = src.splitlines()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return "\n".join(lines[node.lineno - 1 : (node.end_lineno or node.lineno)])
    raise AssertionError(f"обработчик {name} не найден в botmother_menu.py")


def test_the_screen_still_pages_its_lists():
    """Проверка имеет смысл, только пока списки действительно обрезаны —
    если LIMIT убрали, её надо переписать, а не оставлять зелёной."""
    body = _body("cb_topology")
    assert re.search(r"LIMIT\s+\d+", body), (
        "карта больше не ограничивает выборку — проверка ниже потеряла смысл")


def test_totals_come_from_a_count_not_from_the_page():
    body = _body("cb_topology")
    assert "bots_total" in body and "chans_total" in body, (
        "итоговые числа снова берутся из длины обрезанных списков")
    assert re.search(r"COUNT\(\*\)\s+FROM managed_bots", body), "нет счётчика ботов по всему парку"
    assert re.search(r"COUNT\(DISTINCT channel_id\)\s+FROM managed_channels", body), \
        "нет счётчика каналов по всему парку"
    assert not re.search(r"Итого: \{len\(bots\)\}", body), "в итоге снова длина страницы"


def test_the_screen_says_that_it_shows_only_a_part():
    body = _body("cb_topology")
    assert "bots_total > len(bots)" in body and "chans_total > len(channels)" in body, (
        "экран не сравнивает показанное с настоящим — значит, и сказать об "
        "обрезке не может")
    assert "показано" in body or "помещается не всё" in body, (
        "нет строки о том, что на карту поместилось не всё")


def test_swarm_count_does_not_pretend_to_be_global():
    """«В Рое» считается по показанным ботам — это допустимо, но должно быть
    подписано, иначе число читается как итог по всему парку."""
    body = _body("cb_topology")
    assert "среди показанных" in body, (
        "число ботов «в Рое» считается по странице и подаётся как общее")
