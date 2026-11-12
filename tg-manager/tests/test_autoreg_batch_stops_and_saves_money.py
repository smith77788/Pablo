"""Пакет авторега слушает «Отменить» и не жжёт деньги владельца на отказах.

Найдено аудитом связки «Авторег → Защита → Восстановление» (12.11.2026):

* `_exec_auto_register` передавал в пакет колбэк, который ВОЗВРАЩАЛ «продолжать
  ли» (храповик `test_cancel_reaches_the_working_loop.py` засчитывал это как
  уважение отмены), но `_do_batch_register` ответ колбэка выбрасывал. «Отменить»
  не останавливала ничего — пакет докупал номера до конца, а номер = деньги.
* Мини-апп шлёт страну ISO-кодом («RU»), а 5sim ждёт «russia», sms-activate —
  «0». Каждый пакет из мини-аппа падал на покупке первого номера.
* Отказ Telegram «на номере нет аккаунта» (регистрировать новые аккаунты
  сторонним приложениям Telegram не даёт) пакет не останавливал: каждый
  следующий номер покупался и падал так же, а владелец видел английский текст.
* Заблокированный номер отменялся как обычный, хотя у 5sim «ban» возвращает
  деньги, а «cancel» — нет. Удачный номер не закрывался («finish»).
"""
from __future__ import annotations

import asyncio

import pytest

from bot.handlers import auto_registrar as ar
from services import account_manager as am
from services import sms_api_engine as sms


PhoneNumberUnoccupiedError = type("PhoneNumberUnoccupiedError", (Exception,), {})
PhoneNumberBannedError = type("PhoneNumberBannedError", (Exception,), {})


class _Sms:
    def __init__(self, fail_buy: bool = False):
        self.bought = 0
        self.cancelled: list[str] = []
        self.banned: list[str] = []
        self.finished: list[str] = []
        self.fail_buy = fail_buy

    async def buy_number(self, country):
        if self.fail_buy:
            raise RuntimeError("5sim buy_number: 400 not enough user balance")
        self.bought += 1
        return {"id": f"o{self.bought}", "phone": f"+7900000000{self.bought}"}

    async def get_sms(self, order_id, timeout_sec=120):
        return "12345"

    async def cancel_order(self, order_id):
        self.cancelled.append(order_id)

    async def report_banned(self, order_id):
        self.banned.append(order_id)

    async def finish_order(self, order_id):
        self.finished.append(order_id)


@pytest.fixture
def stubbed(monkeypatch):
    state = {"start_exc": None, "confirm_exc": None, "saved": 0}

    async def _start_login(phone, proxy_url=None, **kw):
        exc = state["start_exc"]
        if exc is not None:
            raise exc(phone)
        return "hash", "hint"

    async def _confirm_code(phone, code, h):
        exc = state["confirm_exc"]
        if exc is not None:
            raise exc("The phone number is not yet being used (caused by SignInRequest)")
        return object()

    async def _info(phone):
        return "SESSION", {"tg_user_id": 1, "first_name": "A"}

    async def _noop(*a, **k):
        return None

    async def _profile(*a, **k):
        return {}

    async def _save(pool, owner_id, phone, session_str, info, proxy_id=None):
        state["saved"] += 1
        return 100 + state["saved"]

    monkeypatch.setattr(am, "start_login", _start_login)
    monkeypatch.setattr(am, "confirm_code", _confirm_code)
    monkeypatch.setattr(am, "get_client_info_and_session", _info)
    monkeypatch.setattr(am, "cleanup_pending", _noop)
    monkeypatch.setattr(am, "pick_registration_proxy", _noop)
    monkeypatch.setattr(ar.db, "get_autoreg_device_profile", _profile)
    monkeypatch.setattr(ar, "_save_account", _save)
    monkeypatch.setattr(ar, "_INTER_REG_DELAY", 0)
    return state


def _run(coro):
    return asyncio.run(coro)


