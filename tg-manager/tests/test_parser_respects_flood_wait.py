"""Регрессия: разбор аудитории обязан уважать паузу Telegram (FloodWait).

Три дефекта, которые ловит этот файл.

1. Длительность паузы читалась неверно. Код делил текст ошибки по «wait » и
   брал следующее слово, а настоящий текст Telethon — «A wait of 300 seconds
   is required», то есть следующее слово «of». `int("of")` падал ВСЕГДА, и в
   запасной ветке подставлялось 30 секунд. На просьбу подождать пять минут
   разбор возвращался через полминуты — и получал следующий FloodWait длиннее
   предыдущего.

2. Сбор отправителей глотал флуд молча. `get_entity` на каждого автора стоял
   под голым `except Exception`, поэтому под действующим ограничением цикл шёл
   дальше и продолжал долбить Telegram. Так аккаунт зарабатывает спамблок
   вместо паузы.

3. Флуд не попадал в общий пульс здоровья аккаунта, и для остальных подсистем
   аккаунт оставался «спокойным»: следующая операция уходила к нему как ни в
   чём не бывало. Плюс оборванный флудом разбор выглядел как обычное
   завершение — владелец считал, что в источнике ровно столько людей, сколько
   успели собрать.
"""
from __future__ import annotations

import asyncio
import inspect

import pytest

from services import parser


class _FloodLike(Exception):
    """Ошибка с настоящим текстом Telethon и атрибутом seconds."""

    def __init__(self, seconds: int):
        self.seconds = seconds
        super().__init__(
            f"A wait of {seconds} seconds is required (caused by GetParticipantsRequest)"
        )


class _FloodTextOnly(Exception):
    """Тот же текст, но без атрибута seconds (запасной путь разбора)."""

    def __init__(self, seconds: int):
        super().__init__(f"A wait of {seconds} seconds is required")


# --- 1. длительность паузы ------------------------------------------------

def test_flood_seconds_reads_the_attribute():
    assert parser.flood_seconds(_FloodLike(300)) == 300


def test_flood_seconds_reads_the_real_telethon_text():
    """Главный тест: пять минут — это 300, а не подставленные 30."""
    got = parser.flood_seconds(_FloodTextOnly(300))
    assert got == 300, f"пауза прочитана как {got!r}, а Telegram просил 300 с"


def test_flood_seconds_does_not_fall_back_to_thirty():
    for real in (42, 120, 600, 3600):
        assert parser.flood_seconds(_FloodTextOnly(real)) == real


def test_flood_seconds_is_none_for_other_errors():
    assert parser.flood_seconds(ValueError("channel invalid")) is None
    assert parser.flood_seconds(ConnectionError("network unreachable")) is None


def test_flood_seconds_survives_broken_attribute():
    exc = _FloodTextOnly(90)
    exc.seconds = "не число"
    assert parser.flood_seconds(exc) == 90


# --- 2. сбор отправителей не долбит Telegram под ограничением -------------

class _Msg:
    def __init__(self, sender_id: int):
        from datetime import datetime, timezone

        self.sender_id = sender_id
        self.date = datetime.now(timezone.utc)


class _FloodingClient:
    """Клиент, у которого get_entity всегда отвечает длинным FloodWait."""

    def __init__(self, messages: int, seconds: int):
        self._messages = messages
        self._seconds = seconds
        self.entity_calls = 0

    def iter_messages(self, entity, limit=None):
        msgs = [_Msg(1000 + i) for i in range(self._messages)]

        async def _gen():
            for m in msgs:
                yield m

        return _gen()

    async def get_entity(self, sender_id):
        self.entity_calls += 1
        raise _FloodLike(self._seconds)


@pytest.fixture
def _no_flood_record(monkeypatch):
    """Перехватывает запись флуда, чтобы не ходить в flood_engine и в БД."""
    recorded: list[tuple] = []

    async def _rec(pool, account_id, seconds):
        recorded.append((account_id, seconds))

    monkeypatch.setattr(parser, "_record_parse_flood", _rec)
    return recorded


def test_collector_stops_on_a_long_flood(_no_flood_record):
    client = _FloodingClient(messages=50, seconds=600)

    found, saved, flood_wait = asyncio.run(
        parser._collect_and_save_senders(
            None, 7, 1, client, object(),
            source_type="group", source_id=1, source_title="t",
            source_username="t", days_back=30, limit=100,
            account_id=55,
        )
    )

    assert flood_wait == 600, "пауза Telegram не вернулась наверх"
    assert found == 0 and saved == 0
    assert client.entity_calls == 1, (
        f"под действующим ограничением сделано {client.entity_calls} запросов "
        "вместо остановки после первого"
    )


def test_collector_records_the_flood_in_account_health(_no_flood_record):
    client = _FloodingClient(messages=10, seconds=600)

    asyncio.run(
        parser._collect_and_save_senders(
            None, 7, 1, client, object(),
            source_type="group", source_id=1, source_title="t",
            source_username="t", days_back=30, limit=100,
            account_id=55,
        )
    )

    assert _no_flood_record == [(55, 600)], (
        "флуд не записан в пульс здоровья аккаунта: " f"{_no_flood_record!r}"
    )


def test_collector_waits_out_a_short_flood(monkeypatch, _no_flood_record):
    """Короткую паузу ждём на месте и продолжаем — обрывать разбор незачем."""
    slept: list[float] = []

    async def _sleep(sec):
        slept.append(sec)

    monkeypatch.setattr(parser.asyncio, "sleep", _sleep)

    client = _FloodingClient(messages=3, seconds=30)

    found, saved, flood_wait = asyncio.run(
        parser._collect_and_save_senders(
            None, 7, 1, client, object(),
            source_type="group", source_id=1, source_title="t",
            source_username="t", days_back=30, limit=100,
            account_id=55,
        )
    )

    assert flood_wait == 0, "короткий флуд не должен обрывать разбор"
    assert client.entity_calls == 3
    assert slept and all(s >= 30 for s in slept), (
        f"ждали {slept!r} — меньше, чем просил Telegram"
    )


def test_collector_returns_three_values():
    sig = inspect.signature(parser._collect_and_save_senders)
    assert "account_id" in sig.parameters, (
        "сборщику нечем записать флуд: он не знает, каким аккаунтом работает"
    )


# --- 3. оборванный разбор виден снаружи -----------------------------------

def _src() -> str:
    import os

    path = os.path.join(os.path.dirname(__file__), "..", "services", "parser.py")
    with open(path, encoding="utf-8") as f:
        return f.read()


def test_no_naive_wait_token_parsing():
    """Старый разбор `split("wait ")` возвращать нельзя: он ломался всегда."""
    src = _src()
    assert 'split("wait ")' not in src and "split('wait ')" not in src


def test_every_parse_reports_a_truncated_run():
    """Все три функции разбора отдают наверх flood_wait и признак неполноты."""
    src = _src()
    assert src.count('"flood_wait": flood_wait') == 3, (
        "не все функции разбора сообщают о прерванном флудом сборе"
    )
    assert src.count('"partial": bool(flood_wait)') == 3


def test_flood_is_written_into_the_run_error():
    src = _src()
    assert src.count("_flood_note") >= 6, (
        "обрыв по флуду не записывается в отчёт о разборе"
    )
