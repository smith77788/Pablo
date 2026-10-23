"""Предупреждения по аккаунтам доходят до владельца все, а не по одному.

`notify_if_enabled` ограничивает частоту по тройке (владелец, тип, ключ): одно
сообщение в минуту на тройку, остальное выбрасывается МОЛЧА. Ключ
необязательный, и без него все сообщения одного типа делят один слот.

Для предупреждений по аккаунтам это ломает саму суть. Они приходят пачкой:
сторож теневого бана идёт циклом по всем аккаунтам владельца с высоким риском
лимитов, `check_owner_now` зовётся подряд после каждой деактивации. Тип у них
общий — "restriction". Значит владелец узнавал ровно про ОДИН аккаунт, а про
остальные не узнавал никогда: собственный персистентный кулдаун события уже
сказал «говори», а слот в памяти сообщение съел.

То же с типом "flood_warning": сводка авто-ротации trust и предупреждение
«мало активных аккаунтов» делили один слот и вытесняли друг друга.

Проверяем оба слоя: что ограничитель различает ключи, и что места отправки в
слое Telegram этот ключ передают.
"""
from __future__ import annotations

import ast
import asyncio
import pathlib

import pytest

from database import db

ROOT = pathlib.Path(__file__).resolve().parent.parent
OWNER = 7001


class _Bot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))


class _Pool:
    async def fetchrow(self, q, *a):
        return None

    async def fetch(self, q, *a):
        return []

    async def execute(self, q, *a):
        return "OK"

    async def fetchval(self, q, *a):
        return None


@pytest.fixture(autouse=True)
def _clean_cooldown(monkeypatch):
    db._notify_cooldown.clear()
    # Настройки уведомлений берём из базы — здесь она заглушка, поэтому
    # отвечаем прямо: всё включено.
    async def _settings(pool, user_id):
        return {}
    monkeypatch.setattr(db, "get_notification_settings", _settings)
    yield
    db._notify_cooldown.clear()


def _send(bot, pool, text, key=None):
    asyncio.run(db.notify_if_enabled(
        pool, bot, OWNER, "restriction", text, dedup_key=key))


def test_a_batch_of_accounts_reaches_the_owner_in_full():
    """Сторож рисков идёт циклом по аккаунтам — дойти должны все."""
    bot, pool = _Bot(), _Pool()
    for acc_id in (11, 12, 13):
        _send(bot, pool, f"риск лимитов у аккаунта {acc_id}",
              key=f"account_flood_risk:acc:{acc_id}")

    assert len(bot.sent) == 3, (
        f"владелец узнал только про {len(bot.sent)} аккаунт(а) из трёх — "
        "остальные выброшены молча"
    )


def test_without_a_key_the_batch_collapses_to_one():
    """Так это и выглядело: три предупреждения, доставлено одно."""
    bot, pool = _Bot(), _Pool()
    for acc_id in (11, 12, 13):
        _send(bot, pool, f"риск лимитов у аккаунта {acc_id}")

    assert len(bot.sent) == 1


def test_the_same_event_is_still_not_repeated():
    """Ключ — не отмена защиты от спама: повтор того же события глушится."""
    bot, pool = _Bot(), _Pool()
    _send(bot, pool, "риск", key="account_flood_risk:acc:11")
    _send(bot, pool, "риск", key="account_flood_risk:acc:11")

    assert len(bot.sent) == 1


def test_different_kinds_of_account_trouble_do_not_displace_each_other():
    bot, pool = _Bot(), _Pool()
    _send(bot, pool, "низкий trust", key="low_trust")
    _send(bot, pool, "аккаунт деактивирован", key="account_deactivated")
    _send(bot, pool, "теневой бан у бота 5", key="search_drop:bot:5")

    assert len(bot.sent) == 3


# ── места отправки в слое Telegram ───────────────────────────────────────────

_MINE = (
    "services/account_monitor.py",
    "services/shadowban_monitor.py",
    "services/trust_engine.py",
)


def _calls_without_key(rel: str):
    tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
        if name != "notify_if_enabled":
            continue
        if not any(k.arg == "dedup_key" for k in node.keywords):
            out.append(node.lineno)
    return out


@pytest.mark.parametrize("rel", _MINE)
def test_every_account_alert_carries_its_key(rel):
    missing = _calls_without_key(rel)
    assert not missing, (
        f"{rel}: отправка без dedup_key в строках {missing} — эти сообщения "
        "делят один слот с остальными того же типа и теряются. Ключ — род "
        "события плюс идентификатор сущности, например account_flood_risk:acc:42."
    )


def test_the_detector_sees_the_calls_at_all():
    total = 0
    for rel in _MINE:
        tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
        total += sum(
            1 for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and (getattr(n.func, "attr", None) or getattr(n.func, "id", None))
            == "notify_if_enabled"
        )
    assert total >= 7, f"найдено всего {total} мест отправки — сканер сломан"
