"""Регресс: выпадашка не показывает список с прошлого открытия экрана.

Экран открывается, уходит в сеть за списком и заполняет `<select>` ответом. Всё,
что лежало в выпадашке с прошлого открытия, висит там до ответа сервера — и по
нему можно выбрать и отправить. Не гипотеза: этот же баг уже чинили в
`_gfLoadAccounts`, где комментарий объясняет цену («в выпадашке висели аккаунты с
ПРОШЛОГО открытия экрана»).

Здесь закреплены три выпадашки, которые молчали так же:
  * `crChannel`  — каналы в «Позициях канала»: выбирался устаревший канал;
  * `bfEcosystem`— экосистемы в фабрике ботов: бот привязывался к той, что
                   осталась на экране с прошлого раза;
  * `gpEcosystem`— то же в фабрике персон.

Общего храповика на это НЕ ставим сознательно. Пробовали два: «до запроса что-то
записать в экран» проходил вхолостую (открывашка чистила соседние поля, и правило
выполнялось, пока выпадашка оставалась старой), а «зачистить всё, что заполняется
после ответа» давал 38 находок на 109 открывашек — почти все ложные, это счётчики
`m-*-val` на карточке меню, которым положено обновляться после загрузки. Детектор
с такой долей ложных срабатываний живёт до первого неудобства, после чего его
отключают вместе с защитой. Поэтому проверяем поимённо то, что проверено руками.
"""
from __future__ import annotations

import re

import pytest

from tests.miniapp_source import miniapp_source

# выпадашка → функция, которая её наполняет
CASES = {
    "crChannel": "openChannelRanks",
    "bfEcosystem": "openBotFactory",
    "gpEcosystem": "openGpCreateModal",
}


def _body(fn: str) -> str:
    src = miniapp_source()
    m = re.search(r"^(?:async )?function " + re.escape(fn) + r"\s*\(", src, re.M)
    assert m, f"функция {fn} не найдена"
    i = src.index("{", m.end() - 1)
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[i:j + 1]
    raise AssertionError(f"не закрылось тело {fn}")


@pytest.mark.parametrize("sel_id,fn", sorted(CASES.items()))
def test_select_is_cleared_before_the_request(sel_id, fn):
    body = _body(fn)
    assert "await api(" in body, f"{fn} больше не ходит в сеть — проверка устарела"
    head = body[:body.index("await api(")]
    assert sel_id in head, (
        f"{fn} уходит в сеть, не тронув выпадашку {sel_id}: до ответа сервера там "
        "лежит список с прошлого открытия экрана, и по нему можно выбрать"
    )
    assert re.search(r"innerHTML\s*=", head), (
        f"{fn} упоминает {sel_id} до запроса, но ничего в неё не пишет"
    )


@pytest.mark.parametrize("sel_id,fn", sorted(CASES.items()))
def test_select_is_actually_filled_from_the_response(sel_id, fn):
    """Иначе проверка выше стерегла бы выпадашку, которую никто не наполняет."""
    body = _body(fn)
    tail = body[body.index("await api("):]
    assert sel_id in tail, f"{sel_id} больше не наполняется ответом в {fn}"
    assert "<option" in tail, f"в {fn} после запроса не кладут <option>"
