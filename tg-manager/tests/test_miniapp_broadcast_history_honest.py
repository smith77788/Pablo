"""История рассылок: видно, сколько их всего, и как дойти до неудачных.

РАЗРЫВ. `/api/miniapp/broadcasts` отдавал `LIMIT 30` без общего числа и без
`offset`. Тридцать первая рассылка для владельца просто не существовала: ни
строки о том, что список обрезан, ни способа догрузить остальное. Среза по
статусу не было — а неудачные рассылки ищут именно так.

Экран «Расписание» считал по той же странице и выдавал результат за всю
историю: «Запланировано 3» означало «три среди последних тридцати», а
«Доставка 94%» — «по последним тридцати рассылкам». Цифра, посчитанная по
хвосту, показывалась как итог по всему, что владелец когда-либо отправлял.

ЗАМЕРЕНО ПОСЛЕ ПРАВКИ (Chromium, 140 рассылок): заголовок «30 из 140», срезы
«Все 140 · Готово 100 · Ошибка 12 · Частично 18 · Ожидает 8 · Работает 2», тап
по «Ошибка» даёт 12 строк одним запросом, догрузка доводит список до конца,
плитки расписания берут числа по всей истории (доставка 97%).
"""
from __future__ import annotations

import pathlib
import re

from tests.miniapp_source import miniapp_html


def _api_src() -> str:
    return (pathlib.Path(__file__).resolve().parents[1]
            / "services" / "mini_app_api.py").read_text(encoding="utf-8")


def _py_fn(src: str, name: str) -> str:
    i = src.index("async def " + name + "(")
    indent = len(src[:i].split("\n")[-1])
    out = []
    for line in src[src.index("\n", i):].split("\n"):
        if line.strip() and (len(line) - len(line.lstrip())) <= indent and out:
            break
        out.append(line)
    return "\n".join(out)


def _js_fn(src: str, name: str) -> str:
    m = re.search(r"(?:async )?function " + re.escape(name) + r"\s*\(", src)
    assert m, f"{name} не найдена"
    j = src.index("{", m.start())
    depth, k, n = 0, j, len(src)
    while k < n:
        c = src[k]
        if c in "'\"`":
            q, k = c, k + 1
            while k < n and src[k] != q:
                k += 2 if src[k] == "\\" else 1
        elif c == "/" and k + 1 < n and src[k + 1] == "/":
            nl = src.find("\n", k)
            if nl == -1:
                break
            k = nl
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return src[j:k + 1]
        k += 1
    raise AssertionError(f"не нашли конец {name}")


def test_endpoint_pages_counts_and_slices():
    body = _py_fn(_api_src(), "broadcasts_list")
    assert 'request.query.get("limit"' in body and 'request.query.get("offset"' in body, (
        "историю нельзя листать — всё за тридцатой строкой недостижимо")
    assert '"total": total' in body, "нет общего числа рассылок"
    assert "GROUP BY status" in body, "нет счётчиков по статусам"
    assert 'request.query.get("status")' in body, "нельзя запросить только неудачные"
    assert "lower(b.status)=$2" in body, "статус подставляется не параметром"


def test_delivery_is_summed_over_the_whole_history():
    body = _py_fn(_api_src(), "broadcasts_list")
    assert "SUM(sent_count)" in body and "SUM(failed_count)" in body, (
        "доставляемость снова придётся считать по странице, а показывать как "
        "итог по всей истории")


def test_schedule_tiles_use_server_counts():
    body = _js_fn(miniapp_html(), "loadBroadcastSchedule")
    assert "d.counts" in body and "d.totals" in body, (
        "плитки «Запланировано», «Отправлено» и «Доставка» считаются по "
        "странице и выдаются за всю историю")
    assert "limit=200" in body, "экран расписания просит ту же короткую страницу"
    assert "bcSchedNote" in body, (
        "список запланированных может быть неполон, и об этом не сказано")


def test_history_shows_how_much_of_it_is_on_screen():
    html = miniapp_html()
    sec = _js_fn(html, "renderBcasts")
    assert "_bcastShownTotal()" in sec, "заголовок не называет всю историю"
    assert "function renderBcastMore" in html and "function loadMoreBcasts" in html
    more = _js_fn(html, "renderBcastMore")
    assert "из ${String(t)}" in more


def test_slice_goes_to_the_server_not_to_the_loaded_page():
    html = miniapp_html()
    q = _js_fn(html, "_bcastQuery")
    assert "status=" in q, "срез фильтровал бы загруженную страницу, а не историю"
    f = _js_fn(html, "filterBcasts")
    assert "api(_bcastQuery(0))" in f, "срез не перезапрашивает историю"


def test_empty_under_a_slice_does_not_claim_there_are_no_broadcasts():
    body = _js_fn(miniapp_html(), "renderBcasts")
    assert "BCAST_FILTER" in body and "По этому срезу пусто" in body, (
        "под активным срезом пустой список говорит «Нет рассылок» — это "
        "неправда, они есть, просто другого статуса")


def test_a_row_action_does_not_collapse_the_loaded_history():
    body = _js_fn(miniapp_html(), "loadBroadcasts")
    assert "Math.max(BCAST_PAGE, BCAST_ROWS.length)" in body, (
        "перезагрузка после действия в строке схлопывает догруженное до "
        "одной страницы")
