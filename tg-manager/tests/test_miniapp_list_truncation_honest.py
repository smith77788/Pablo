"""Длинные списки мини-аппа: показано ровно то, что сказано.

Разрыв, который это закрывает. `/api/miniapp/bots` и `/api/miniapp/channels`
отдают СРЕЗ (500 строк) и рядом — полное число. Мини-апп брал срез, писал в
шапку полное число и про остаток молчал. При 700 каналах в шапке стояло
«700 каналов», в списке лежало 500 строк, а «Все» в панели массовых действий
добавлял в выбор только загруженные. Дальше подтверждение обещало «в 500
каналах» — и пост, смена названия или выдача админки уходили по неполному
набору, ни разу не сказав, что часть объектов осталась за кадром. То же было
у ботов: «Разослать сообщение всем 500 ботам?» при 700 подключённых.

Здесь защищаются четыре свойства:
  1. загрузчики списков запрашивают срез явно (limit/offset), а не «как выйдет»;
  2. у каждого списка есть узел остатка и кнопка «загрузить ещё»;
  3. «Выбрать все» берёт отфильтрованный срез, а не весь загруженный массив;
  4. у списков есть поиск и чипы среза — иначе сотни карточек ищут прокруткой.
"""
from __future__ import annotations

import re

from tests.miniapp_source import miniapp_html, source_of


def _body(name: str) -> str:
    """Тело функции по балансу фигурных скобок."""
    src = source_of(name)
    m = re.search(r"^(?:async )?function " + re.escape(name) + r"\s*\(", src, re.M)
    assert m, f"функция {name} не найдена"
    i = src.index("{", m.end() - 1)
    depth, j = 0, i
    while j < len(src):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                break
        j += 1
    return src[i:j + 1]


def _asks_for_a_page(body: str, builder: str = "_chQuery") -> bool:
    """Загрузчик задаёт страницу сам или через общий сборщик адреса.

    Раньше limit/offset клеились в каждом загрузчике вручную. Теперь адрес
    списка каналов собирает `_chQuery` (туда же уехали поиск и срез), поэтому
    признаём и прямое `limit=`, и обращение к сборщику — но сам сборщик
    проверяем отдельно, чтобы делегирование не стало способом ничего не задать.
    """
    return ("limit=" in body and "offset=" in body) or (builder + "(") in body


def test_list_loaders_request_explicit_page():
    """Срез запрашивается явно — иначе о его границах нечего и сказать."""
    bots = _body("loadBots")
    assert "limit=" in bots, "loadBots должен запрашивать явный limit"

    builder = _body("_chQuery")
    assert "limit" in builder and "offset" in builder, (
        "_chQuery обязан задавать и limit, и offset — иначе страница «как выйдет»")

    for fn in ("openChannels", "reloadChannels"):
        assert _asks_for_a_page(_body(fn)), (
            f"{fn} должен запрашивать страницу явно (limit+offset)")

    more = _body("loadMoreChannels")
    assert "offset=" in more or "_chQuery(CH_OFFSET)" in more, (
        "догрузка каналов идёт по offset")


def test_lists_have_load_more_node():
    """Остаток списка виден на экране, а не только в ответе сервера."""
    html = miniapp_html()
    for node in ('id="botLoadMore"', 'id="chLoadMore"'):
        assert node in html, f"нет узла остатка {node}"
    assert "loadMoreBots()" in html and "loadMoreChannels()" in html


def test_select_all_takes_the_filtered_slice():
    """«Все» = то, что пользователь видит, а не весь загруженный массив.

    Именно здесь жила тихая неполнота: массовое действие уходило по срезу,
    подтверждение называло его «всеми».
    """
    bots = _body("botSelAll")
    assert "_botsFiltered()" in bots, "botSelAll должен брать отфильтрованный срез"
    assert "BOTS.forEach" not in bots, (
        "botSelAll снова выбирает весь загруженный массив — это и была тихая неполнота")

    chans = _body("chSelAll")
    assert "_chFiltered()" in chans, "chSelAll должен брать отфильтрованный срез"
    assert "CUR_CHANNELS.forEach(c=>CH_SEL" not in chans, (
        "chSelAll снова выбирает весь загруженный массив")


def test_bulk_confirmation_does_not_promise_all_when_list_is_partial():
    """Подтверждение обещает ровно то, что уйдёт."""
    body = _body("submitQuickBroadcast")
    assert "BOT_TOTAL" in body, (
        "подтверждение быстрой рассылки должно сверяться с полным числом ботов")


def test_long_lists_have_search_and_slices():
    """Сотни карточек не ищут прокруткой."""
    html = miniapp_html()
    for node in ('id="botSearch"', 'id="chSearch"',
                 'id="botFilterChips"', 'id="chFilterChips"'):
        assert node in html, f"нет контрола {node}"
    assert "onBotSearch(" in html and "onChSearch(" in html


def test_empty_slice_leaves_a_way_out():
    """Из опустевшего среза есть выход — иначе экран пуст и снять его нечем.

    Выходов ровно два, и оба годятся: либо срез сам возвращается в «Все», либо
    чип активного среза остаётся на экране и при нуле. Раньше требовался только
    первый. Для каналов он перестал быть верным: срез применяет сервер, и сброс
    переменной ВНУТРИ отрисовки поменял бы подпись чипа, не перезапросив список
    — на экране остались бы группы под заголовком «Все». Поэтому у каналов
    выход второй: чип видно всегда, и он перезапрашивает список по нажатию.
    """
    for fn, var in (("renderBotFilterChips", "BOT_FILTER"),
                    ("renderChFilterChips", "CH_FILTER")):
        body = _body(fn)
        resets = f"{var} = 'all'" in body
        keeps_chip = f"k==={var}" in body or f"k === {var}" in body
        assert resets or keeps_chip, (
            f"{fn}: из пустого среза не выйти — чипа активного среза на экране "
            "нет, и в «Все» он сам не возвращается")
