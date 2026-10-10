"""Статистика бота: каждое число ведёт к тому, что оно посчитало.

Четырнадцать чисел, из которых не нажималось ни одно: «Воронок 3» — а каких,
«Рассылок 12» — а чем кончились, «Диалогов 48» — а где они. Ровно та жалоба
владельца, с которой начата эта работа: экран показывает количества, и в нём
ничего не выбирается и не настраивается.

Экраны, куда эти числа ведут, в продукте были всё это время и открываются с
соседней плитки «Боты → бот»; не хватало только связи.

Отдельно — дата на графике. `date.slice(5)` давало «10-05»: в русской записи
это пятое октября, а читается как десятое мая.
"""
from __future__ import annotations

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = open(os.path.join(ROOT, "mini_app", "index.html"), encoding="utf-8").read()


def _fn(name: str) -> str:
    m = re.search(r"(?:async\s+)?function\s+" + re.escape(name) + r"\s*\(", HTML)
    assert m, f"функция {name} не найдена"
    i = HTML.index("{", m.end() - 1)
    depth = 0
    for j in range(i, len(HTML)):
        if HTML[j] == "{":
            depth += 1
        elif HTML[j] == "}":
            depth -= 1
            if depth == 0:
                return HTML[i:j + 1]
    raise AssertionError(name)


def test_every_destination_screen_exists():
    """Самопроверка: ссылки ведут в функции, которые в самом деле есть."""
    for name in ("openBotSubs", "openBotFunnels", "openRelay",
                 "openArScreen", "openBcastForBot"):
        assert re.search(r"(?:async\s+)?function\s+" + name + r"\s*\(", HTML), (
            f"экрана {name} нет — число вело бы в пустоту")


def test_all_numbers_are_tappable():
    f = _fn("openBotStats")
    assert f.count("bstatsGo(") >= 13, (
        "часть чисел статистики по-прежнему ни на что не нажимается: "
        f"найдено {f.count('bstatsGo(')}")
    assert "cursor:pointer" in f and "chev" in f, (
        "по виду не понять, что числа нажимаются")


def test_destinations_are_mapped_in_one_place():
    f = _fn("bstatsGo")
    for where, dest in (("subs", "openBotSubs"), ("funnels", "openBotFunnels"),
                        ("relay", "openRelay"), ("ar", "openArScreen"),
                        ("bcast", "openBcastForBot")):
        assert f"'{where}'" in f, f"нет направления {where}"
        assert dest in f, f"направление {where} никуда не ведёт"


def test_bot_name_is_not_pasted_into_markup():
    """В именах ботов бывают кавычки — имя идёт через переменную."""
    f = _fn("openBotStats")
    assert "BSTATS_BOT_NAME" in HTML, "имя бота негде взять при переходе"
    assert "openBotSubs(" not in f, (
        "имя бота снова подставляется прямо в onclick — кавычка в имени "
        "сломает разметку")


def test_chart_date_is_readable_in_russian():
    f = _fn("openBotStats")
    assert "d2.date.slice(5)" not in f, (
        "вернулась дата вида «10-05» — читается как десятое мая")
    assert "d2.date.slice(8,10)" in f, "день и месяц не переставлены"


def test_empty_chart_explains_itself():
    f = _fn("openBotStats")
    assert ">Нет данных<" not in f, "вернулась подпись «Нет данных» без объяснения"
    assert "нужны хотя бы сутки" in f


def test_error_clears_stale_blocks_and_offers_retry():
    f = _fn("openBotStats")
    assert "errHtml(errRu(e)" in f, "ошибка без кнопки повтора"
    i = f.index("catch(e)")
    tail = f[i:]
    assert "txt('bstatsChart', '')" in tail and "txt('bstatsDetails', '')" in tail, (
        "при ошибке под сообщением остаются цифры прошлой загрузки")
