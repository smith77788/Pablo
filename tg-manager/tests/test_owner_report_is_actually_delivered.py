"""Отчёт об операции доходит до владельца, а сбой доставки виден.

Уведомление об итоге операции — ЕДИНСТВЕННЫЙ отчёт, который владелец получает о
работе, сделанной его аккаунтами: сколько приглашений ушло, сколько постов
опубликовано, на чём операция встала. Доставлялся он одной попыткой «как
получится», и у этого было две беды.

ПЕРВАЯ. Слот защиты от спама занимался ДО отправки: `_notify_cooldown[key] =
now`, а `bot.send_message` шёл следом. Сбой сети, таймаут или 5xx Telegram
сжигали окно на минуту — а повторять было некому, и отчёт владелец не получал
вообще. Операция при этом считалась завершённой и отчитанной.

ВТОРАЯ. Повторных попыток не было совсем, и недоставка уходила в лог на
WARNING — то есть снаружи (в метриках, на экранах) её не видно никак.

ФИКС: две повторные попытки с паузой (с уважением к `retry_after` от Telegram),
слот освобождается при неудаче, окончательная недоставка — log.error и счётчик
`infragram_notifications_undelivered_total`. Постоянные отказы (владелец не
нажал /start, заблокировал бота, удалил аккаунт) не повторяются: повтор там
только жжёт лимиты.
"""
from __future__ import annotations

import asyncio

import pytest

from database import db


class _Pool:
    async def fetchrow(self, query, *args):
        return None

    async def fetch(self, query, *args):
        return []

    async def execute(self, query, *args):
        return "OK"

    async def fetchval(self, query, *args):
        return None


class _Bot:
    """Бот, у которого первые `fail_first` отправок не проходят."""

    def __init__(self, fail_first=0, exc=None):
        self.fail_left = fail_first
        self.exc = exc or RuntimeError("Bad Gateway")
        self.sent: list[str] = []

    async def send_message(self, user_id, text, **kw):
        if self.fail_left > 0:
            self.fail_left -= 1
            raise self.exc
        self.sent.append(text)


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    """Паузы между попытками в тесте не нужны — проверяем поведение, не часы."""
    async def _no_sleep(_s):
        return None

    monkeypatch.setattr(db.asyncio, "sleep", _no_sleep)
    db._notify_cooldown.clear()
    yield
    db._notify_cooldown.clear()


def _notify(bot, *, user=555, pref="op_complete", key="done:1"):
    return asyncio.run(db.notify_if_enabled(
        _Pool(), bot, user, pref, "итог операции", dedup_key=key))


def test_a_transient_failure_is_retried_until_it_lands():
    bot = _Bot(fail_first=2)
    _notify(bot)
    assert bot.sent == ["итог операции"], (
        "отчёт об итоге операции потерян на временном сбое сети")


def test_a_failed_delivery_does_not_burn_the_window():
    """Сбой не должен запирать следующий обоснованный повод сообщить."""
    bot = _Bot(fail_first=99)
    _notify(bot)
    assert bot.sent == []
    # следующая попытка — уже с живым ботом, окно не должно её блокировать
    ok_bot = _Bot()
    _notify(ok_bot)
    assert ok_bot.sent == ["итог операции"], (
        "недоставленное уведомление сожгло окно антиспама: владелец не узнает "
        "об операции и со второго повода")


def test_a_successful_delivery_still_holds_the_window():
    """Обратная сторона: защита от спама не сломана."""
    bot = _Bot()
    _notify(bot)
    _notify(bot)
    assert len(bot.sent) == 1, "окно антиспама перестало работать"


def test_an_undelivered_report_is_visible_outside():
    from services import metrics

    metrics.reset()
    _notify(_Bot(fail_first=99))
    counters = metrics.snapshot().get("counters") or {}
    assert any("infragram_notifications_undelivered_total" in str(k)
               for k in counters), (
        f"недоставленный отчёт не попал в метрики: {counters}")
    assert ("infragram_notifications_undelivered_total"
            in metrics._HELP), "у метрики нет описания — она не попадёт в выдачу"


def test_a_permanent_refusal_is_not_retried():
    """Владелец не нажал /start — повтор только жжёт лимиты Telegram."""
    bot = _Bot(fail_first=99, exc=RuntimeError(
        "Forbidden: bot was blocked by the user"))
    _notify(bot)
    assert db._notify_failure_is_permanent(
        RuntimeError("Forbidden: bot was blocked by the user"))
    assert not db._notify_failure_is_permanent(RuntimeError("Bad Gateway"))


def test_telegram_waiting_time_is_respected(monkeypatch):
    """Если Telegram сказал, сколько ждать, берём его цифру, а не свою."""
    waited: list[float] = []

    async def _spy_sleep(s):
        waited.append(s)

    monkeypatch.setattr(db.asyncio, "sleep", _spy_sleep)

    class _Flood(Exception):
        retry_after = 7

    bot = _Bot(fail_first=1, exc=_Flood("Too Many Requests"))
    _notify(bot)
    assert bot.sent, "уведомление не доехало после флуд-паузы"
    assert waited and waited[0] == 7.0, (
        f"пауза взята не из ответа Telegram: {waited}")
