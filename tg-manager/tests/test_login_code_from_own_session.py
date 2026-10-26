"""Код входа можно взять из своей же сессии — но только с экрана профиля.

Жалоба владельца: «коды авторизации не приходят». Причина доставки, а не
продукта: Telegram отдаёт код НЕ в SMS, а в приложение — в любую живую сессию
номера. Пока на номере висит чужая сессия (например, от стороннего бота), код
уходит туда.

Выход у продукта уже был: `profile_setter_engine.get_login_code` читает
последний код из служебного чата Telegram (777000) через сессию, которая у нас
есть. Но жил он в «Профиле аккаунта» — то есть на другом экране, не там, где
человек стоит и ждёт код. Дверь есть, а в момент нужды её не видно.

Проверяем две вещи: экран входа узнаёт у сервера, ЕСТЬ ли откуда взять код, и
показывает кнопку только тогда. Кнопка, которая всегда отвечает «неоткуда», —
тот же тупик, только с лишним тапом.
"""
from __future__ import annotations

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _handler(src: str, name: str) -> str:
    i = src.index("async def " + name + "(")
    nxt = re.search(r"\n    async def \w+\(", src[i:])
    return src[i:i + nxt.start()] if nxt else src[i:]


def _js_function(src: str, name: str) -> str:
    i = src.index("function " + name + "(")
    nxt = re.search(r"\n(?:async function|function) \w+\(", src[i + 10:])
    return src[i:i + 10 + nxt.start()] if nxt else src[i:]


def test_start_tells_the_screen_whether_the_code_is_reachable():
    body = _handler(_read("services/mini_app_accounts.py"), "account_add_phone_start")
    assert "code_from_account" in body, (
        "экран входа не узнаёт, есть ли живая сессия на этот номер")
    assert "tg_accounts" in body and "owner_id=$1" in body, (
        "поиск аккаунта по номеру не ограничен владельцем")
    assert "session_str IS NOT NULL" in body, (
        "в ответ попадёт аккаунт без сессии — читать код будет нечем")


def test_the_number_is_matched_by_digits_not_by_plus():
    """В tg_accounts номер лежит как отдал Telegram — часто без «+»."""
    body = _handler(_read("services/mini_app_accounts.py"), "account_add_phone_start")
    assert "[^0-9]" in body, (
        "сверка по сырой строке: '+995…' и '995…' перестанут совпадать, и "
        "кнопка не покажется там, где сессия есть")


def test_the_button_appears_only_when_there_is_something_to_read():
    js = _read("mini_app/screens/account_login.js")
    show = _js_function(js, "_accShowPull")
    assert "display" in show and "none" in show, (
        "кнопку не прячут — она будет всегда отвечать «неоткуда»")
    start = _js_function(js, "accPhoneStart")
    assert "_accShowPull(" in start, (
        "после отправки кода экран не решает, показывать ли кнопку")


def test_the_button_fills_the_code_field():
    js = _read("mini_app/screens/account_login.js")
    pull = _js_function(js, "accPhonePullCode")
    assert "/login_code" in pull, "кнопка не ходит за кодом"
    assert "accPhoneCode" in pull, (
        "код получен, но в поле не попал — владельцу придётся переписывать руками")
    assert "_accCodeAccId" in pull, "кнопка не знает, у какого аккаунта спрашивать"


def test_the_screen_explains_why_this_works():
    html = _read("mini_app/index.html")
    i = html.index('id="accPhonePullNote"')
    note = html[html.index(">", i) + 1:html.index("</div>", i)]
    assert "сесси" in note.lower(), note
    # Имя продукта — не английская фраза; всё остальное владелец должен читать.
    assert not re.findall(r"\b[a-z]{4,}\b", note.replace("Infragram", "")), (
        "пояснение владельцу содержит английские слова")


def test_probe_the_detector_sees_a_screen_without_the_door():
    """Измеритель обязан отличать починенный экран от прежнего."""
    bad = """
function accPhoneStart(){
  const r = 1;
  document.getElementById('accPhoneStep-code').style.display='block';
}
function other(){}
"""
    body = _js_function(bad, "accPhoneStart")
    assert "_accShowPull(" not in body and "function other" not in body
