"""Публикация ссылки в несколько каналов не палит целевой канал.

Жалоба владельца: после массовой публикации ссылки в десятки каналов целевой
канал (на который ведут ссылки) почти сразу удаляют. Причина — сигнатура
накрутки: одна и та же ссылка, одинаковым текстом, залпом (~30с шаг) во все
каналы. Telegram видит «многие → один target за минуты» и сносит target.

Защита (op_worker): когда в тексте есть ссылка на канал —
  1) форма ссылки варьируется на КАЖДЫЙ канал (адрес тот же, вид разный),
  2) межканальный разнос поднимается до безопасного минимума с джиттером.
Без ссылки поведение прежнее (no-op).

Поведенческий тест на _exec_bulk_post_chans (лёгкий путь: один аккаунт,
несколько каналов) ловит оба эффекта на реальном вызове. Для _exec_mass_publish
(тяжёлый харнес) — проверка проводки по исходнику.
"""
from __future__ import annotations

import asyncio
import re

import pytest

from services import op_worker, account_manager
from services import link_vary


class _Pool:
    def __init__(self, channels):
        self._channels = channels

    async def fetchrow(self, query, *a):
        if "FROM tg_accounts" in query:
            return {"id": 1, "session_str": "s1", "first_name": "Акк",
                    "phone": "+700", "device_model": "x", "system_version": "x",
                    "app_version": "x", "lang_code": "ru", "system_lang_code": "ru",
                    "proxy_url": None}
        return None

    async def fetch(self, query, *a):
        if "FROM managed_channels" in query:
            return self._channels
        return []

    async def execute(self, query, *a):
        return "UPDATE 1"

    async def fetchval(self, query, *a):
        return 0


def _targets(text: str) -> set[str]:
    t = set()
    for m in re.finditer(r"(?:https?://)?(?:t\.me|telegram\.me)/(?!joinchat/|\+)"
                         r"([A-Za-z][A-Za-z0-9_]{3,31})", text, re.I):
        t.add(m.group(1).lower())
    for m in re.finditer(r"(?<![\w@./])@([A-Za-z][A-Za-z0-9_]{3,31})\b", text):
        t.add(m.group(1).lower())
    return t


def _harness(monkeypatch, sent, slept):
    async def _post(session_str, ch_id, body, **kw):
        sent.append((int(ch_id), body))
        return {"msg_id": 1}

    async def _true(*a, **k):
        return True

    async def _false(*a, **k):
        return False

    async def _none(*a, **k):
        return None

    async def _empty_set(*a, **k):
        return set()

    async def _zero_flood(*a, **k):
        return 0.0

    async def _sleep(secs, *a, **k):
        slept.append(float(secs))

    monkeypatch.setattr(account_manager, "post_to_channel", _post)
    monkeypatch.setattr(op_worker, "try_claim_account", _true)
    monkeypatch.setattr(op_worker, "release_accounts", _none)
    monkeypatch.setattr(op_worker, "completed_targets", _empty_set)
    monkeypatch.setattr(op_worker, "_is_cancelled", _false)
    monkeypatch.setattr(op_worker, "_single_account_parked", _none)
    monkeypatch.setattr(op_worker, "_note_flood_penalty", _none)
    monkeypatch.setattr(op_worker, "bounded_flood_sleep", _zero_flood)
    monkeypatch.setattr(op_worker, "_flood_cooldown_left", lambda a: 0.0)
    monkeypatch.setattr(op_worker._infra_mem, "is_account_quarantined", _false)
    monkeypatch.setattr(asyncio, "sleep", _sleep)


_CHANNELS = [{"id": i, "channel_id": 1000 + i, "access_hash": 0, "username": ""}
             for i in range(1, 6)]


@pytest.mark.asyncio
async def test_link_form_varies_per_channel_but_target_stays(monkeypatch):
    sent: list = []
    slept: list = []
    _harness(monkeypatch, sent, slept)

    res = await op_worker._exec_bulk_post_chans(
        _Pool(_CHANNELS), object(), 10, 555,
        {"acc_id": 1, "channel_ids": [1, 2, 3, 4, 5],
         "text": "Подпишись на https://t.me/best_news_channel — там всё!"},
    )
    assert res["status"] == "done" and res["ok"] == 5

    bodies = [b for _, b in sent]
    # Все ведут на тот же канал…
    for b in bodies:
        assert _targets(b) == {"best_news_channel"}, f"адрес ссылки уехал: {b}"
    # …но форма у разных каналов различается (иначе сигнатура не снята).
    assert len(set(bodies)) >= 2, (
        "ссылка во всех каналах посимвольно одинакова — защита не работает"
    )


@pytest.mark.asyncio
async def test_pacing_is_raised_when_text_has_a_link(monkeypatch):
    sent: list = []
    slept: list = []
    _harness(monkeypatch, sent, slept)

    await op_worker._exec_bulk_post_chans(
        _Pool(_CHANNELS), object(), 11, 555,
        {"acc_id": 1, "channel_ids": [1, 2, 3, 4, 5],
         "text": "Жми https://t.me/best_news_channel"},
    )
    # Хотя бы одна межканальная пауза должна дотянуть до безопасного минимума
    # (с учётом джиттера 0.7x). Без ссылки паузы — секунды (backoff cap=30).
    assert max(slept) >= op_worker._LINK_SAFE_MIN_DELAY_S * 0.7, (
        f"разнос не поднят для ссылки: max пауза {max(slept):.0f}с"
    )


@pytest.mark.asyncio
async def test_no_link_is_a_noop(monkeypatch):
    sent: list = []
    slept: list = []
    _harness(monkeypatch, sent, slept)

    await op_worker._exec_bulk_post_chans(
        _Pool(_CHANNELS), object(), 12, 555,
        {"acc_id": 1, "channel_ids": [1, 2, 3, 4, 5],
         "text": "Обычный пост без ссылок про погоду"},
    )
    bodies = [b for _, b in sent]
    assert all(b == "Обычный пост без ссылок про погоду" for b in bodies), \
        "текст без ссылок изменён — варьирование не должно его трогать"
    # И паузы остаются короткими (быстрый темп сохраняется).
    assert max(slept) < op_worker._LINK_SAFE_MIN_DELAY_S * 0.7


def test_mass_publish_is_wired_to_link_vary():
    """_exec_mass_publish тяжело поднять харнесом — проверяем проводку."""
    import inspect
    src = inspect.getsource(op_worker._exec_mass_publish)
    assert "contains_channel_link" in src, "mass_publish не детектит ссылку"
    assert "vary_channel_links" in src, "mass_publish не варьирует форму ссылки"
    assert "_LINK_SAFE_MIN_DELAY_S" in src, "mass_publish не поднимает разнос для ссылок"
