"""Реферальный экран: кодом наконец можно поделиться, а статусы честные.

Экран показывал уровень, число рефералов, число оплативших, заработок и сам
код — и предлагал «Поделитесь кодом!». Поделиться было нечем: ни копирования,
ни ссылки, ни кнопки. Голый код к тому же бесполезен — непонятно, куда его
вводить; бот (`/referral`) отдаёт t.me/<бот>?start=<код> с кнопкой
«Поделиться» с самого начала.

Второе: сервер возвращал у реферала только два состояния — 'active' и
'pending', — а экран проверял `status === 'paid'`, которого не было НИКОГДА.
Поэтому оплативший реферал показывался как «Зарегистрирован», хотя счётчик
«Оплативших» рядом считал его по paid_at. Два числа об одном человеке
противоречили друг другу на одном экране.
"""
from __future__ import annotations

import ast
import functools
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = os.path.join(ROOT, "mini_app", "index.html")
API = os.path.join(ROOT, "services", "mini_app_api.py")


@functools.lru_cache(maxsize=1)
def _html() -> str:
    with open(HTML, encoding="utf-8") as f:
        return f.read()


def _js_func(name: str) -> str:
    h = _html()
    m = re.search(r"(?:async\s+)?function\s+" + re.escape(name) + r"\s*\([^)]*\)\s*\{", h)
    assert m, f"функция {name} не найдена"
    depth = 0
    for j in range(m.end() - 1, len(h)):
        if h[j] == "{":
            depth += 1
        elif h[j] == "}":
            depth -= 1
            if depth == 0:
                return h[m.start():j + 1]
    raise AssertionError(name)


@functools.lru_cache(maxsize=1)
def _referral_handler() -> str:
    with open(API, encoding="utf-8") as f:
        src = f.read()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) \
                and node.name == "referral_overview_detail":
            return ast.get_source_segment(src, node) or ""
    raise AssertionError("referral_overview_detail не найдена")


def test_the_screen_still_shows_the_code():
    """Антивакуумность."""
    body = _js_func("openReferral")
    assert "ref_code" in body and "referralCode" in body


def test_paid_status_is_actually_returned():
    """Экран ждал 'paid', сервер его не отдавал ни при каких условиях."""
    h = _referral_handler()
    assert 'r.paid_at' in h, "запрос не берёт признак оплаты для строки реферала"
    assert '"paid" if r["paid_at"]' in h, "статус 'paid' по-прежнему недостижим"
    assert '"active" if r["activated_at"]' in h, "потеряно состояние «активен»"


def test_all_three_states_are_shown():
    body = _js_func("openReferral")
    for word in ("Оплатил", "Активен", "Зарегистрирован"):
        assert word in body, f"состояние «{word}» не показывается"


def test_paid_counter_and_row_agree():
    """Счётчик считает по paid_at — строка обязана считать по нему же."""
    h = _referral_handler()
    assert "r.paid_at IS NOT NULL" in h, "счётчик оплативших считает не по оплате"
    assert '"paid" if r["paid_at"]' in h


def test_server_gives_a_link_not_just_a_code():
    h = _referral_handler()
    assert '"ref_link"' in h, "сервер отдаёт код без ссылки"
    assert "_resolve_bot_username()" in h
    assert "?start=" in h


def test_the_code_can_be_copied_and_shared():
    body = _js_func("openReferral")
    assert "refCopyCode()" in body and "refShare()" in body, "поделиться по-прежнему нечем"
    share = _js_func("refShare")
    assert "t.me/share/url" in share, "кнопка «Поделиться» ничего не открывает"
    assert "copyToClipboard" in share, "нет запасного пути, если Telegram не открылся"


def test_old_modal_prefers_the_server_link():
    """Раньше ссылку строили только из window._botUsername: не определился —
    «Скопировать» отвечало отказом, хотя код есть."""
    link = _js_func("refLink")
    assert "if (REF_LINK) return REF_LINK;" in link


def test_empty_state_offers_sharing_instead_of_advising_it():
    body = _js_func("openReferral")
    # Комментарии цитируют старый текст — смотрим только то, что попадёт на экран.
    visible = "\n".join(l for l in body.split("\n") if not l.strip().startswith("//"))
    assert "Поделитесь кодом!" not in visible, "совет поделиться без способа поделиться"
    assert "refShare()" in body and "Пока никто не пришёл" in body


def test_error_has_a_way_out():
    body = _js_func("openReferral")
    assert "'openReferral()')" in body, "с экрана ошибки нет кнопки повтора"
    assert "errHtml(errRu(e)" in body, "текст ошибки снова показывается как есть"
