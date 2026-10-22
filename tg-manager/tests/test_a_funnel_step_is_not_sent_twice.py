"""Шаг воронки не уходит человеку второй раз и не обрывается от блипа.

РАЗРЫВ 1 — повтор. `_process_run` отправлял сообщение шага и ТОЛЬКО ПОТОМ
писал продвижение (`next_step_num`/`next_send_at`). Между этими двумя
действиями процесс умирает на каждом деплое рабочей ветки (автодеплой: пуш в
рабочую ветку уезжает в прод): строка остаётся с тем же шагом и
`next_send_at <= NOW()`, поэтому следующий круг опроса отправляет тот же шаг
тому же ЖИВОМУ человеку заново. Теперь шаг занимается условным UPDATE'ом ДО
отправки: не прошёл claim — значит шаг уже уходил.

РАЗРЫВ 2 — обрыв воронки. Любая неудача отправки трактовалась как «User
likely blocked the bot» и глушила воронку этому человеку НАВСЕГДА: сетевой
блип или 5xx Telegram выписывали живого подписчика. Решает та же дверь, что
у рассылки — `bot_api.recipient_is_gone`.

Потерянное сообщение дешевле второго: второе человек читает как спам, а бот
получает за него блокировку.
"""
from __future__ import annotations

import asyncio

import pytest

_STEP = {"step_num": 1, "message_text": "Привет", "button_text": None,
         "button_url": None}
_NEXT = {"step_num": 2, "delay_hours": 24}


class _Pool:
    def __init__(self, claim_ok: bool = True, next_step=_NEXT):
        self.claim_ok = claim_ok
        self.next_step = next_step
        self.trace: list[str] = []
        self.updates: list[tuple[str, tuple]] = []

    async def fetchrow(self, query, *args):
        if "auto_funnel_steps" in query and "step_num > " in query:
            return dict(self.next_step) if self.next_step else None
        if "auto_funnel_steps" in query:
            return dict(_STEP)
        return None

    async def fetchval(self, query, *args):
        if "last_sent_step" in query:
            self.trace.append("claim")
            return 1 if self.claim_ok else None
        return None

    async def execute(self, query, *args):
        self.trace.append("update")
        self.updates.append((query, args))
        return "UPDATE 1"

    def wrote(self, needle):
        return [a for q, a in self.updates if needle in q]


@pytest.fixture
def af(monkeypatch):
    from services import auto_funnel, bot_api
    from database import db

    sent: list[int] = []
    outcome = {"ok": True, "cat": "ok"}

    async def _send(session, token, chat_id, text, **kw):
        sent.append(int(chat_id))
        return outcome["ok"], 0, outcome["cat"]

    async def _bot_row(pool, query, *a):
        return {"token": "123456789:AAFqwertyuiopasdfghjklzxcvbnm123456A"}

    monkeypatch.setattr(bot_api, "send_message_classified", _send)
    monkeypatch.setattr(db, "fetchrow_bot", _bot_row, raising=False)
    return auto_funnel, sent, outcome


_RUN = {"id": 9, "funnel_id": 3, "bot_id": 555, "user_id": 101,
        "next_step_num": 1}


def _process(af_mod, pool):
    asyncio.run(af_mod._process_run(pool, object(), dict(_RUN)))


def test_the_step_is_claimed_before_it_is_sent(af):
    """Иначе рестарт между отправкой и продвижением повторяет отправку."""
    af_mod, sent, _ = af
    pool = _Pool()
    _process(af_mod, pool)

    assert sent == [101]
    assert pool.trace[0] == "claim", (
        f"сначала произошло {pool.trace[0]}, а не отметка шага")
    assert pool.wrote("next_step_num=$1"), "воронка не продвинулась"


def test_an_already_sent_step_is_not_sent_again(af):
    af_mod, sent, _ = af
    pool = _Pool(claim_ok=False)
    _process(af_mod, pool)

    assert sent == [], "шаг ушёл человеку второй раз после рестарта"
    assert pool.wrote("next_step_num=$1"), (
        "воронка застряла: шаг пропущен, но продвижения нет")


def test_a_blocked_recipient_stops_the_run(af):
    af_mod, sent, outcome = af
    outcome.update(ok=False, cat="blocked")
    pool = _Pool()
    _process(af_mod, pool)

    assert pool.wrote("status='stopped'"), (
        "заблокировавший бота человек остался в воронке")


def test_a_network_blip_does_not_kill_the_funnel(af):
    af_mod, sent, outcome = af
    outcome.update(ok=False, cat="unavailable")
    pool = _Pool()
    _process(af_mod, pool)

    assert not pool.wrote("status='stopped'"), (
        "сетевой сбой выписал живого подписчика из воронки навсегда")
    assert pool.wrote("next_step_num=$1"), "воронка не пошла к следующему шагу"


def test_the_last_step_completes_the_run(af):
    af_mod, sent, _ = af
    pool = _Pool(next_step=None)
    _process(af_mod, pool)
    assert sent == [101]
    assert pool.wrote("status='completed'")
