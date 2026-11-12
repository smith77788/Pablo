"""Разогрев: на ответ человеку отвечает тот аккаунт, к которому он обратился.

До этой правки следующий ход доставался случайному аккаунту флота: человек
отвечал «Олегу», а на вопрос, заданный Олегу, отвечала «Маша». В живом чате так
не бывает, и это первое, что замечают участники. Заодно здесь:
* повтором считается и фраза ДРУГОГО аккаунта флота в этом же чате;
* «набор» длится по длине реплики, а не случайные 2–6 секунд;
* адресат видит в задании, что ответили ЕМУ.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from services import chat_warmup as cw


# ── Чистая часть ───────────────────────────────────────────────────────────

def _m(i, text, sender, fleet=False, reply_to=0):
    return {"id": i, "sender_id": sender, "sender_name": f"u{sender}", "text": text,
            "is_fleet": fleet, "is_self": False, "reply_to": reply_to}


def test_who_was_addressed():
    recent = [_m(1, "я из Казани", 501, fleet=True), _m(2, "а где именно?", 9, reply_to=1),
              _m(3, "всем привет", 9), _m(4, "ты про что?", 9, reply_to=3)]
    assert cw.addressed_sender(recent, 2) == 501
    assert cw.addressed_sender(recent, 3) is None          # не ответ
    assert cw.addressed_sender(recent, 4) is None          # ответ не флоту
    assert cw.addressed_sender(recent, 99) is None


def test_the_addressed_goes_first_only_if_it_can_speak():
    assert cw.prefer_addressed([3, 1, 2], 2) == [2, 3, 1]
    assert cw.prefer_addressed([3, 1], 2) == [3, 1]        # выпал из очереди
    assert cw.prefer_addressed([3, 1], None) == [3, 1]


def test_typing_follows_the_length():
    assert cw.typing_seconds("ага") < 3
    assert cw.typing_seconds("x" * 60) > cw.typing_seconds("ага")
    assert cw.typing_seconds("x" * 5000) <= 14


def test_mock_message_has_no_reply_and_real_one_has():
    m = MagicMock()
    m.id, m.sender_id, m.message = 1, 2, "x"
    assert cw._msg_to_dict(m, set())["reply_to"] == 0
    m.reply_to_msg_id = 7
    assert cw._msg_to_dict(m, set())["reply_to"] == 7


def test_the_addressee_knows_it_was_addressed():
    _, user = cw.build_dialogue_prompt("п", [], {"text": "а где?", "sender_name": "Маша",
                                                 "to_you": True}, "", "engage")
    assert "ТВОЁ сообщение" in user
    _, user2 = cw.build_dialogue_prompt("п", [], {"text": "а где?", "sender_name": "Маша"},
                                        "", "engage")
    assert "ТВОЁ сообщение" not in user2


# ── Ход целиком, на заглушках ──────────────────────────────────────────────

class _Pool:
    def __init__(self):
        self.executed = []

    async def fetchrow(self, q, *a):
        return {"id": a[0], "session_str": "s", "device_model": None, "system_version": None,
                "app_version": None, "lang_code": None, "system_lang_code": None,
                "proxy_id": None, "geo_country": None}

    async def execute(self, q, *a):
        self.executed.append((q, a))
        return "UPDATE 1"

    async def fetchval(self, q, *a):
        return None

    async def fetch(self, q, *a):
        return []


def _msg(i, text, sender, out=False, reply_to=None):
    return SimpleNamespace(id=i, sender_id=sender, message=text, out=out,
                           reply_to_msg_id=reply_to, sender=None)


@pytest.fixture
def turn(monkeypatch):
    """Один ход разогрева: флот [1 (tg 501), 2 (tg 502)], говорить выпало 2."""
    state = {"addressed": [], "skips": [], "sent": [], "prompt_target": None, "logs": []}
    from services import account_manager, content_safety, infra_memory, op_worker
    from services import persona_voice as pv

    async def _avail(*a, **k):
        return {}

    async def _none_set(*a, **k):
        return set()

    async def _true(*a, **k):
        return True

    async def _false(*a, **k):
        return False

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(cw, "fleet_availability", _avail)
    monkeypatch.setattr(cw, "_write_forbidden_ids", _none_set)
    monkeypatch.setattr(cw, "_warmup_allowed", _true)
    monkeypatch.setattr(cw, "_speaker_order", lambda ids, last, busy=None: [2, 1])
    monkeypatch.setattr(infra_memory, "is_account_quarantined", _false)
    monkeypatch.setattr(op_worker, "try_claim_account", _true)
    monkeypatch.setattr(op_worker, "release_accounts", _noop)

    async def _tg_ids(pool, ids):
        return {501, 502}
    monkeypatch.setattr(cw, "_fleet_tg_ids", _tg_ids)

    async def _acc_by_tg(pool, ids, tg):
        return {501: 1, 502: 2}.get(int(tg))
    monkeypatch.setattr(cw, "_account_by_tg", _acc_by_tg)

    async def _set_addr(pool, sid, acc):
        state["addressed"].append(acc)
    monkeypatch.setattr(cw, "_set_addressed", _set_addr)

    async def _skip(pool, session, reason, **kw):
        state["skips"].append(reason)
    monkeypatch.setattr(cw, "_skip", _skip)
    async def _log(pool, session, status, *a, **k):
        state["logs"].append((status, a[0] if a else ""))
    monkeypatch.setattr(cw, "_log", _log)
    monkeypatch.setattr(cw, "_touch", _noop)
    monkeypatch.setattr(cw, "should_react", lambda *a, **k: False)
    monkeypatch.setattr(cw, "typing_seconds", lambda t: 0)

    class _Client:
        async def get_messages(self, entity, limit=0):
            return list(reversed(state["messages"]))

        async def send_message(self, entity, text, reply_to=None):
            state["sent"].append((text, reply_to))

        async def disconnect(self):
            return None

    monkeypatch.setattr(account_manager, "_make_client", lambda s, a: _Client())
    monkeypatch.setattr(account_manager, "_connect_and_track", _noop)

    async def _join(client, ref):
        return object()
    monkeypatch.setattr(cw, "_resolve_and_join", _join)

    async def _load(pool, acc, fallback="", context=""):
        return pv.Voice(account_id=acc, prompt="голос")
    monkeypatch.setattr(pv, "load", _load)
    monkeypatch.setattr(pv, "remember", _noop)

    async def _gen(persona, recent, target, topics, mode, **kw):
        state["prompt_target"] = target
        return state.get("reply", "ну а сам как думаешь"), ""
    monkeypatch.setattr(cw, "generate_reply_detailed", _gen)
    monkeypatch.setattr(content_safety, "scan_text", lambda t: SimpleNamespace(blocked=False))

    def go(messages, addressed_acc=None, reply=None):
        state["messages"] = messages
        if reply is not None:
            state["reply"] = reply
        session = {"id": 1, "owner_id": 5, "account_ids": [1, 2], "mode": "engage",
                   "chat_ref": "@chat", "last_seen_msg": 0, "last_speaker": None,
                   "addressed_acc": addressed_acc}
        asyncio.run(cw._process_session(_Pool(), session))
        return state
    return go


def test_a_reply_to_another_account_is_handed_to_it(turn):
    st = turn([_msg(1, "я из Казани", 501), _msg(2, "а где именно в Казани?", 9, reply_to=1)])
    assert st["sent"] == [], "ответил не тот аккаунт, к которому обратились"
    assert st["addressed"] == [1]
    assert "ход отдан ему" in st["skips"][-1]


def test_the_addressee_answers_and_knows_it(turn):
    st = turn([_msg(1, "я из Казани", 502, out=True),
               _msg(2, "а где именно в Казани?", 9, reply_to=1)])
    assert st["sent"] and st["sent"][0][1] == 2
    assert st["prompt_target"]["to_you"] is True


def test_deferral_happens_once_then_someone_answers(turn):
    """Адресат выпал — человек без ответа хуже чужого голоса."""
    st = turn([_msg(1, "я из Казани", 501), _msg(2, "а где именно?", 9, reply_to=1)],
              addressed_acc=1)
    assert st["sent"], "второй раз подряд ход снова отложен — человек остался без ответа"
    assert st["addressed"] == [None], "отметка адресата не снята после ответа"


def test_a_phrase_another_fleet_account_said_here_is_a_repeat(turn):
    st = turn([_msg(1, "норм тема, го", 501), _msg(2, "кто идёт?", 9)],
              reply="Норм тема, го!")
    assert st["sent"] == []
    assert ("skipped", "Реплика повторяла уже сказанное — ход пропущен") in st["logs"]


# ── Живая база: оба новых запроса принимаются Postgres ─────────────────────

_DSN = __import__("os").getenv("INFRAGRAM_TEST_DSN", "")


@pytest.mark.skipif(not _DSN, reason="нужен живой Postgres: INFRAGRAM_TEST_DSN")
def test_addressee_queries_run_on_a_live_base():
    async def go():
        import asyncpg
        pool = await asyncpg.create_pool(_DSN, min_size=1, max_size=2)
        owner = 990851
        try:
            await pool.execute("DELETE FROM chat_warmup_sessions WHERE owner_id=$1", owner)
            await pool.execute("DELETE FROM tg_accounts WHERE owner_id=$1", owner)
            acc = await pool.fetchval(
                "INSERT INTO tg_accounts(owner_id, phone, session_str, is_active, tg_user_id) "
                "VALUES($1, '+79990000851', 'x', TRUE, 77700851) RETURNING id", owner)
            sid = await pool.fetchval(
                "INSERT INTO chat_warmup_sessions(owner_id, chat_ref, account_ids) "
                "VALUES($1, '@c', $2::bigint[]) RETURNING id", owner, [acc])
            assert await cw._account_by_tg(pool, [acc], 77700851) == acc
            assert await cw._account_by_tg(pool, [acc], 1) is None
            await cw._set_addressed(pool, sid, acc)
            assert await pool.fetchval(
                "SELECT addressed_acc FROM chat_warmup_sessions WHERE id=$1", sid) == acc
            await cw._set_addressed(pool, sid, None)
            assert await pool.fetchval(
                "SELECT addressed_acc FROM chat_warmup_sessions WHERE id=$1", sid) is None
        finally:
            await pool.execute("DELETE FROM chat_warmup_sessions WHERE owner_id=$1", owner)
            await pool.execute("DELETE FROM tg_accounts WHERE owner_id=$1", owner)
            await pool.close()
    asyncio.run(go())
