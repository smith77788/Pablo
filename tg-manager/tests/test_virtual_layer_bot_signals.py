"""Обновления бота кормят виртуальный слой, а не только сенсор намерений.

ЧТО БЫЛО. Слой состояний умел всё — переход, распад, каскад, виртуальные
события, — но сигнал в него подавал ровно ОДИН источник: сенсор намерений,
который читает переписку аккаунтов. Всё, что человек делает с ботом (тапнул по
кнопке, написал /start, ответил, попросил «стоп»), до слоя не доходило вовсе,
хотя это самые дешёвые и самые честные сигналы из всех: их шлёт сам Telegram.

ЧТО ТЕПЕРЬ. auto_responder подаёт четыре сигнала. Ключ — контакт владельца
(unified_contacts.id), тот же, что пишет сенсор намерений: два пространства
идентификаторов дали бы одному человеку два независимых состояния, и оба были
бы неполными.
"""
from __future__ import annotations

import inspect
import re

import pytest

from services import virtual_layer as vl


class _Pool:
    def __init__(self, contact_id=None):
        self.contact_id = contact_id
        self.lookups: list[tuple] = []

    async def fetchval(self, sql, *args):
        self.lookups.append(args)
        return self.contact_id


async def test_signal_resolves_telegram_id_to_contact(monkeypatch):
    seen = {}

    async def _fake_signal(pool, owner_id, entity_type, entity_id, name, **kw):
        seen.update(owner_id=owner_id, entity_type=entity_type,
                    entity_id=entity_id, name=name, kw=kw)
        return {"value": "curious"}

    monkeypatch.setattr(vl, "signal", _fake_signal)
    pool = _Pool(contact_id="c-1")
    out = await vl.signal_for_telegram_user(pool, 7, 555, "opened",
                                            confidence=0.5, source="bot_1")
    assert out == {"value": "curious"}
    assert pool.lookups == [(7, 555)], "поиск контакта не привязан к владельцу"
    assert seen["entity_type"] == vl.USER and seen["entity_id"] == "c-1"
    assert seen["name"] == "opened" and seen["kw"]["confidence"] == 0.5


async def test_unknown_person_is_skipped_silently(monkeypatch):
    """Слой — надстройка над контактом; заводить контакт здесь не его дело."""
    called = False

    async def _fake_signal(*a, **k):
        nonlocal called
        called = True

    monkeypatch.setattr(vl, "signal", _fake_signal)
    assert await vl.signal_for_telegram_user(_Pool(None), 7, 555, "opened") is None
    assert not called


async def test_layer_failure_never_breaks_the_caller(monkeypatch):
    class _Broken:
        async def fetchval(self, *a):
            raise RuntimeError("база лежит")

    assert await vl.signal_for_telegram_user(_Broken(), 7, 555, "opened") is None


async def test_missing_owner_or_user_is_not_a_lookup():
    pool = _Pool("c-1")
    assert await vl.signal_for_telegram_user(pool, 0, 555, "opened") is None
    assert await vl.signal_for_telegram_user(pool, 7, None, "opened") is None
    assert pool.lookups == [], "запрос в базу на заведомо пустых данных"


# ── Источники сигналов в auto_responder ──────────────────────────────────────

def _auto_responder_source() -> str:
    from services import auto_responder
    return inspect.getsource(auto_responder)


@pytest.mark.parametrize("signal_name", [
    "clicked_offer",     # тап по кнопке
    "unsubscribed",      # «стоп»
    "opened",            # /start
    "replied",           # обычное сообщение
])
def test_auto_responder_feeds_every_signal(signal_name):
    src = _auto_responder_source()
    assert f'"{signal_name}"' in src, (
        f"источник сигнала «{signal_name}» пропал из auto_responder: "
        "виртуальный слой снова кормит один сенсор намерений")


def test_signals_are_known_to_the_ladder():
    """Сигнал, которого нет в таблице переходов, не двигает ничего."""
    for name in ("clicked_offer", "opened", "replied"):
        assert name in vl.SIGNAL_TARGET, name
    assert "unsubscribed" in vl.NEGATIVE_SIGNALS


def test_button_tap_is_stronger_than_a_plain_message():
    """Тап по кнопке — осознанное действие, ответ — ещё не квалификация."""
    assert vl.rank(vl.SIGNAL_TARGET["clicked_offer"]) > \
           vl.rank(vl.SIGNAL_TARGET["replied"]) > \
           vl.rank(vl.SIGNAL_TARGET["opened"])


def test_signal_failure_is_isolated_in_auto_responder():
    src = _auto_responder_source()
    i = src.index("async def _vl_signal(")
    seg = src[i:i + 900]
    assert "try:" in seg and "except Exception" in seg, (
        "сбой виртуального слоя снова может уронить обработку сообщения")
