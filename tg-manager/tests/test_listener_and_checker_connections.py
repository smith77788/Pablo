"""Слушатель аудитории и проверщик регистрации не теряют живые коннекты.

Тот же класс, что был в QR-входе, ещё в двух местах.

Слушатель (`audience_listener._start_one`) подключал клиента, проверял
авторизацию и только ПОТОМ клал его в `_listening`. Сессия не авторизована —
основная причина сюда не дойти, и на этом пути подключённый клиент оставался
без единой ссылки: `_stop_one` его не найдёт, отключить некому. Слушатель
поднимается по многим аккаунтам и перезапускается, так что промахи копились.

Проверщик (`registration_checker._get_telethon_client`) строил клиента как
`_make_client(acc["session_str"])` — без словаря аккаунта, хотя он тут же,
рядом. Транспорт выбирается по его полям (`proxy_id`, `cf_relay_url`, `id`),
поэтому проверка уходила НАПРЯМУЮ с host-IP, пока остальные подсистемы того же
аккаунта шли через назначенный прокси.

Цена в обоих случаях одна и та же и она не про память: второй коннект на той же
сессии (или тот же аккаунт с двух адресов) — это AUTH_KEY_DUPLICATED, после
которого Telegram отзывает ключ и аккаунт мёртв.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services import audience_listener, registration_checker


def _run(coro):
    return asyncio.run(coro)


def _client(*, authorized=True, connect_error=None):
    client = MagicMock()
    client.connect = AsyncMock(side_effect=connect_error)
    client.disconnect = AsyncMock()
    client.is_user_authorized = AsyncMock(return_value=authorized)
    client.add_event_handler = MagicMock()
    return client


class _FakePool:
    pass


@pytest.fixture(autouse=True)
def _clean_listening():
    audience_listener._listening.clear()
    yield
    audience_listener._listening.clear()


# ── слушатель аудитории ──────────────────────────────────────────────────────

def _patch_account(acc):
    """get_account_for_telethon возвращает наш аккаунт."""
    fake_db = MagicMock()
    fake_db.get_account_for_telethon = AsyncMock(return_value=acc)
    return patch.dict("sys.modules", {"database.db": fake_db})


def test_unauthorized_session_does_not_leak_a_live_connection():
    client = _client(authorized=False)
    acc = {"session_str": "s", "owner_id": 5, "id": 7}

    with patch("database.db.get_account_for_telethon", AsyncMock(return_value=acc)):
        with patch("services.account_manager._make_client", return_value=client):
            with patch("services.op_worker.try_claim_account",
                       AsyncMock(return_value=True), create=True):
                with patch("services.op_worker.release_accounts",
                           AsyncMock(), create=True):
                    ok = _run(audience_listener._start_one(_FakePool(), MagicMock(), 7))

    assert ok is False
    client.disconnect.assert_awaited(), (
        "подключённый клиент потерян: _stop_one его не найдёт, мьютекс сессии "
        "держит аккаунт"
    )
    assert 7 not in audience_listener._listening


def test_successful_start_keeps_the_client_listening():
    client = _client(authorized=True)
    acc = {"session_str": "s", "owner_id": 5, "id": 8}

    with patch("database.db.get_account_for_telethon", AsyncMock(return_value=acc)):
        with patch("services.account_manager._make_client", return_value=client):
            with patch("services.op_worker.try_claim_account",
                       AsyncMock(return_value=True), create=True):
                ok = _run(audience_listener._start_one(_FakePool(), MagicMock(), 8))

    assert ok is True
    assert audience_listener._listening.get(8) is client
    client.disconnect.assert_not_awaited()
    client.add_event_handler.assert_called_once()


# ── проверщик регистрации ────────────────────────────────────────────────────

def test_checker_passes_the_whole_account_so_transport_is_chosen_right():
    client = _client()
    acc = {
        "id": 42, "session_str": "sess", "proxy_id": 3,
        "cf_relay_url": "https://relay.example", "owner_id": 9,
    }
    seen = {}

    def _capture(session_string, device=None, *a, **k):
        seen["session"] = session_string
        seen["device"] = device
        return client

    with patch("services.resource_selector.select_all_active",
               AsyncMock(return_value=[acc])):
        with patch("services.account_manager._make_client", side_effect=_capture):
            got_client, got_acc = _run(
                registration_checker._get_telethon_client(_FakePool(), 9))

    assert got_client is client and got_acc is acc
    assert seen["device"] is not None, (
        "словарь аккаунта не передан — клиент пойдёт напрямую с host-IP, "
        "не тем выходом, что остальные подсистемы аккаунта"
    )
    for key in ("id", "proxy_id", "cf_relay_url"):
        assert seen["device"].get(key) == acc[key]


def test_checker_does_not_leak_when_the_connection_fails():
    """Вызывающему клиент не достанется — закрыть его может только эта функция."""
    client = _client(connect_error=asyncio.TimeoutError())
    acc = {"id": 42, "session_str": "sess", "proxy_id": None, "cf_relay_url": ""}

    with patch("services.resource_selector.select_all_active",
               AsyncMock(return_value=[acc])):
        with patch("services.account_manager._make_client", return_value=client):
            with pytest.raises(asyncio.TimeoutError):
                _run(registration_checker._get_telethon_client(_FakePool(), 9))

    client.disconnect.assert_awaited_once()


def test_checker_returns_nothing_when_there_is_no_account():
    with patch("services.resource_selector.select_all_active",
               AsyncMock(return_value=[])):
        assert _run(registration_checker._get_telethon_client(_FakePool(), 9)) == (None, None)
