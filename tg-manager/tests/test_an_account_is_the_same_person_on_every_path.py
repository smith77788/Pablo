"""Аккаунт приходит в Telegram одним и тем же человеком, кто бы ни открыл клиента.

ЧТО БЫЛО. `_make_client` выбирает выход (прокси, релей, IPv6), устройство по
умолчанию и пару api_id/api_hash ПО ПОЛЯМ словаря аккаунта, в первую очередь по
id. Карта транспорта страховала словарь, в котором id есть. Но 22 собственные
выборки (SEO, фабрики каналов и групп, консоль ИИ) строили словарь вообще без
id, а ещё 29 — без сохранённого устройства. Клиент из такого словаря выходил с
адреса хоста вместо своего прокси, с чужой моделью телефона и под другим
приложением — та же сессия, которую минуту назад операция вела через прокси с
её настоящим устройством. Для Telegram это признак угнанной сессии:
ограничение или отзыв ключа.

ЧТО ТЕПЕРЬ. Сессию клиент получает всегда, поэтому аккаунт опознаётся по ней:
id и сохранённое устройство добираются, явно переданное не трогается.
"""
from __future__ import annotations

import asyncio

import pytest

from services import account_manager as am


@pytest.fixture(autouse=True)
def _clean_maps():
    saved = [dict(m) for m in (am._ACC_TRANSPORT, am._SESSION_ACC, am._ACC_DEVICE,
                               am._ACC_SESSION)]
    for m in (am._ACC_TRANSPORT, am._SESSION_ACC, am._ACC_DEVICE, am._ACC_SESSION):
        m.clear()
    yield
    for m, old in zip((am._ACC_TRANSPORT, am._SESSION_ACC, am._ACC_DEVICE,
                       am._ACC_SESSION), saved):
        m.clear()
        m.update(old)


class _Pool:
    def __init__(self, rows):
        self.rows = rows

    async def fetch(self, q, *a):
        return list(self.rows)


_ROW = {"id": 7, "owner_id": 1, "cf_relay_url": None, "proxy_id": 3,
        "proxy_url": "socks5://10.0.0.1:1080", "phone": "+79990001122",
        "session_str": "SESSION-A", "device_model": "Pixel 8 Pro",
        "system_version": "Android 15", "app_version": "11.2.0",
        "lang_code": "ru", "system_lang_code": "ru-RU"}


def test_a_dict_without_id_is_recognised_by_its_session():
    """Выборка «SELECT session_str, phone» (как в SEO и консоли ИИ)."""
    asyncio.run(am.prime_account_transport(_Pool([_ROW])))
    d = am._complete_account_identity("SESSION-A", {"session_str": "SESSION-A",
                                                    "phone": "+79990001122"})
    assert d["id"] == 7
    assert d["device_model"] == "Pixel 8 Pro" and d["system_version"] == "Android 15"
    # А с id карта транспорта добирает и прокси: выход тот же, что у операций.
    assert "proxy_url" in am._fill_transport_fields(d)
    assert d["proxy_url"] == "socks5://10.0.0.1:1080"


def test_a_bare_session_without_any_dict_is_recognised_too():
    asyncio.run(am.prime_account_transport(_Pool([_ROW])))
    d = am._complete_account_identity("SESSION-A", None)
    assert d and d["id"] == 7 and d["device_model"] == "Pixel 8 Pro"


def test_the_saved_device_fills_a_dict_that_has_only_the_id():
    asyncio.run(am.prime_account_transport(_Pool([_ROW])))
    d = am._complete_account_identity("SESSION-A", {"id": 7})
    assert d["device_model"] == "Pixel 8 Pro" and d["app_version"] == "11.2.0"


def test_what_the_caller_passed_is_not_replaced():
    asyncio.run(am.prime_account_transport(_Pool([_ROW])))
    mine = {"id": 7, "device_model": "Samsung SM-S938B", "system_version": "Android 17",
            "app_version": "11.5.3", "lang_code": "en", "system_lang_code": "en-US"}
    assert am._complete_account_identity("SESSION-A", mine) is mine
    # Словарь вызывающего не меняется и тогда, когда добирать есть что.
    short = {"session_str": "SESSION-A"}
    am._complete_account_identity("SESSION-A", short)
    assert short == {"session_str": "SESSION-A"}


def test_an_unknown_session_changes_nothing():
    """Логин и проверка чужой сессии при импорте: опознавать некого."""
    asyncio.run(am.prime_account_transport(_Pool([_ROW])))
    assert am._complete_account_identity("SESSION-ЧУЖАЯ", None) is None
    assert am._complete_account_identity("", {"phone": "1"}) == {"phone": "1"}


def test_a_new_session_replaces_the_old_key_and_a_removed_account_is_forgotten():
    asyncio.run(am.prime_account_transport(_Pool([_ROW])))
    asyncio.run(am.prime_account_transport(_Pool([{**_ROW, "session_str": "SESSION-B"}])))
    assert am._complete_account_identity("SESSION-A", None) is None
    assert am._complete_account_identity("SESSION-B", None)["id"] == 7
    asyncio.run(am.prime_account_transport(_Pool([])))
    assert am._complete_account_identity("SESSION-B", None) is None


def test_the_client_factory_asks_who_it_is_before_choosing_anything():
    """Сторож: опознание стоит ДО выбора устройства и выхода."""
    import inspect
    src = inspect.getsource(am._make_client)
    assert src.index("_complete_account_identity(") < src.index("_normalize_device_profile(")
    assert src.index("_complete_account_identity(") < src.index("_resolve_client_proxy(")
