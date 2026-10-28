"""Регресс: занятость сессии не должна портить оценку прокси.

Сценарий из жизни. Владелец листает диалоги аккаунта в живой консоли, а этим же
аккаунтом прямо сейчас идёт массовая операция. Мьютекс сессии
(`account_manager._wrap_client_session_mutex`) честно не даёт открыть ВТОРОЙ
коннект на одном auth-key и поднимает `SessionBusyError` — ещё ДО того, как
клиент откроет сокет. Прокси в этой попытке не участвовал вообще.

Но `SessionBusyError` — наследник `ConnectionError`, а полтора десятка
raw-коннекторов в `account_manager` ловят `except (OSError, ConnectionError)` и
пишут `_record_proxy_fail`. То есть каждое пересечение консоли с операцией
записывалось здоровому прокси как отказ: `infra_memory.get_proxy_score` падает,
`proxy_selector` начинает обходить рабочий прокси, и аккаунт уезжает на другой
выход. Смена выхода у живой сессии — ровно то, от чего мьютекс и защищает
(AUTH_KEY_DUPLICATED).

Фикс: `_record_proxy_fail` берёт исключение из живого контекста `except`
(`sys.exc_info`) и на `SessionBusyError` не пишет ничего.
"""
from __future__ import annotations

import asyncio
import inspect
import re

import pytest

from services import account_manager as am
from services import infra_memory


ACC = {"id": 77, "proxy_url": "socks5://10.0.0.1:1080", "session_str": "s"}


