"""Регресс: счётчик каналов считает то же, что показывает список.

Список каналов отдаёт `SELECT DISTINCT channel_id AS id`, а счётчик рядом брал
`COUNT(DISTINCT id)` — первичный ключ СТРОКИ. В managed_channels один и тот же
канал лежит отдельной строкой у каждого владельца (`UNIQUE(owner_id, channel_id)`),
а выборка нарочно тянет ещё и чужие строки: каналы из экосистем и рабочих
пространств пользователя. Общий канал попадал в список один раз, а в счётчик —
столько раз, сколько у него владельцев.

Наружу это выходило дважды. Шапка экрана писала «47 каналов» там, где их 40. И
кнопка «Загрузить ещё» жила по условию `CH_OFFSET >= CH_TOTAL`: раз счётчик
больше числа строк, кнопка предлагала догрузить то, чего нет, — тап впустую.

Здесь сверяется один инвариант: список и счётчик считают ОДНО И ТО ЖЕ поле.
"""
from __future__ import annotations

import ast
import pathlib
import re

from tests.miniapp_source import api_function

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _handler_source(name: str) -> str:
    # API-слой мини-аппа — больше не один файл; ручка списка каналов живёт в
    # services/mini_app_channels.py.
    return ast.unparse(ast.parse(api_function(name)))


def _sql_literals(src: str) -> list[str]:
    """Строковые литералы ручки — с подстановками как ИМЕНАМИ.

    Регекспом по кавычкам это не берётся: запросы здесь тройные f-строки с
    вложенными кавычками. Подставленный кусок читаем как `{ИМЯ}` — именно имя
    и надо увидеть, чтобы убедиться, что источник у всех запросов общий.
    """
    out = []
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.append(node.value)
        elif isinstance(node, ast.JoinedStr):
            out.append("".join(
                part.value if isinstance(part, ast.Constant)
                and isinstance(part.value, str)
                else "{" + (ast.unparse(part.value)
                            if isinstance(part, ast.FormattedValue) else "") + "}"
                for part in node.values))
    return out


def test_channels_count_and_list_agree_by_construction():
    """Список и ВСЕ счётчики обязаны читать один и тот же источник.

    Прежняя проверка требовала, чтобы список и счётчики писали `DISTINCT` по
    одному столбцу. Довод был верен, а форма — слаба, и на ней же всё и
    разошлось: `SELECT DISTINCT channel_id AS id, …, title, is_admin, …`
    различает строки по ВСЕМУ списку столбцов, а у одного канала, видимого
    через экосистему, копии владельцев отличаются и названием, и правами. То
    есть список отдавал общий канал ДВАЖДЫ (проверено на живой базе), счётчик
    рядом считал `COUNT(DISTINCT channel_id)` и говорил «один» — и проверка
    всё это время была зелёной, потому что столбец в обоих местах совпадал.

    Поэтому равенство теперь не сверяется, а обеспечивается ПОСТРОЕНИЕМ:
    список и все пять счётчиков читают одну и ту же выборку
    `_CHANNELS_ONE_ROW_SQL`, где на канал ровно одна строка (и своя
    предпочтительнее чужой). Сверять в такой форме нечего — можно лишь
    проверить, что никто не завёл себе отдельный источник.
    """
    src = api_function("channels")
    reading = [q for q in _sql_literals(src)
               if "FROM visible" in q or "managed_channels" in q]
    assert reading, "в ручке каналов не нашлось ни одного чтения — проверка ослепла"
    own_source = [q for q in reading
                  if "managed_channels" in q and "_CHANNELS_ONE_ROW_SQL" not in q]
    assert not own_source, (
        "рядом со списком завёлся отдельный источник каналов вместо общей "
        "выборки по одной строке на канал: число в шапке или на чипе снова "
        "разойдётся со списком:\n  " + "\n  ".join(q[:120] for q in own_source))
    assert "_CHANNELS_ONE_ROW_SQL" in src, (
        "ручка больше не читает общую выборку одной строки на канал")
    # И прямой запрет вернувшейся формы: простой DISTINCT тут не работает.
    assert "SELECT DISTINCT channel_id" not in src, (
        "вернулся `SELECT DISTINCT channel_id`: он различает строки по всему "
        "списку столбцов, и общий канал снова придёт двумя строками")


def test_frontend_stops_paging_by_that_count():
    """Счётчик не витрина: на нём висит условие догрузки."""
    ui = (ROOT / "mini_app" / "index.html").read_text(encoding="utf-8")
    assert "CH_OFFSET >= CH_TOTAL" in ui, (
        "догрузка каналов больше не опирается на серверный счёт — "
        "проверьте, актуальна ли эта проверка"
    )
