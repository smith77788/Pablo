"""Клонер контента: флуд не усыпляет прогон и доходит до пульса аккаунта.

Два сросшихся дефекта одного места.

Первый — сон. `clone_to_channel` на FloodWait делал `sleep(e.seconds + 2)`.
Telegram отдаёт на forward/send и минуты, и часы; всё это время слот исполнителя,
арендованный аккаунт и живой коннект к Telegram заняты, а запросов не идёт —
ровно то поведение, за которое сессию и отзывают. Храповик
`test_no_unbounded_flood_sleep` сторожит только `op_worker.py`, поэтому здесь
класс не ловился.

Второй — молчание. Флуд никуда не возвращался: результат состоял из ok/fail/
errors. Исполнитель не знал, что аккаунт упёрся, не ставил его на cooldown в
`flood_engine` и шёл СЛЕДУЮЩЕЙ целью тем же аккаунтом — гарантированно во второй
флуд и ближе к бану.

Третий, соседний: словарь аккаунта для `_make_client` собирался из шести полей
отображения. Транспорт выбирается по `id`, `proxy_id` и `cf_relay_url`, и без
них клиент уходил НАПРЯМУЮ с host-IP, пока другие подсистемы того же аккаунта
шли через назначенный прокси. Одна сессия с двух адресов — AUTH_KEY_DUPLICATED.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services import content_cloner_engine as cce


def _run(coro):
    return asyncio.run(coro)


class _FloodWait(Exception):
    """Подмена telethon.errors.FloodWaitError — сравнение по имени класса."""

    def __init__(self, seconds: int):
        super().__init__(f"FloodWait {seconds}")
        self.seconds = seconds


def _client(*, forward_side_effect=None):
    client = MagicMock()
    client.connect = AsyncMock()
    client.is_user_authorized = AsyncMock(return_value=True)
    client.get_entity = AsyncMock(
        side_effect=[MagicMock(id=1), MagicMock(id=2)]
    )
    client.forward_messages = AsyncMock(side_effect=forward_side_effect)
    client.disconnect = AsyncMock()
    return client


@pytest.fixture
def no_sleep(monkeypatch):
    """Считаем сны вместо того, чтобы их спать."""
    slept: list[float] = []

    async def _fake(sec):
        slept.append(sec)

    monkeypatch.setattr(cce.asyncio, "sleep", _fake)
    return slept


def test_long_flood_stops_and_is_reported_upward(no_sleep, monkeypatch):
    """Длинный FloodWait: не спим, останавливаемся, отдаём настоящую паузу."""
    monkeypatch.setattr(cce, "FloodWaitError", _FloodWait)
    long_wait = cce._MAX_FLOOD_INLINE + 3000
    client = _client(forward_side_effect=_FloodWait(long_wait))

    with patch.object(cce, "_make_client", return_value=client):
        res = _run(cce.clone_to_channel(
            "sess", {"id": 5, "proxy_url": "", "proxy_id": None, "cf_relay_url": ""},
            source_ref="@src", target_ref="@dst",
            msg_ids=[1, 2, 3], mode="forward",
        ))

    assert res["flood_wait"] == long_wait, (
        "без настоящей паузы наверху исполнитель не поставит cooldown"
    )
    assert not [s for s in no_sleep if s >= cce._MAX_FLOOD_INLINE], (
        f"уснули на {no_sleep} — слот и аккаунт простаивают всю паузу"
    )
    assert res["ok"] == 0 and res["fail"] == 3


def test_short_flood_is_still_waited_out_and_retried(no_sleep, monkeypatch):
    """Короткую паузу переждать на месте дешевле, чем перепланировать."""
    monkeypatch.setattr(cce, "FloodWaitError", _FloodWait)
    short = max(1, cce._MAX_FLOOD_INLINE - 10)
    client = _client(forward_side_effect=[_FloodWait(short), None])

    with patch.object(cce, "_make_client", return_value=client):
        res = _run(cce.clone_to_channel(
            "sess", {"id": 5, "proxy_url": ""},
            source_ref="@src", target_ref="@dst",
            msg_ids=[1], mode="forward",
        ))

    assert res["flood_wait"] == 0
    assert res["ok"] == 1
    assert short + cce._FLOOD_BASE in no_sleep


def test_peer_flood_stops_the_clone(no_sleep, monkeypatch):
    """PeerFlood — спам-блок аккаунта: продолжать нечем и незачем."""
    class _PeerFlood(Exception):
        pass

    monkeypatch.setattr(cce, "PeerFloodError", _PeerFlood)
    client = _client(forward_side_effect=_PeerFlood())

    with patch.object(cce, "_make_client", return_value=client):
        res = _run(cce.clone_to_channel(
            "sess", {"id": 5, "proxy_url": ""},
            source_ref="@src", target_ref="@dst",
            msg_ids=[1, 2], mode="forward",
        ))

    assert res["peer_flood"] is True
    assert res["ok"] == 0


def test_device_keeps_transport_fields():
    """Поля, по которым выбирается выход в Telegram, обязаны дойти до клиента."""
    acc = {
        "id": 77,
        "proxy_id": 12,
        "cf_relay_url": "https://relay.example/tg",
        "proxy_url": "socks5://user:pass@host:1080",
        "owner_id": 9,
        "ipv6_subnet": "2001:db8::/64",
    }
    device = cce._build_device(acc)

    for key in ("id", "proxy_id", "cf_relay_url", "owner_id", "ipv6_subnet"):
        assert device[key] == acc[key], (
            f"{key} потерян — клиент уйдёт не тем выходом, чем остальные "
            "подсистемы аккаунта (риск AUTH_KEY_DUPLICATED)"
        )
    # Поля отображения по-прежнему получают значения по умолчанию.
    assert device["device_model"] == "Infragram"
    assert device["lang_code"] == "en"


def test_device_defaults_do_not_overwrite_real_values():
    device = cce._build_device({"device_model": "Pixel 8", "lang_code": "ru"})
    assert device["device_model"] == "Pixel 8"
    assert device["lang_code"] == "ru"
    assert device["proxy_url"] == ""


def test_executor_records_flood_and_defers():
    """Исполнитель обязан читать новый контракт, иначе фикс мёртвый."""
    import inspect
    import re

    from services import op_worker

    src = inspect.getsource(op_worker._exec_content_clone)
    assert "record_flood" in src, (
        "флуд из клонера не доходит до flood_engine — аккаунт не уходит в cooldown"
    )
    assert re.search(r'res\.get\("flood_wait"\)', src)
    assert re.search(r'res\.get\("peer_flood"\)', src)
    assert '"defer_s"' in src, "длинную паузу надо откладывать, а не пересиживать"