def test_owner_cancel_stops_buying_numbers(stubbed):
    client = _Sms()
    calls = []

    async def progress(i, ok, failed):
        calls.append(i)
        return i < 2          # «Отменить» нажата после первого номера

    res = _run(ar._do_batch_register(None, 1, "RU", 10, client, None, progress_cb=progress))
    assert client.bought == 1, "после отмены пакет продолжил покупать номера"
    assert res["stopped"].startswith("⏹")
    assert len(res["ok"]) == 1


def test_callback_without_answer_does_not_stop(stubbed):
    """Колбэк, который ничего не возвращает (None), — не отмена."""
    client = _Sms()

    async def progress(i, ok, failed):
        return None

    res = _run(ar._do_batch_register(None, 1, "RU", 3, client, None, progress_cb=progress))
    assert client.bought == 3 and not res["stopped"]


def test_unoccupied_number_stops_the_batch_in_russian(stubbed):
    stubbed["confirm_exc"] = PhoneNumberUnoccupiedError
    client = _Sms()
    res = _run(ar._do_batch_register(None, 1, "RU", 10, client, None))
    assert client.bought == 1, "пакет покупал номера, которые заведомо не станут аккаунтами"
    assert res["stopped"].startswith("⛔")
    assert "сторонним приложениям" in res["stopped"]
    assert "not yet being used" not in " ".join(res["failed"])
    assert client.cancelled == ["o1"]


def test_banned_number_is_reported_as_banned_and_batch_goes_on(stubbed):
    stubbed["start_exc"] = PhoneNumberBannedError
    client = _Sms()
    res = _run(ar._do_batch_register(None, 1, "RU", 3, client, None))
    assert client.banned == ["o1", "o2", "o3"], "забаненный номер отменён без возврата денег"
    assert client.cancelled == []
    assert client.bought == 3 and not res["stopped"]
    assert "заблокирован" in res["failed"][0]


def test_provider_refusal_stops_at_once(stubbed):
    client = _Sms(fail_buy=True)
    res = _run(ar._do_batch_register(None, 1, "RU", 10, client, None))
    assert res["stopped"].startswith("⛔") and "Сервис номеров" in res["stopped"]
    assert res["failed"] == []


def test_successful_number_is_finished(stubbed):
    client = _Sms()
    res = _run(ar._do_batch_register(None, 1, "RU", 2, client, None))
    assert client.finished == ["o1", "o2"]
    assert res["account_ids"] == [101, 102]


@pytest.mark.parametrize("service,iso,native", [
    ("5sim", "RU", "russia"), ("5sim", "GB", "england"), ("5sim", "us", "usa"),
    ("smsactivate", "RU", "0"), ("smsactivate", "UA", "1"), ("smsactivate", "US", "187"),
])
def test_iso_country_reaches_the_service_in_its_own_code(service, iso, native):
    assert sms.service_country(service, iso) == native


@pytest.mark.parametrize("service,code", [("5sim", "russia"), ("smsactivate", "0"),
                                          ("smsactivate", "187")])
def test_native_country_code_passes_through(service, code):
    assert sms.service_country(service, code) == code


def test_executor_reports_owner_cancel_as_cancelled(monkeypatch):
    from services import op_worker as ow

    async def _client(pool):
        return _Sms(), "5sim"

    async def _batch(pool, owner_id, country, cnt, client, status_msg, progress_cb=None):
        return {"ok": ["+1 → id1"], "failed": [], "account_ids": [1],
                "stopped": "⏹ Остановлено владельцем — новые номера больше не покупались."}

    monkeypatch.setattr(ar, "_get_sms_client", _client)
    monkeypatch.setattr(ar, "_do_batch_register", _batch)

    class _Pool:
        async def execute(self, *a, **k):
            return "UPDATE 1"

    res = _run(ow._exec_auto_register(_Pool(), None, 7, 1, {"count": 5, "country": "RU"}))
    assert res["status"] == "cancelled"
    assert "Остановлено владельцем" in res["summary"]
    assert res["handoff"]["account_ids"] == [1]
