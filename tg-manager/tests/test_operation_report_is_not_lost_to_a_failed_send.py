"""Отчёт об операции не теряется навсегда из-за сбоя отправки.

РАЗРЫВ. Судьбу операции владельцу говорит `op_worker._notify_owner_about_op`, и
перед отправкой он занимает ПЕРСИСТЕНТНУЮ отметку анти-повтора на 48 часов
(`_OP_NOTIFY_DEDUP_S`): исход операции — событие однократное, и после каждого
деплоя повторять его нельзя.

Отметка занималась ДО отправки и при неудаче не возвращалась. А `notify_if_enabled`
все сбои глотает: три попытки, метрика, log.error — и `return None`, то есть
вызывающий не может отличить доставленное от потерянного. Значит один временный
сбой (Telegram ответил 5xx, сеть моргнула) сжигал окно на двое суток, и отчёт
об операции владелец не получал ВООБЩЕ. Для рассылки или инвайта это
единственный отчёт о работе, сделанной его аккаунтами.

Тот же класс в этом коде уже дважды закрывали: у внутрипроцессного кулдауна в
`notify_if_enabled` слот при неудаче возвращается («раньше сбой сети сжигал окно
на минуту, повторять было некому»), а у алерта о застрявших операциях отметка
ставится после доставки («операция помечалась объявленной ДО отправки, поэтому
ошибка доставки теряла алерт навсегда»). Персистентное окно отчёта об операции
осталось единственным, где отметка опережала доставку.

ЧТО СЧИТАТЬ НЕУДАЧЕЙ. Не всякая: владелец заблокировал бота или не нажал /start
— повторять бессмысленно (`_NOTIFY_PERMANENT_MARKS`), и отметку надо оставить,
иначе каждый круг воркера будет жечь лимиты Telegram впустую. Отключённое
владельцем уведомление — тоже не неудача. Возвращать отметку надо ровно на
ВРЕМЕННОМ сбое.
"""
from __future__ import annotations

import asyncio

import pytest


@pytest.fixture
def db_mod():
    from database import db
    return db


class _Bot:
    def __init__(self, fail: Exception | None = None):
        self.fail = fail
        self.calls = 0

    async def send_message(self, uid, text, **kw):
        self.calls += 1
        if self.fail:
            raise self.fail


class _Pool:
    """Пул для анти-повтора: помнит занятые ключи и снятые отметки."""

    def __init__(self):
        self.taken: set[tuple[int, str]] = set()
        self.forgotten: list[tuple] = []

    async def fetchrow(self, q, *a):
        if "notification_dedup" in q:
            key = (int(a[0]), str(a[1]))
            if key in self.taken:
                return None
            self.taken.add(key)
            return {"user_id": a[0]}
        return None

    async def execute(self, q, *a):
        if "notification_dedup" in q and q.strip().upper().startswith("DELETE"):
            self.forgotten.append(tuple(a))
            self.taken.discard((int(a[0]), str(a[1])))
        return "DELETE 1"

    async def fetch(self, q, *a):
        return []


@pytest.fixture(autouse=True)
def _fast_and_enabled(db_mod, monkeypatch):
    """Без ожиданий между попытками и с включённым уведомлением."""
    monkeypatch.setattr(db_mod, "_NOTIFY_RETRY_DELAYS", ())

    async def _settings(pool, uid):
        return {}

    monkeypatch.setattr(db_mod, "get_notification_settings", _settings)
    db_mod._notify_cooldown.clear()
    yield
    db_mod._notify_cooldown.clear()


# ── Контракт доставки: вызывающий обязан узнать исход ────────────────────────

def test_delivered_notification_reports_success(db_mod):
    bot = _Bot()
    ok = asyncio.run(db_mod.notify_if_enabled(
        _Pool(), bot, 7, "op_complete", "готово"))
    assert ok is True, "успешная доставка не отчитывается об успехе"
    assert bot.calls == 1


