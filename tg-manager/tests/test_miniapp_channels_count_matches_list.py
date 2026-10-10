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

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _handler_source(name: str) -> str:
    tree = ast.parse((ROOT / "services" / "mini_app_api.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return ast.unparse(node)
    raise AssertionError(f"обработчик {name} не найден")


def test_channels_count_and_list_agree_on_the_same_column():
    """ВСЕ счётчики обработчика считают то же поле, что отдаёт список.

    Проверялся только первый COUNT(DISTINCT …). Счётчиков рядом со списком стало
    пять — общее число для «загрузить ещё» и четыре для чипов срезов, — и они
    стоят на том же экране, под той же шапкой. Ошибись в любом, и чип «👑 Мои 47»
    снова встанет рядом со списком из сорока.
    """
    src = _handler_source("channels")
    listed = re.search(r"SELECT DISTINCT\s+(\w+)", src)
    counted = re.findall(r"COUNT\(DISTINCT\s+(\w+)\)", src)
    assert listed, "в списке каналов не найден SELECT DISTINCT — проверка ослепла"
    assert counted, "рядом со списком не найден COUNT(DISTINCT …) — проверка ослепла"
    wrong = sorted({c for c in counted if c != listed.group(1)})
    assert not wrong, (
        f"список отдаёт DISTINCT {listed.group(1)}, а счётчики считают "
        f"DISTINCT {', '.join(wrong)}: число в шапке или на чипе не совпадёт со "
        "списком, а «Загрузить ещё» будет предлагать несуществующие страницы"
    )


def test_frontend_stops_paging_by_that_count():
    """Счётчик не витрина: на нём висит условие догрузки."""
    ui = (ROOT / "mini_app" / "index.html").read_text(encoding="utf-8")
    assert "CH_OFFSET >= CH_TOTAL" in ui, (
        "догрузка каналов больше не опирается на серверный счёт — "
        "проверьте, актуальна ли эта проверка"
    )
