"""Перед рассылкой в ЛС видно, что именно уйдёт человеку.

Рассылка уходит живым людям и не отменяется, а до нажатия автор видел только
исходный шаблон: что развернёт spintax и что встанет вместо {name}, выяснялось
уже после отправки. Экран обещал «spintax обязателен», но текст без вариантов
уходил молча — а одинаковый текст тысяче людей и есть самая заметная
спам-сигнатура.

Ключевое требование: предпросмотр обязан рендерить ТЕМИ ЖЕ функциями и в ТОМ
ЖЕ порядке, что и исполнитель (dm_engine: personalize → expand_spintax).
Порядок здесь не косметика: spintax-движок видит `{name}` как группу из одного
варианта и превращает её в голое слово «name». Разъедься предпросмотр с
исполнителем — он показывал бы не то, что уйдёт, а это хуже, чем не
показывать ничего.
"""
from __future__ import annotations

import re
from pathlib import Path

from tests.miniapp_source import miniapp_html, miniapp_source

SRC = miniapp_source()
HTML = miniapp_html()
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


# ───────────────────────── рендер = рендер исполнителя ────────────────────────

def test_preview_uses_the_executor_functions():
    h = _handler("dm_preview")
    assert "from services.dm_engine import expand_spintax, personalize" in h, (
        "предпросмотр рендерит сам — это второй источник правды")
    assert "expand_spintax(personalize(" in h, (
        "порядок перепутан: spintax до подстановки имени превратит {name} в слово «name»")


def test_executor_still_renders_in_that_order():
    """Если исполнитель поменяет порядок, предпросмотр обязан поехать за ним."""
    eng = (Path(__file__).resolve().parent.parent / "services" / "dm_engine.py").read_text(encoding="utf-8")
    assert "expand_spintax(personalize(" in eng, (
        "исполнитель рендерит иначе — предпросмотр разошёлся с отправкой")


def test_route_is_registered():
    assert 'app.router.add_post("/api/miniapp/dm/preview", dm_preview)' in API


def test_verdict_is_drawn_from_more_samples_than_shown():
    """На шаблоне из двух вариантов три совпавших броска выпадают в четверти
    случаев: по трём показанным нельзя судить, меняется ли текст."""
    h = _handler("dm_preview")
    m = re.search(r"_DRAWS = (\d+)", h)
    assert m and int(m.group(1)) >= 8, "мало проб — предупреждение будет врать на здоровом тексте"
    assert "same_for_everyone" in h and "len(set(draws)) == 1" in h, (
        "вердикт считается не по всем пробам")
    assert re.search(r"len\(variants\) == 3", h), "показываем не три варианта"


def test_shown_variants_prefer_distinct_draws():
    h = _handler("dm_preview")
    assert "if d not in seen:" in h, "три одинаковых броска выглядели бы как отсутствие spintax"


def test_broken_template_is_a_message_not_a_500():
    h = _handler("dm_preview")
    assert "Шаблон не разворачивается" in h and "400" in h


# ──────────────────────────────── экран ───────────────────────────────────────

def test_composer_has_a_preview_button():
    assert 'onclick="dmPreview()"' in HTML, "кнопки предпросмотра нет"
    assert 'id="daPreview"' in HTML, "некуда выводить предпросмотр"
    i, j = HTML.index('onclick="dmPreview()"'), HTML.index('onclick="submitDmAdhoc()"')
    assert i < j, "предпросмотр стоит после кнопки отправки — его не заметят"


def test_confirmation_shows_a_rendered_sample():
    """В подтверждении — то, что уйдёт, а не шаблон автора."""
    body = _fn_body("submitDmAdhoc")
    assert "_dmRender(" in body, "подтверждение показывает шаблон, а не результат"
    assert "Пример того, что уйдёт" in body
    i = body.index("_dmRender(")
    assert body.index("askConfirm(") > i, "пример считается уже после вопроса"


def test_sample_failure_does_not_block_sending():
    """Недоступный предпросмотр — не повод запретить отправку, но и примера
    выдумывать нельзя."""
    body = _fn_body("submitDmAdhoc")
    i = body.index("_dmRender(")
    seg = body[i - 200:i + 600]
    assert "catch(_)" in seg, "сбой предпросмотра уронит отправку"
    assert "sampleNote = ''" in body, "при сбое остался бы пример от прошлого текста"


def test_identical_text_is_called_out_in_both_places():
    prev = _fn_body("dmPreview")
    assert "same_for_everyone" in prev and "спам-сигнатура" in prev
    conf = _fn_body("submitDmAdhoc")
    assert "same_for_everyone" in conf and "спам-сигнатура" in conf, (
        "предупреждение есть в предпросмотре, но не там, где нажимают «отправить»")


def test_preview_escapes_what_it_shows():
    """Текст пишет пользователь, и он попадает в innerHTML."""
    body = _fn_body("dmPreview")
    assert "esc(v)" in body, "вариант вставляется в разметку без экранирования"
    assert "esc(r.name_used)" in body