def test_a_temporary_failure_reports_failure(db_mod):
    bot = _Bot(RuntimeError("Telegram server error 502"))
    ok = asyncio.run(db_mod.notify_if_enabled(
        _Pool(), bot, 7, "op_complete", "готово"))
    assert ok is False, (
        "временный сбой доставки выглядит как успех — вызывающий не узнает, "
        "что отчёт об операции до владельца не дошёл")


def test_a_permanent_refusal_is_not_worth_retrying(db_mod):
    """Заблокированный бот — не повод жечь лимиты Telegram каждый круг."""
    bot = _Bot(RuntimeError("Forbidden: bot was blocked by the user"))
    ok = asyncio.run(db_mod.notify_if_enabled(
        _Pool(), bot, 7, "op_complete", "готово"))
    assert ok is True, "отказ, который повторять бессмысленно, зовёт на повтор"


def test_a_notification_the_owner_switched_off_is_not_a_failure(db_mod, monkeypatch):
    async def _settings(pool, uid):
        return {"op_complete": False}

    monkeypatch.setattr(db_mod, "get_notification_settings", _settings)
    bot = _Bot()
    ok = asyncio.run(db_mod.notify_if_enabled(
        _Pool(), bot, 7, "op_complete", "готово"))
    assert ok is True and bot.calls == 0


# ── Главное: окно не сжигается потерянным отчётом ────────────────────────────

def test_a_lost_report_frees_the_window_for_another_try():
    from services import op_worker

    pool = _Pool()
    bot = _Bot(RuntimeError("Telegram server error 502"))
    asyncio.run(op_worker._notify_owner_about_op(
        pool, bot, 7, "итог операции", dedup_key="done:1"))
    assert bot.calls >= 1, "подготовка: отправка не вызывалась"
    assert pool.forgotten, (
        "окно анти-повтора осталось занятым после потерянной доставки — "
        "владелец не узнает об операции ещё 48 часов")

    # Второй заход обязан снова дойти до отправки.
    bot2 = _Bot()
    asyncio.run(op_worker._notify_owner_about_op(
        pool, bot2, 7, "итог операции", dedup_key="done:1"))
    assert bot2.calls == 1, "повторная попытка отчёта так и не состоялась"


def test_a_delivered_report_keeps_the_window():
    """Иначе владелец получал бы один и тот же итог после каждого деплоя."""
    from services import op_worker

    pool = _Pool()
    bot = _Bot()
    asyncio.run(op_worker._notify_owner_about_op(
        pool, bot, 7, "итог операции", dedup_key="done:2"))
    assert bot.calls == 1 and not pool.forgotten

    bot2 = _Bot()
    asyncio.run(op_worker._notify_owner_about_op(
        pool, bot2, 7, "итог операции", dedup_key="done:2"))
    assert bot2.calls == 0, "итог отправлен владельцу второй раз"


def test_forgetting_the_window_is_scoped_to_one_key(db_mod):
    """Снятие отметки не должно открывать шлюз остальным уведомлениям."""
    pool = _Pool()
    asyncio.run(db_mod.notify_dedup_ok(pool, 7, "op:a", 60))
    asyncio.run(db_mod.notify_dedup_ok(pool, 7, "op:b", 60))
    asyncio.run(db_mod.notify_dedup_forget(pool, 7, "op:a"))
    assert (7, "op:a") not in pool.taken
    assert (7, "op:b") in pool.taken, "снята отметка чужого ключа"


# ── Самопроверка пробника ────────────────────────────────────────────────────

def test_the_probe_would_notice_a_window_that_is_never_freed():
    """Без снятия отметки второй заход не доходит до отправки — это и ловим."""
    from services import op_worker

    pool = _Pool()
    # Занимаем окно руками и НЕ снимаем: имитация прежнего поведения.
    asyncio.run(pool.fetchrow(
        "INSERT INTO notification_dedup(user_id, dedup_key, last_sent)", 7, "op:done:3"))
    bot = _Bot()
    asyncio.run(op_worker._notify_owner_about_op(
        pool, bot, 7, "итог операции", dedup_key="done:3"))
    assert bot.calls == 0, (
        "занятое окно не останавливает отправку — значит тесты выше "
        "ничего не измеряют")