@pytest.fixture()
def recorded(monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(
        infra_memory, "record_proxy_op",
        lambda url, action, success=True, latency_ms=0.0: calls.append(
            (url, action, success)),
    )
    return calls


def test_busy_session_is_not_a_proxy_failure(recorded):
    try:
        raise am.SessionBusyError("сессия занята другим подключением")
    except ConnectionError:
        am._record_proxy_fail(ACC, "dialogs")
    assert recorded == [], (
        "занятость сессии записана прокси как отказ — здоровый прокси получит "
        "штраф за каждое пересечение консоли с операцией"
    )


def test_real_network_failure_is_still_a_proxy_failure(recorded):
    try:
        raise ConnectionRefusedError("connection refused")
    except ConnectionError:
        am._record_proxy_fail(ACC, "dialogs")
    assert recorded == [(ACC["proxy_url"], "dialogs", False)], (
        "настоящий сетевой отказ обязан снижать оценку прокси"
    )


def test_timeout_is_still_a_proxy_failure(recorded):
    try:
        raise asyncio.TimeoutError()
    except asyncio.TimeoutError:
        am._record_proxy_fail(ACC, "dialogs")
    assert recorded == [(ACC["proxy_url"], "dialogs", False)]


def test_explicit_exception_wins_over_context(recorded):
    """Явно переданное исключение сильнее живого контекста."""
    try:
        raise ConnectionRefusedError("connection refused")
    except ConnectionError:
        am._record_proxy_fail(ACC, "dialogs", exc=am.SessionBusyError("занята"))
    assert recorded == []


def test_outside_except_block_records_as_before(recorded):
    """Вызов не из обработчика (exc_info пуст) работает как раньше."""
    am._record_proxy_fail(ACC, "join")
    assert recorded == [(ACC["proxy_url"], "join", False)]


class _BusyClient:
    """Клиент, у которого мьютекс сессии не дал открыть коннект."""

    def __init__(self):
        self.connect_calls = 0

    async def connect(self):
        self.connect_calls += 1
        raise am.SessionBusyError("сессия занята другим подключением — повтор позже")

    async def disconnect(self):
        return None

    def iter_dialogs(self, *a, **k):  # до сюда не доходит
        raise AssertionError("диалоги не запрашиваются без коннекта")


def test_get_dialogs_on_busy_session_does_not_blame_proxy(monkeypatch, recorded):
    """Сквозная проверка через настоящий raw-коннектор."""
    client = _BusyClient()
    monkeypatch.setattr(am, "_make_client", lambda *a, **k: client)
    out = asyncio.run(am.get_dialogs("session-string", _acc=dict(ACC)))
    assert out == []
    assert client.connect_calls == 1
    assert recorded == [], (
        "get_dialogs записал отказ прокси, хотя сокет не открывался"
    )


def test_guard_lives_in_the_recorder_not_in_call_sites():
    """Храповик: защита должна быть ВНУТРИ `_record_proxy_fail`.

    Её нельзя размазывать по обработчикам: их полтора десятка, и в новом
    обработчике её просто забудут. Тест держит два свойства сразу — что защита
    есть и что каждый существующий вызов идёт через неё.
    """
    body = inspect.getsource(am._record_proxy_fail)
    assert "SessionBusyError" in body, (
        "фильтр занятости сессии ушёл из _record_proxy_fail — вызовы его обойдут"
    )
    assert "sys.exc_info" in body, (
        "без чтения живого контекста защита работает только там, где не забыли "
        "передать исключение"
    )

    src = inspect.getsource(am)
    sites = [m for m in re.finditer(r"_record_proxy_fail\(", src)]
    # 1 определение + вызовы; вызовов должно быть заметно больше десятка —
    # именно поэтому защита централизована.
    assert len(sites) > 10


# ── Вторая половина находки: изоляция аккаунта и совет «исправьте прокси» ─────
#
# `join_channel` и соседи возвращали при занятой сессии `proxy_error=True`.
# В bound-режиме исполнитель по этому флагу изолирует аккаунт ДО КОНЦА операции
# (`op_worker`: `isolated_accounts.add(...)` + `break`) и пишет владельцу
# «❌ Прокси недоступен — смена IP ломает auth key. Исправьте прокси».
# То есть одно пересечение с разбором сущности стоило аккаунта на всю операцию,
# а владельца отправляло чинить исправный прокси.

class _BusyOnConnect:
    def __init__(self):
        self.disconnected = False

    async def connect(self):
        raise am.SessionBusyError("сессия занята другим подключением — повтор позже")

    async def disconnect(self):
        self.disconnected = True

    def __call__(self, *a, **k):
        raise AssertionError("запрос не должен уйти без коннекта")


def _run_join(monkeypatch):
    monkeypatch.setattr(am, "_make_client", lambda *a, **k: _BusyOnConnect())
    return asyncio.run(am.join_channel("session-string", "@some_channel", _acc=dict(ACC)))


def test_join_on_busy_session_is_not_a_proxy_error(monkeypatch, recorded):
    res = _run_join(monkeypatch)
    assert res.get("proxy_error") is not True, (
        "занятая сессия помечена как сбой прокси — исполнитель изолирует аккаунт "
        "до конца операции и посоветует владельцу чинить исправный прокси"
    )
    assert res.get("session_busy") is True
    assert recorded == []


def test_join_busy_message_is_russian_and_does_not_accuse_the_proxy(monkeypatch, recorded):
    res = _run_join(monkeypatch)
    text = res.get("error") or ""
    assert "прокси" not in text.lower(), (
        "владельцу нельзя показывать «прокси?» там, где прокси не участвовал"
    )
    assert "занят" in text.lower()
    assert not re.search(r"[A-Za-z]{3,}", text), f"текст владельцу не по-русски: {text}"


def test_join_busy_is_not_mistaken_for_a_dead_session(monkeypatch, recorded):
    """Текст занятости не должен попадать под разбор «сессия мертва».

    Иначе исполнитель деактивирует живой аккаунт (is_active=FALSE) из-за того,
    что владелец параллельно открыл разбор канала.
    """
    from services.op_errors import _is_dead_session_error

    res = _run_join(monkeypatch)
    assert _is_dead_session_error(res.get("error") or "") is False


def test_leave_channel_busy_keeps_the_ok_contract(monkeypatch, recorded):
    monkeypatch.setattr(am, "_make_client", lambda *a, **k: _BusyOnConnect())
    res = asyncio.run(am.leave_channel("session-string", 12345, _acc=dict(ACC)))
    assert res.get("ok") is False
    assert res.get("proxy_error") is not True
    assert res.get("session_busy") is True


def test_every_proxy_error_return_is_guarded(monkeypatch):
    """Храповик: ни один `except (OSError, ConnectionError)`, возвращающий
    `proxy_error`, не должен обходить фильтр занятости."""
    src = inspect.getsource(am).split("\n")
    unguarded = []
    for i, line in enumerate(src):
        if "proxy_error" not in line or "True" not in line:
            continue
        window = "\n".join(src[max(0, i - 12):i])
        if "except (OSError, ConnectionError)" not in window:
            continue  # ветка таймаута — SessionBusyError туда не приходит
        if "_busy_session_result" not in window:
            unguarded.append(i + 1)
    assert not unguarded, (
        f"строки {unguarded}: занятая сессия вернётся как сбой прокси"
    )
