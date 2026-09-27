"""Экран собранной аудитории: найти человека и дойти до конца списка.

Что было не так. Шапка обещала «Найдено: 18 432», а под ней лежали ровно 50
строк — без намёка на обрыв и без способа увидеть остальные. Найти конкретного
человека было нечем: единственное поле фильтровало по ИСТОЧНИКУ парсинга, а не
по людям, и било запросом на каждую нажатую букву.

Что стало. Поиск по имени / @username / ID, постраничная догрузка, честный
счётчик «Найдено: N · показано M» и debounce на ввод. Условие поиска живёт в
общем построителе фильтров, поэтому список, счётчик и ВЫГРУЗКА отбирают одно и
то же: иначе на экране одно, а в CSV другое — и заметить это можно только по
факту.
"""
from __future__ import annotations

import re

from services.audience_filters import parsed_audience_filters
from tests.miniapp_source import miniapp_html, miniapp_source

SRC = miniapp_source()
HTML = miniapp_html()


def _fn_body(name: str, src: str = "") -> str:
    """Тело функции по балансу скобок (границы по коду, не по длине отрезка)."""
    s = src or SRC
    m = re.search(r"^(?:async )?function " + re.escape(name) + r"\s*\(", s, re.M)
    assert m, f"функция {name} не найдена"
    p = s.index("(", m.end() - 1)
    depth = 0
    for j in range(p, len(s)):
        if s[j] == "(":
            depth += 1
        elif s[j] == ")":
            depth -= 1
            if depth == 0:
                p = j
                break
    i = s.index("{", p)
    depth = 0
    for j in range(i, len(s)):
        if s[j] == "{":
            depth += 1
        elif s[j] == "}":
            depth -= 1
            if depth == 0:
                return s[i:j + 1]
    raise AssertionError(f"не закрылось тело {name}")


# ─────────────────────────── бэкенд: условие поиска ───────────────────────────

def test_search_matches_name_and_username():
    sql, params = parsed_audience_filters({"q": "аня"})
    assert "username ILIKE" in sql and "first_name ILIKE" in sql and "last_name ILIKE" in sql
    assert params == ["%аня%"]


def test_search_strips_the_at_sign():
    """Пользователь пишет «@ivan», в базе username лежит без собаки."""
    _, params = parsed_audience_filters({"q": "@ivan"})
    assert params == ["%ivan%"], "собака ушла бы в LIKE и не нашла бы никого"


def test_numeric_search_also_matches_exact_id():
    """У человека может не быть username — тогда его ищут по ID."""
    sql, params = parsed_audience_filters({"q": "778899"})
    assert "tg_user_id=" in sql
    assert 778899 in params


def test_search_is_ored_not_anded():
    """Три поля соединяются ИЛИ и обязаны быть в скобках.

    Без скобок `A AND B OR C` развалило бы соседние фильтры: строка прошла бы
    по одному лишь last_name, минуя premium и источник.
    """
    sql, _ = parsed_audience_filters({"premium": "1", "q": "ivan"})
    m = re.search(r"\(username ILIKE[^)]*\)", sql)
    assert m, f"условие поиска не в скобках: {sql}"
    assert " OR " in m.group(0) and " AND " not in m.group(0)


def test_search_placeholders_do_not_collide():
    """Номера плейсхолдеров продолжают общую нумерацию, а не начинаются заново."""
    sql, params = parsed_audience_filters({"source": "news", "gender": "f", "q": "777"})
    nums = [int(x) for x in re.findall(r"\$(\d+)", sql)]
    assert sorted(set(nums)) == list(range(2, 2 + len(params))), (sql, params)


def test_empty_search_adds_nothing():
    for empty in ("", "   ", "@", None):
        assert parsed_audience_filters({"q": empty}) == ("", []), f"пустой поиск сузил выборку: {empty!r}"


