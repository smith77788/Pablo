"""Три числа на экране пользователя вели в никуда.

Обход 06.11.2026 числил `s-admin-user-detail` среди «почти мёртвых»: два
действия, 261 знак. На экране стояли «🤖 Ботов: 7», «📡 Каналов: 14»,
«📱 Аккаунтов: 25» — и ни одно число не открывалось. В поддержке («мой бот
перестал отвечать») владелец видел счётчик и шёл в базу руками.

Решение о доступе здесь не новое: админ-панель БОТА показывает все боты
системы с их владельцами и умеет «Экспорт токенов», то есть строго больше, чем
этот экран. Поэтому важно другое — чего в выдаче НЕТ (токен, строка сессии,
полный телефон) и что ноль дверью не становится: открывать пустое незачем.
"""
from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = (ROOT / "mini_app" / "index.html").read_text(encoding="utf-8")


def _fn(name: str) -> str:
    i = SRC.index(f"function {name}(")
    nxt = re.search(r"\n(?:async\s+)?function\s", SRC[i + 10:])
    return SRC[i:i + 10 + (nxt.start() if nxt else 6000)]


def test_each_count_opens_its_list():
    body = _fn("openAdminUserDetail")
    assert "openAdminUserAssets('${kind}')" in body, (
        "числа снова ничего не открывают")
    for kind in ("bots", "channels", "accounts"):
        assert f"d.{kind}, '{kind}')" in body, f"число «{kind}» осталось тупиком"
    assert 'role="button"' in body, "дверь без роли не дверь для скринридера"


def test_a_zero_is_not_a_door():
    body = _fn("openAdminUserDetail")
    i = body.index("const udCnt")
    head = body[i:i + 700]
    assert "Number(n) > 0" in head, (
        "ноль тоже стал дверью: открывать пустой список незачем, и это снова "
        "тупик, только с лишним нажатием")


def test_the_list_screen_exists_and_can_be_retried():
    assert 'id="s-admin-user-assets"' in SRC, "экрана списков нет"
    body = _fn("loadAdminUserAssets")
    assert "/api/miniapp/admin/user/" in body and "/assets" in body
    assert "retryCall('loadAdminUserAssets')" in body, (
        "сбой загрузки без повтора оставляет экран мёртвым")
    assert "empty(" in body, "пустой раздел должен говорить, что он пуст"


def test_the_screen_shows_the_masked_phone_the_server_sends():
    body = _fn("loadAdminUserAssets")
    assert "phone_masked" in body, (
        "экран снова читает поле телефона напрямую: сервер отдаёт только хвост")
    assert "session_str" not in body and "token" not in body, (
        "на экране появились секреты чужих сущностей")


def test_the_handler_sends_no_secrets():
    src = (ROOT / "services" / "mini_app_admin.py").read_text(encoding="utf-8")
    i = src.index("async def admin_user_assets")
    j = src.index("async def admin_broadcast", i)
    body = src[i:j]
    assert "token" not in body, "в выдачу имущества попал токен бота"
    assert "session_str" not in body, "в выдачу имущества попала строка сессии"
    assert "_phone_masked(" in body, "телефон отдаётся целиком"
    assert "_is_admin(uid)" in body, "ручка доступна не только владельцу"


def test_a_field_that_is_not_a_list_does_not_kill_the_screen():
    """`/admin/user/{id}` и `/admin/user/{id}/assets` — одни имена, разный смысл.

    У детали `bots` — ЧИСЛО («🤖 Ботов: 7»), у списков — список. Заглушка
    обхода отвечала числом на оба адреса, экран падал на `7.map` и уходил в
    отчёт как «ОТВЕТИЛИ ОШИБКОЙ». В приложении такой находки нет, но вывод
    общий: поле не того вида обязано давать пустой раздел, а не «Что-то пошло
    не так», по которому не понять ничего.
    """
    body = _fn("loadAdminUserAssets")
    assert "Array.isArray(" in body, (
        "экран снова зовёт .map на том, что пришло: сменился контракт — и "
        "вместо данных общая ошибка")

    harness = (ROOT / "deploy" / "scripts" / "render_miniapp.mjs").read_text(encoding="utf-8")
    i = harness.index("url.indexOf('/admin/user/') >= 0 && url.indexOf('/assets') >= 0")
    # Именно ветка ЭТОГО адреса: `o.bots = 7` есть и у экрана топологии выше.
    j = harness.index("o.bots = 7;", harness.index("if (url.indexOf('/admin/user/')"))
    assert i < j, (
        "ветка заглушки для списков должна стоять ДО ветки с числами, иначе "
        "экран списков снова получит числа")
