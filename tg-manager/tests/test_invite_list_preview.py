"""Регрессия: устойчивый разбор «Своего списка» + предпросмотр до запуска.

classify_invite_list — единый разбор вставленного списка на телефоны /
@username-ID / нераспознанное. Должен:
  * принимать номера в человеческом формате (с «+» и без, со скобками/дефисами/
    пробелами внутри одного номера);
  * не путать короткий числовой ID (≤10 цифр) с телефоном;
  * несколько токенов в строке через пробел разбирать по отдельности;
  * складывать непонятые строки в unrecognized (а не терять молча).
Эндпоинт /api/miniapp/invite/parse_list отдаёт счётчики для предпросмотра.
"""
from __future__ import annotations

import inspect
import re

from services.mass_inviter_engine import classify_invite_list, split_invite_targets
from services import mini_app_api


def test_bare_and_plus_numbers_recognized():
    r = classify_invite_list("79111491199\n+79265285721")
    assert r["phones"] == ["+79111491199", "+79265285721"]
    assert r["user_refs"] == [] and r["unrecognized"] == []


def test_formatted_phone_not_split():
    # Пробелы/скобки/дефисы ВНУТРИ одного номера не должны рвать его на куски.
    r = classify_invite_list("+7 (999) 123-45-67")
    assert r["phones"] == ["+79991234567"], r
    assert r["unrecognized"] == []


def test_short_numeric_is_id_not_phone():
    r = classify_invite_list("123456789")
    assert r["user_refs"] == ["123456789"] and r["phones"] == []


def test_multiple_tokens_per_line_split():
    r = classify_invite_list("@alice @bob 79111491199")
    assert "@alice" in r["user_refs"] and "@bob" in r["user_refs"]
    assert r["phones"] == ["+79111491199"]


def test_unrecognized_reported():
    r = classify_invite_list("ok_user\n$$\nab")   # 'ab' <3 симв, '$$' мусор
    assert "ab" in r["unrecognized"] and "$$" in r["unrecognized"]
    assert r["user_refs"] == ["@ok_user"]


def test_split_wrapper_backcompat():
    refs, phones = split_invite_targets("@a1_b\n79111491199\n12345")
    assert phones == ["+79111491199"]
    assert "@a1_b" in refs and "12345" in refs
    assert not (set(refs) & set(phones))


def test_parse_list_endpoint_registered_and_uses_classifier():
    src = inspect.getsource(mini_app_api)
    assert 'app.router.add_post("/api/miniapp/invite/parse_list", invite_parse_list)' in src
    m = re.search(r"async def invite_parse_list\(.*?\n(.*?)\n    async def ", src, re.DOTALL)
    assert m, "invite_parse_list handler not found"
    body = m.group(1)
    assert "classify_invite_list" in body and "if not uid" in body