def test_view_and_export_share_the_builder():
    """Список и выгрузка обязаны идти через ОДИН построитель условий."""
    api = open("services/mini_app_api.py", encoding="utf-8").read()
    view = api.index("async def parsed_audience(")
    exp = api.index("async def parsed_audience_export(")
    end = api.index("app.router.add_get(\"/api/miniapp/parser/audience\"")
    assert "parsed_audience_filters(q, base_params_count=1)" in api[view:exp]
    assert "parsed_audience_filters(q, base_params_count=1)" in api[exp:end]


# ─────────────────────────── фронт: поиск и страницы ──────────────────────────

def test_screen_has_a_people_search_box():
    """Поле поиска стоит в РАЗМЕТКЕ экрана, а не рисуется вместе со списком.

    Иначе каждое обновление списка пересоздавало бы input, и фокус слетал бы
    после первой же буквы.
    """
    assert 'id="audSearch"' in HTML, "поле поиска человека исчезло из разметки"
    i = HTML.index('id="audSearch"')
    box = HTML[i - 400:i + 400]
    assert "onAudQuery(this.value)" in box
    assert "aria-label=" in box, "поле без имени — скринридер прочитает пустоту"
    # старое поле по источнику осталось: это разные вопросы к списку
    assert 'id="parserSearch"' in HTML


def test_typing_does_not_fire_a_request_per_letter():
    for fn in ("onAudQuery", "onAudSource"):
        assert "_audDebounced(" in _fn_body(fn), f"{fn} шлёт запрос на каждую букву"
    deb = _fn_body("_audDebounced")
    assert "clearTimeout" in deb and "setTimeout" in deb


def test_search_goes_into_the_query_and_into_the_export():
    """Одна строка запроса на список и на выгрузку — иначе CSV отдаст не то,
    что человек видит на экране."""
    qs = _fn_body("audFilterQS")
    assert "'&q='+encodeURIComponent(AUD_Q)" in qs, "поиск не уходит в запрос"
    assert "audFilterQS()" in _fn_body("exportAudience"), "выгрузка игнорирует фильтры"
    assert "audFilterQS()" in _fn_body("loadParsedAudience")


def test_list_pages_instead_of_cutting_off():
    body = _fn_body("loadParsedAudience")
    assert "offset='+AUD_OFFSET" in body, "страницы не запрашиваются"
    assert "AUD_ROWS.concat(users)" in body, "догруженная страница затирает предыдущую"
    assert "audLoadMore()" in body, "нет кнопки «загрузить ещё»"


def test_counter_admits_the_cut():
    """Счётчик говорит и сколько найдено, и сколько показано."""
    body = _fn_body("loadParsedAudience")
    # Якорь — строка кода, а не слово «Найдено»: то же слово стоит в комментарии
    # выше, и проверка читала бы объяснение вместо самой шапки.
    m = re.search(r"^\s*const header = .*$", body, re.M)
    assert m, "шапка списка не найдена"
    head = m.group(0)
    assert "показано" in head, "шапка обещает весь список, показывая его часть"
    assert "shown < AUD_TOTAL" in head, "«показано» рисуется даже когда список целиком на экране"


def test_new_search_resets_the_pages():
    """Иначе вторая страница старого запроса приклеится к первой странице нового."""
    body = _fn_body("loadParsedAudience")
    i = body.index("if (!append)")
    head = body[i:i + 200]
    for name in ("AUD_ROWS = []", "AUD_OFFSET = 0", "AUD_TOTAL = 0"):
        assert name in head, f"при новом запросе не сбрасывается {name}"


def test_failed_load_more_keeps_what_is_already_shown():
    """Обрыв на догрузке не должен стирать уже показанные строки."""
    body = _fn_body("loadParsedAudience")
    i = body.index("catch")
    tail = body[i:]
    assert "if (append)" in tail, "ошибка догрузки сотрёт весь список"
    assert "errHtml(" in tail, "первая загрузка падает в тупик без кнопки выхода"


def test_empty_result_says_why_it_is_empty():
    """«Пусто» под поиском и «пусто» без поиска — разные новости."""
    body = _fn_body("loadParsedAudience")
    assert "Под этот поиск никто не подошёл" in body
    assert "(AUD_Q || AUD_SOURCE)" in body
