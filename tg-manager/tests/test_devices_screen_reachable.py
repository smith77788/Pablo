"""Связанные устройства можно увидеть и отозвать.

Вне Telegram вход в мини-апп идёт по коду из бота (`/api/miniapp/pair`), и
браузер запоминается надолго: его ключ лежит в localStorage и меняется на
сессию сам. Половина этой пары была без интерфейса — `/api/miniapp/devices` и
`/devices/revoke` с фронта не звал никто. Потерянный или чужой браузер отозвать
было нечем.

Отзыв — необратимое действие для того, кто им пользуется, поэтому он
спрашивает подтверждение и честно предупреждает: если это текущий браузер,
войти придётся заново по новому коду.
"""
from __future__ import annotations

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
    assert m, f"функция {name} в мини-аппе не найдена"
    depth = 0
    for j in range(m.end() - 1, len(h)):
        if h[j] == "{":
            depth += 1
        elif h[j] == "}":
            depth -= 1
            if depth == 0:
                return h[m.start():j + 1]
    raise AssertionError(f"не удалось найти конец функции {name}")


def test_pairing_exists_so_the_screen_is_needed():
    """Антивакуумность: если связывание убрали, проверки ниже бессмысленны."""
    h = _html()
    assert "askPairingCode" in h and "/api/miniapp/pair" in h


def test_there_is_a_way_in():
    h = _html()
    assert 'onclick="openDevices()"' in h, "к устройствам нет входа из настроек"
    assert 'id="s-devices"' in h, "экрана устройств нет"


def test_screen_lists_and_revokes():
    body = _js_func("openDevices")
    assert "/api/miniapp/devices" in body
    assert "revokeDevice" in body, "отозвать устройство нечем"
    rev = _js_func("revokeDevice")
    assert "/api/miniapp/devices/revoke" in rev
    assert "askConfirm" in rev, "отзыв доступа без подтверждения"
    assert "заново" in rev, "не предупреждает, что текущий браузер выйдет"


def test_revoked_devices_are_marked_not_hidden():
    """Скрыть отозванное — значит потерять след: было устройство или не было."""
    body = _js_func("openDevices")
    assert "отозвано" in body
    assert "r.revoked ? ''" in body.replace("\n", " ") or "r.revoked" in body


def test_screen_admits_the_server_cap():
    """Сервер отдаёт последние 50 — список не должен выдавать себя за полный."""
    body = _js_func("openDevices")
    assert "50" in body, "потолок выдачи не признаётся"
    src = open(API, encoding="utf-8").read()
    assert "devices_list" in src


def test_error_has_a_way_out():
    assert "errHtml(e.message, 'openDevices()')" in _js_func("openDevices")
