"""Экран рассылки всем пользователям был полем ввода и кнопкой — и всё.

Обход 06.11.2026 числил его среди «почти мёртвых»: два действия, двенадцать
знаков текста («📢 Отправить»). Самое далеко идущее действие продукта владелец
запускал, не зная НИ скольким людям оно уйдёт (число он впервые видел в окне
подтверждения), НИ что уже рассылали, НИ чем кончилась прошлая попытка. А
повтор того же текста почти всегда означает «первое нажатие не ответило» — и
именно он писал каждому человеку второй раз.

Эти проверки держат три вещи: экран говорит, кто получит; показывает прошлые
рассылки (включая оборванную); кнопка гаснет на время отправки, а повтор
назван своим словом, а не «отправлено 0».
"""
from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = (ROOT / "mini_app" / "index.html").read_text(encoding="utf-8")


def _fn(name: str) -> str:
    """Тело функции от её объявления до следующего объявления верхнего уровня."""
    i = SRC.index(f"function {name}(")
    nxt = re.search(r"\n(?:async\s+)?function\s", SRC[i + 10:])
    return SRC[i:i + 10 + (nxt.start() if nxt else 4000)]


def test_the_screen_says_who_will_get_it_on_open():
    body = _fn("openAdminBroadcast")
    assert "massTargets('admin_broadcast')" in body, (
        "экран снова открывается, не считая получателей: число впервые "
        "появляется в окне подтверждения, когда решение уже принято")
    assert "adm-bc-who" in body
    assert "loadAdminBroadcastLast()" in body, "прошлые рассылки не загружаются"


def test_the_count_failing_is_said_not_swallowed():
    body = _fn("openAdminBroadcast")
    assert "узнать не удалось" in body, (
        "сбой подсчёта получателей оставил бы пустую строку — владелец решит, "
        "что получателей нет")


def test_the_previous_broadcasts_name_a_broken_one():
    body = _fn("loadAdminBroadcastLast")
    assert "/api/miniapp/admin/broadcast/recent" in body
    assert "ОБОРВАЛАСЬ" in body, (
        "незакрытая запись — рассылка, прерванная на середине: часть людей "
        "письмо не получила, и это главное, что видно из прошлой попытки")
    assert "errHtml(" in body and "retryCall('loadAdminBroadcastLast')" in body, (
        "сбой загрузки прошлых рассылок обязан быть виден и повторяем")


def test_the_button_goes_dark_while_the_broadcast_runs():
    body = _fn("submitAdminBroadcast")
    assert "btn.disabled = true" in body, (
        "живая кнопка во время отправки — это второе сообщение каждому "
        "живому человеку: ответа нет секунды, и владелец нажимает снова")
    assert "if (btn.disabled) return;" in body, (
        "без раннего выхода два нажатия подряд успевают оба до блокировки")
    i = body.index("btn.disabled = true")
    j = body.index("api('/api/miniapp/admin/broadcast'")
    assert i < j, "кнопка гаснет уже после отправки — это не защита"
    assert "finally" in body and "btn.disabled = false" in body, (
        "кнопка должна вернуться: иначе после ошибки экран мёртвый")


def test_a_repeat_is_named_not_reported_as_zero_sent():
    body = _fn("submitAdminBroadcast")
    assert "r.duplicate" in body, (
        "повтор сервер отбивает, но экран обязан сказать об этом: "
        "«отправлено 0» владелец прочтёт как «не сработало» и нажмёт снова")
    assert "уже ушла" in body


def test_the_server_is_the_second_line_not_the_only_one():
    """Кнопка — первый рубеж, окно сервера — второй. Нужны оба.

    Источник берём из `tests.miniapp_source`, а не по `__file__` монолита:
    проверка, прибитая к пути, ослепнет при первом же выносе ручки (сторож:
    tests/test_tests_do_not_pin_the_monolith.py).
    """
    from tests.miniapp_source import api_function

    body = api_function("_admin_broadcast_core")
    assert "_ADMIN_BC_DEDUP_SEC" in body, "окно повтора на сервере снято"
    assert "claim_admin_action" in body, (
        "заявка на рассылку снова не берётся: пока рассылка идёт, второго "
        "нажатия от первого не отличить")
    assert "hashlib.sha256(text.encode" in body, (
        "личность нажатия снова считается не от текста: от времени или от id "
        "она у двух нажатий разная, и дедуп не срабатывает ни разу")
