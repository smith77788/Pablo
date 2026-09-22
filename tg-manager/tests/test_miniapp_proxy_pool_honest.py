"""Пул прокси показывает весь парк, а не первые двести молча.

РАЗРЫВ. `/api/miniapp/proxies` отдавал `LIMIT 200` без `total` и без `offset`,
а пять мест во фронте принимали этот срез за весь парк:

* экран «Пул прокси» писал в плитку «Всего» длину ПОЛУЧЕННОГО списка, то есть у
  владельца с 340 прокси там стояло 200 — и это называлось всем парком;
* счётчик на вкладке «Прокси» считался так же;
* три выпадашки назначения прокси (аккаунту, при импорте сессии, в авторег-про)
  строились из того же среза: прокси за его границей нельзя было ни увидеть, ни
  выбрать, и причина нигде не называлась. Владелец видел, что нужного прокси в
  списке нет, и делал единственный доступный вывод — что его нет вовсе.

Поиска по пулу не было совсем: найти конкретный прокси среди сотен можно было
только прокруткой.

ЧТО ДЕРЖИТ ЭТОТ ТЕСТ. Ручка принимает limit/offset и возвращает total со
счётчиками по всему парку; экран показывает остаток строкой и умеет догрузить;
плитки никогда не выдают цифру со страницы за цифру по парку; действие в строке
не схлопывает догруженный список обратно до одной страницы.
"""
from __future__ import annotations

import re

from tests.miniapp_source import miniapp_html

API = "services/mini_app_api.py"


def _api_src() -> str:
    import pathlib
    return (pathlib.Path(__file__).resolve().parents[1] / API).read_text(encoding="utf-8")


def _fn(src: str, name: str) -> str:
    m = re.search(r"(?:async )?(?:def|function) " + re.escape(name) + r"\s*\(", src)
    assert m, f"{name} не найдена"
    if src[m.start():].lstrip().startswith(("async def", "def")):
        # Python: тело до следующей строки с тем же отступом
        start = src.index("\n", m.end())
        indent = len(src[m.start():].split("\n")[0]) - len(src[m.start():].split("\n")[0].lstrip())
        out = []
        for line in src[start:].split("\n"):
            if line.strip() and (len(line) - len(line.lstrip())) <= indent and out:
                break
            out.append(line)
        return "\n".join(out)
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


def test_endpoint_pages_and_counts_the_whole_pool():
    body = _fn(_api_src(), "proxies")
    assert 'request.query.get("limit"' in body and 'request.query.get("offset"' in body, (
        "ручка не принимает страницу — фронту нечем догрузить остаток")
    assert "LIMIT $2 OFFSET $3" in body, "срез всё ещё зашит числом в SQL"
    assert '"total": total' in body, "нет числа по всему парку"
    # счётчики берутся из БД по всему парку, а не из отданной страницы
    assert "FROM user_proxies WHERE owner_id=$1" in body
    assert "is_alive IS TRUE" in body and "is_alive IS FALSE" in body, (
        "нет агрегата живых/мёртвых — плиткам нечего показать честно")


def test_screen_asks_for_a_page_and_shows_the_rest():
    html = miniapp_html()
    load = _fn(html, "loadProxyPool")
    assert "limit=" in load and "offset=0" in load, "экран не запрашивает страницу"
    assert "renderPpLoadMore" in load, "остаток не показывается"
    more = _fn(html, "renderPpLoadMore")
    assert "из ${num(PP_TOTAL)}" in more, "строка догрузки не называет весь парк"
    assert "function loadMoreProxies" in html, "догружать нечем"


def test_a_row_action_does_not_collapse_the_loaded_list():
    """После отметки резервным список из 340 не схлопывается до 200."""
    load = _fn(miniapp_html(), "loadProxyPool")
    assert "Math.max(PP_PAGE, _proxyPoolData.length)" in load, (
        "перезагрузка просит одну страницу и теряет догруженное")


def test_tiles_never_pass_a_page_number_off_as_the_whole_pool():
    body = _fn(miniapp_html(), "_ppApplyCounts")
    assert "partial" in body, "нет случая «агрегата нет, парк догружен не весь»"
    assert "partial ? '—'" in body, (
        "при недоступном агрегате в плитки идёт цифра со страницы под подписью "
        "«Живых» — непроверенное выдаётся за проверенное")
    assert "'Всего' : 'Показано'" in body, (
        "подпись плитки не различает «по всему парку» и «по загруженному»")


def test_pickers_take_the_whole_pool_and_admit_a_cut():
    html = miniapp_html()
    assert "PROXY_PICK_LIMIT" in html
    # ни одна выпадашка не ходит за срезом по умолчанию
    assert "api('/api/miniapp/proxies')" not in html.replace(" ", ""), (
        "осталась выпадашка, берущая серверный срез за весь парк")
    note = _fn(html, "_proxyTruncOption")
    assert "Пуле прокси" in note, "обрезанный список не говорит, где искать остальное"


def test_search_says_what_it_searched():
    html = miniapp_html()
    assert 'id="ppSearch"' in html, "по пулу из сотен прокси нельзя искать"
    more = _fn(html, "renderPpLoadMore")
    assert "narrowed" in more and "из ${num(PP_TOTAL)}" in more, (
        "не сказано, что поиск идёт по загруженным, а не по всему парку")
    empty = _fn(html, "renderProxyPoolList")
    assert "Ничего не подошло" in empty, (
        "под поиском пустой список говорит «Нет прокси» — это неправда, "
        "прокси есть, просто ни один не подошёл")
