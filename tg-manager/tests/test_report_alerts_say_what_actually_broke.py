"""Тревоги говорят о том, что сломалось на самом деле.

Жалоба по скриншоту бота. Тревога «Защита не записалась» пришла с одной
строкой «отчёт об операции не доехал до владельца», а под ней — общая
приписка «запись не легла в базу, скорее всего Postgres». База была ни при
чём: это сбой доставки через Telegram, операция выполнена и записана. А в
еженедельном отчёте «Хранилище не пишет… переподключите» висело у того, кто
бизнес-бота не подключал ни разу, и «3 аккаунтов» вместо «3 аккаунта».
"""
from __future__ import annotations

import asyncio

from database import db
from services import op_worker
from services.organism.brain import build_suggestions


def _snap(**kw):
    base = {"fleet": {}, "ops": {}, "graph": {}, "vault": {}}
    base.update(kw)
    return base


def test_undelivered_report_alone_does_not_blame_the_database():
    text = op_worker._protective_alert_text(
        [("infragram_notifications_undelivered_total", "отчёт не доехал", 3)])
    assert "Сообщение не доставлено" in text
    assert "Postgres" not in text and "Защита не записалась" not in text
    assert "повторять её не нужно" in text


def test_a_real_write_failure_still_warns_about_doing_it_twice():
    text = op_worker._protective_alert_text([
        ("infragram_journal_write_failures_total", "строка журнала", 1),
        ("infragram_notifications_undelivered_total", "отчёт не доехал", 2),
    ])
    assert "Защита не записалась" in text and "Postgres" in text
    assert "строка журнала — 1 раз(а)" in text and "отчёт не доехал — 2 раз(а)" in text


def test_never_connected_vault_is_not_a_breakdown():
    s = [x for x in build_suggestions(_snap(vault={"health": "never"}))
         if x["id"] == "vault_off"]
    assert s and s[0]["title"] == "Хранилище не подключено"
    assert s[0]["severity"] == "info", "не подключённое — не повод будить пушем"
    assert "Переподключите" not in s[0]["why"]


def test_disabled_vault_is_still_urgent():
    s = build_suggestions(_snap(vault={"health": "disabled"}))
    assert s[0]["id"] == "vault_off" and s[0]["severity"] == "urgent"
    assert s[0]["title"] == "Хранилище не пишет"


def test_restricted_accounts_are_counted_in_proper_russian():
    for n, word in ((1, "аккаунт "), (3, "аккаунта "), (5, "аккаунтов "), (12, "аккаунтов ")):
        s = [x for x in build_suggestions(_snap(fleet={"restricted": n}))
             if x["id"] == "accounts_restricted"]
        assert s[0]["title"] == f"{n} {word}под спам-блоком", s[0]["title"]


class _Pool:
    async def fetchrow(self, query, *args):
        return None

    async def fetch(self, query, *args):
        return []

    async def execute(self, query, *args):
        return "OK"


class _BlockedBot:
    async def send_message(self, *a, **kw):
        raise RuntimeError("Forbidden: bot was blocked by the user")


def test_a_blocked_bot_is_not_counted_as_a_delivery_failure():
    from services import metrics

    metrics.reset()
    db._notify_cooldown.clear()
    asyncio.run(db.notify_if_enabled(_Pool(), _BlockedBot(), 777001, "op_complete", "x", dedup_key="gone:1"))
    counters = metrics.snapshot().get("counters") or {}
    assert not any("notifications_undelivered" in k for k in counters), counters
    assert any("notifications_recipient_gone" in k for k in counters), counters
    assert "infragram_notifications_recipient_gone_total" in metrics._HELP, (
        "у нового счётчика нет описания — он не попадёт в выдачу")
