"""Еженедельный отчёт владельцу приходит в бот сам.

РАЗРЫВ. Пульс-дайджест отдавался только по запросу мини-аппа
(/api/miniapp/organism/digest): владелец, который не открыл приложение, не
узнавал ни о росте, ни о падении, ни о выручке бота-продавца. Теперь
services.weekly_report раз в неделю, утром понедельника, шлёт сводку в личку,
один раз на неделю, и выключается кнопкой под отчётом.
"""
from __future__ import annotations

import asyncio
import datetime
import os

import pytest

UTC = datetime.UTC
MONDAY_9 = datetime.datetime(2026, 11, 2, 9, 0, tzinfo=UTC)


def test_window_is_monday_morning_only():
    from services import weekly_report as wr

    assert wr.in_window(MONDAY_9)
    assert not wr.in_window(MONDAY_9.replace(hour=5))
    assert not wr.in_window(MONDAY_9.replace(hour=12))
    assert not wr.in_window(MONDAY_9 + datetime.timedelta(days=1))
    assert wr.week_key(MONDAY_9) == "weekly_report:2026-W45"


def _digest(has_prev=True):
    return {
        "headline": "🟢 Всё здорово <b>",
        "has_prev": has_prev,
        "sections": [
            {"title": "🛰 Флот", "stats": [
                {"label": "Активных аккаунтов", "value": 12,
                 "trend": {"delta": 3, "dir": "up", "good": True}},
                {"label": "Мёртвых", "value": 0},
            ]},
            {"title": "📈 Рост", "stats": [{"label": "Каналов", "value": 0}]},
        ],
        "recommendations": [{"title": f"Шаг {i}", "why": "потому"} for i in range(5)],
    }


def test_report_text_carries_numbers_trends_sales_and_three_actions():
    from services import weekly_report as wr

    text = wr.format_report(_digest(), [
        {"currency": "RUB", "orders": 7, "paid": 5, "revenue": 1_250_000, "revenue_prev": 1_000_000},
    ])
    assert "Активных аккаунтов: <b>12</b> 🟢 +3" in text
    assert "Мёртвых" not in text, "нулевые числа без изменений — шум"
    assert "Рост" not in text, "пустой раздел не печатаем"
    assert "Заказов: <b>7</b>, оплачено: <b>5</b>, выручка: <b>12 500 ₽</b> (+25%" in text
    assert "3. Шаг 2" in text and "4. Шаг 3" not in text
    assert "&lt;b&gt;" in text, "заголовок экранирован: данные, а не разметка"
    assert "первый отчёт" not in text


def test_first_report_says_trends_come_next_week():
    from services import weekly_report as wr

    assert "первый отчёт" in wr.format_report(_digest(has_prev=False), [])


class _Bot:
    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    async def send_message(self, chat_id, text, **kw):
        if self.fail:
            raise RuntimeError("network")
        self.sent.append((chat_id, text, kw))


def _patch(monkeypatch, *, dismissed=(), dedup=True):
    from database import db
    from services import weekly_report as wr
    from services.organism import digest, spine

    forgot = []

    async def _state_get(pool, owner_id, key, default=None):
        return list(dismissed) if key == "dismissed" else default

    async def _dedup(pool, uid, key, cooldown):
        return dedup

    async def _forget(pool, uid, key):
        forgot.append(key)

    async def _build(pool, owner_id, *, state_key="digest_metrics"):
        assert state_key == "digest_metrics_weekly", (
            "тренды отчёта считаются от прошлого отчёта, а не от экрана мини-аппа")
        return _digest()

    async def _sales(pool, owner_id):
        return []

    monkeypatch.setattr(spine, "state_get", _state_get)
    monkeypatch.setattr(db, "notify_dedup_ok", _dedup)
    monkeypatch.setattr(db, "notify_dedup_forget", _forget)
    monkeypatch.setattr(digest, "build", _build)
    monkeypatch.setattr(wr, "sales_week", _sales)
    return forgot


def _send(bot, now=MONDAY_9):
    from services import weekly_report as wr
    return asyncio.new_event_loop().run_until_complete(wr.maybe_send(object(), bot, 42, now=now))


def test_report_is_sent_in_window_with_an_off_button(monkeypatch):
    _patch(monkeypatch)
    bot = _Bot()
    assert _send(bot) is True
    chat, text, kw = bot.sent[0]
    assert chat == 42 and "Отчёт за неделю" in text
    btn = kw["reply_markup"].inline_keyboard[0][0]
    assert "Не присылать" in btn.text and "weekly_report" in btn.callback_data


def test_report_is_not_sent_outside_window_twice_or_when_turned_off(monkeypatch):
    _patch(monkeypatch)
    bot = _Bot()
    assert _send(bot, MONDAY_9.replace(hour=20)) is False
    _patch(monkeypatch, dedup=False)
    assert _send(bot) is False, "второй отчёт за ту же неделю"
    _patch(monkeypatch, dismissed=["weekly_report"])
    assert _send(bot) is False, "владелец выключил отчёт"
    assert not bot.sent


def test_failed_delivery_frees_the_week_for_a_retry(monkeypatch):
    forgot = _patch(monkeypatch)
    assert _send(_Bot(fail=True)) is False
    assert forgot == ["weekly_report:2026-W45"], (
        "сбой сети съел отчёт недели: следующий такт уже не попробует")


def test_runner_calls_the_weekly_report_for_every_owner():
    import inspect

    from services.organism import runner

    assert "weekly_report.maybe_send(pool, bot, oid)" in inspect.getsource(runner._tick)


DSN = os.getenv("INFRAGRAM_TEST_DSN", "")


def test_sales_query_runs_on_real_schema():
    if not DSN:
        pytest.skip("нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")
    import asyncpg
    from services import weekly_report as wr

    owner = 99_000_777

    async def go():
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        try:
            await pool.execute("DELETE FROM bot_sales_orders WHERE owner_id=$1", owner)
            for status, cents, age in (("confirmed", 50000, 1), ("new", 9900, 2),
                                       ("done", 20000, 10)):
                await pool.execute(
                    """INSERT INTO bot_sales_orders(owner_id, bot_id, customer_chat_id,
                           total_cents, currency, status, created_at)
                       VALUES($1, 1, 2, $2, 'RUB', $3, now() - make_interval(days => $4))""",
                    owner, cents, status, age)
            return await wr.sales_week(pool, owner)
        finally:
            await pool.execute("DELETE FROM bot_sales_orders WHERE owner_id=$1", owner)
            await pool.close()

    rows = asyncio.new_event_loop().run_until_complete(go())
    assert rows == [{"currency": "RUB", "orders": 2, "paid": 1,
                     "revenue": 50000, "revenue_prev": 20000}]
