"""В логе операции можно найти конкретную цель.

У инвайта на две тысячи целей вопрос «а @ivan-то позвали?» решался только
прокруткой: срез по статусу был, поиска не было. Ради неудачных целей на лог
и смотрят, а найти их среди сотен строк было нечем.

Поиск идёт по цели И по сообщению: причина отказа («Privacy restricted») —
второй способ вытащить нужные строки. Счётчики, чипы срезов и общее число
сервер пересчитывает по ТОЙ ЖЕ выборке, что и строки: разойдись они, шапка
«N из M» описывала бы один набор, а список показывал другой — ровно та
ошибка, из-за которой лог раньше назывался «Лог (500)» при двух тысячах
целей.
"""
from __future__ import annotations

import re
from pathlib import Path

from tests.miniapp_source import miniapp_source

SRC = miniapp_source()
API = (Path(__file__).resolve().parent.parent / "services" / "mini_app_api.py").read_text(encoding="utf-8")


def _handler(name: str) -> str:
    i = API.index(f"async def {name}(request: web.Request)")
    j = API.index("    async def ", i + 10)
    return API[i:j]


def _fn_body(name: str) -> str:
    m = re.search(r"^(?:async )?function " + re.escape(name) + r"\s*\(", SRC, re.M)
    assert m, f"функция {name} не найдена"
    p = SRC.index("(", m.end() - 1)
    depth = 0
    for j in range(p, len(SRC)):
        if SRC[j] == "(":
            depth += 1
        elif SRC[j] == ")":
            depth -= 1
            if depth == 0:
                p = j
                break
    i = SRC.index("{", p)
    depth = 0
    for j in range(i, len(SRC)):
        if SRC[j] == "{":
            depth += 1
        elif SRC[j] == "}":
            depth -= 1
            if depth == 0:
                return SRC[i:j + 1]
    raise AssertionError(f"не закрылось тело {name}")


# ─────────────────────────────── сервер ───────────────────────────────────────

def test_log_endpoint_searches_target_and_message():
    h = _handler("operation_log")
    assert 'request.query.get("q")' in h, "лог не принимает строку поиска"
    assert "target ILIKE" in h and "message ILIKE" in h, (
        "поиск не смотрит в причину отказа — по ней и ищут неудачные цели")


def test_search_strips_the_at_sign():
    h = _handler("operation_log")
    i = h.index('request.query.get("q")')
    assert 'lstrip("@")' in h[i:i + 200], "собака ушла бы в LIKE и ничего не нашла"


def test_counts_are_recomputed_over_the_same_selection():
    """Иначе чип «Все 2000» стоял бы над тремя найденными строками."""
    h = _handler("operation_log")
    agg = h[h.index("SELECT status, COUNT(*)"):h.index("GROUP BY status") + 20]
    assert "{_qc}" in agg, "агрегат считается без учёта поиска"
    rows = h[h.index("if status:"):]
    assert rows.count("{_qc}") >= 2, "строки и агрегат отбираются по-разному"


def test_placeholders_shift_with_the_search_parameter():
    """Номера плейсхолдеров сдвигаются, когда добавляется параметр поиска:
    жёсткие $2/$3 привязали бы статус к строке поиска."""
    h = _handler("operation_log")
    assert "_n = len(_qa)" in h
    # Ни в одной из двух выборок строк не должно остаться жёсткого номера:
    # с параметром поиска он указывал бы на строку поиска вместо статуса.
    rows = h[h.index("if status:"):h.index("return _json_resp(")]
    hard = re.findall(r"(?<!_n \+ )\$([234])\b", rows.replace("${_n + 2}", "").replace("${_n + 3}", "").replace("${_n + 4}", ""))
    assert not hard, f"жёсткие номера плейсхолдеров в выборке строк: {hard}"
    assert rows.count("${_n + 2}") == 2, "сдвиг применён не в обеих выборках"


def test_search_is_length_capped():
    h = _handler("operation_log")
    i = h.index('request.query.get("q")')
    assert "[:64]" in h[i:i + 120], "строка поиска не ограничена по длине"


# ──────────────────────────────── экран ───────────────────────────────────────

def test_screen_sends_the_search_and_debounces_it():
    assert "&q=' + encodeURIComponent(OPLOG_Q)" in _fn_body("_opLogQuery"), (
        "поиск не уходит в запрос")
    body = _fn_body("onOpLogSearch")
    assert "clearTimeout" in body and "setTimeout" in body, "запрос на каждую букву"
    assert "filterOpLog(OPLOG_FILTER)" in body, "поиск сбрасывает выбранный срез"


def test_search_box_is_rendered_with_its_current_value():
    """Экран перерисовывается сам при завершении операции: поле, отрисованное
    пустым, стёрло бы введённое."""
    body = _fn_body("openOpDetail")
    assert 'id="oplogSearch"' in body, "поля поиска по логу нет"
    i = body.index('id="oplogSearch"')
    box = body[i:i + 400]
    assert "value=\"${esc(OPLOG_Q)}\"" in box, "поле рисуется без текущего запроса"
    assert "onOpLogSearch(this.value)" in box
    assert "aria-label=" in box


def test_redraw_of_the_same_operation_keeps_filter_and_search():
    """Операция завершилась, экран перерисовался — человек смотрел неудачные
    цели, а список молча показал бы снова все."""
    body = _fn_body("openOpDetail")
    assert "_sameOp" in body, "срез и поиск сбрасываются при каждой перерисовке"
    i = body.index("_sameOp")
    seg = body[i:i + 260]
    assert "if (!_sameOp)" in seg and "OPLOG_FILTER = ''" in seg and "OPLOG_Q = ''" in seg, (
        "при переходе к ДРУГОЙ операции срез и поиск обязаны сброситься")


def test_header_says_why_the_list_is_shorter():
    body = _fn_body("renderOpLog")
    assert "по поиску" in body and "по срезу" in body and "по срезу и поиску" in body, (
        "«3 из 3» без объяснения читается как потерянный лог")


def test_empty_result_distinguishes_search_from_filter():
    body = _fn_body("renderOpLog")
    assert "По этому поиску записей нет." in body
    assert "По этому срезу записей нет." in body
