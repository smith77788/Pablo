"""Регрессия: четыре экрана истории врали о пустоте и говорили по-английски.

Что было сломано на экранах «Жалобы», «Накрутка», «Продвижение» и «Проверка
номеров».

1. Каждый просил общую страницу очереди (30 строк) и отбирал свои операции у
   себя. Стоило другим операциям вытеснить последнюю — и экран писал «История
   пуста», хотя история была.

2. Статус показывался как есть: running, done, failed. Владелец не читает
   по-английски (CLAUDE.md: «не оставлять английских фраз в UI»). Там же, где
   у операции нет подписи, вместо неё показывался технический идентификатор —
   report_peer, phone_check, boost_views.

3. Ошибка загрузки притворялась пустым состоянием: «⚠️ Ошибка» через empty(),
   без кнопки повтора. Причину видно, а выйти из неё нечем.

4. Пустые состояния молчали: «История пуста» и ни слова о том, что в ней
   появится.
"""
from __future__ import annotations

import inspect
import re

import pytest

from services import mini_app_api
from tests.miniapp_source import source_of

SCREENS = [
    # функция экрана, элемент истории, тип(ы) операций, русская подпись
    ("openReporter", "reporterHistory", "report_peer", "Жалоба"),
    ("openBoost", "boostHistory", "boost_views,boost_reactions", "Накрутка"),
    ("openGrowth", "growthHistory", "niche_growth_post", "Продвижение"),
    ("openPhoneChecker", "phoneCheckHistory", "phone_check", "Проверка номеров"),
]


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


@pytest.mark.parametrize("fn, elem, op_types, ru", SCREENS)
def test_history_asks_the_server_for_its_own_types(fn, elem, op_types, ru):
    body = _fn(fn)
    assert "op_type=" in body, (
        f"{fn} отбирает операции у себя из общей страницы очереди и врёт "
        f"«история пуста», когда их вытеснили другие")
    assert op_types.split(",")[0] in body, f"{fn} просит не свой тип операций"
    assert ".filter(o=>o.op_type" not in body, (
        f"в {fn} остался клиентский отбор поверх серверного — один из двух лишний")


@pytest.mark.parametrize("fn, elem, op_types, ru", SCREENS)
def test_history_speaks_russian(fn, elem, op_types, ru):
    body = _fn(fn)
    assert "${o.status}" not in body, (
        f"{fn} показывает статус как есть (running/done/failed) — владелец не "
        f"читает по-английски")
    assert "stb(o.status)" in body, f"{fn} не переводит статус"
    assert f"'{ru}'" in body, (
        f"{fn} подставляет технический идентификатор операции вместо названия")


@pytest.mark.parametrize("fn, elem, op_types, ru", SCREENS)
def test_history_error_is_an_error_not_an_empty_state(fn, elem, op_types, ru):
    body = _fn(fn)
    assert f"empty('⚠️','Ошибка'" not in body, (
        f"в {fn} ошибка притворялась пустым состоянием — без выхода из неё")
    assert "errHtml(" in body and f"{fn}()" in body, (
        f"{fn} не даёт повторить загрузку после сбоя")


@pytest.mark.parametrize("fn, elem, op_types, ru", SCREENS)
def test_empty_history_explains_itself(fn, elem, op_types, ru):
    body = _fn(fn)
    assert "История пуста" not in body, (
        f"«История пуста» в {fn} не говорит, что в ней появится")


def test_op_type_filter_accepts_several_types():
    """Раздел «Накрутка» объединяет пять типов операций."""
    src = inspect.getsource(mini_app_api)
    assert '_op_raw.split(",")' in src, (
        "фильтр принимает только один тип — экран с несколькими типами "
        "вынужден отбирать их у себя")
    assert "oq.op_type = ANY(" in src, "список типов не доходит до запроса"
    assert "Слишком много типов операций" in src, (
        "список типов не ограничен сверху")
