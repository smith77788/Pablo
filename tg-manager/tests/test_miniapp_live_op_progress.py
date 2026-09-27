"""Регресс: открытый список операций не замирает на момент открытия.

Живой прогресс приходил по SSE (`op_progress`) каждые несколько секунд, но
рисовался ТОЛЬКО в панель `#opProgress` на Главной. Список операций, экран
массовых операций и карточка одной операции показывают те же числа — и замирали
на том, что было в момент открытия. Человек, открывший «Операции», чтобы
посмотреть, как идёт запущенный инвайт, видел неподвижное «12/340»: событие с
новыми числами приходило и выбрасывалось.

Обновляем ТОЧЕЧНО, а не перерисовкой списка: под пальцем уехала бы прокрутка, а
открытые строки схлопывались бы на каждом событии. Поэтому у каждой строки есть
якорь `data-op-cnt` / `data-op-bar` / `data-op-pct` с id операции, и `liveOpRows`
правит только найденные.

Проверено в браузере: строка списка переехала с «12/340» на «205/340», полоска с
4% на 60%; формат карточки «25 / 50» сохранился; событие по операции, которой нет
на экране, ничего не ломает; done зажимается по total, а процент по сотне;
отложенная операция счётчика не имеет и обновление её не роняет.
"""
from __future__ import annotations

import re

from tests.miniapp_source import miniapp_html


def _body(name: str) -> str:
    src = miniapp_html()
    m = re.search(r"^(?:async )?function " + re.escape(name) + r"\s*\(", src, re.M)
    assert m, f"функция {name} не найдена"
    k, par = m.end() - 1, 0
    while True:
        if src[k] == "(":
            par += 1
        elif src[k] == ")":
            par -= 1
            if par == 0:
                break
        k += 1
    i = src.index("{", k)
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[i:j + 1]
    raise AssertionError(f"не закрылось тело {name}")


def test_sse_event_reaches_the_lists():
    """Обработчик обязан звать не только панель Главной."""
    src = miniapp_html()
    m = re.search(r"addEventListener\('op_progress'.*?\}\);", src, re.DOTALL)
    assert m, "подписка на op_progress не найдена"
    handler = m.group(0)
    assert "renderOpProgress(" in handler, "панель Главной перестала обновляться"
    assert "liveOpRows(" in handler, (
        "событие с новыми числами не доходит до списков операций — они снова "
        "замрут на момент открытия"
    )


def test_every_screen_with_progress_has_an_anchor():
    """Без якоря обновлять нечего, и экран молча вернётся к замиранию."""
    src = miniapp_html()
    assert src.count('data-op-cnt="${o.id}"') >= 2, (
        "якорь счётчика пропал из списка операций или из карточки операции"
    )
    assert 'data-op-cnt="${r.id}"' in src, "якорь счётчика пропал из массовых операций"
    assert src.count("data-op-bar=") >= 2, "якорь полоски пропал"
    assert "data-op-pct=" in src, "якорь процента пропал из карточки операции"


def test_updater_finds_anchors_by_operation_id():
    body = _body("liveOpRows")
    for attr in ("data-op-cnt", "data-op-bar", "data-op-pct"):
        assert attr in body, f"liveOpRows не ищет {attr}"
    assert "querySelectorAll" in body, (
        "одна и та же операция видна сразу на нескольких экранах — нужен обход всех "
        "найденных, а не первого"
    )


def test_updater_clamps_the_numbers():
    """Сервер присылал «34 из 17» у старых операций — в списке это уже зажато."""
    body = _body("liveOpRows")
    assert "Math.min(Number(op.done) || 0, total)" in body, "done не зажат по total"
    assert "Math.max(0, Math.min(100," in body, "процент не зажат в 0..100"


def test_updater_does_not_redraw_whole_lists():
    """Перерисовка на каждом событии рвала бы прокрутку под пальцем."""
    body = _body("liveOpRows")
    for forbidden in ("renderOps(", "innerHTML =", "txt("):
        assert forbidden not in body, (
            f"liveOpRows перерисовывает список ({forbidden}) вместо точечного обновления"
        )
