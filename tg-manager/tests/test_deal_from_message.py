"""«В сделку» у пересланного сообщения клиента в боте и счётчик «ждут ответа».

Проверяется:
* кнопка стоит у обеих пересылок релея (пассивной и операторской);
* нажатие заводит контакт и ставит его в воронку стадией «Лид», а стадию,
  которая уже есть, не сбрасывает;
* клиент чужого бота в сделку не попадает;
* сделка пишется в contact_crm — ту же запись, что у канбана;
* у мини-аппа есть входы #deals и #inbox, а у плитки «Входящие» — счётчик.
"""
from __future__ import annotations

import asyncio
import os

import pytest

from bot.handlers import deal_from_message as dfm

from tests.miniapp_source import miniapp_html

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DSN = os.getenv("INFRAGRAM_TEST_DSN", "")


def _cb_datas(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]


def test_add_button_packs_short_callback():
    m = dfm.add_button_markup(8_123_456_789, 1_234_567_890_123)
    datas = _cb_datas(m)
    assert datas == ["dmsg:add:8123456789:1234567890123:"]
    assert all(len(d.encode()) <= 64 for d in datas)


def test_stage_markup_marks_current_and_fits():
    from services.contacts_hub.crm_engine import STAGE_LABELS
    m = dfm.stage_markup(8_123_456_789, 1_234_567_890_123, "proposal")
    datas = _cb_datas(m)
    assert len(datas) == len(STAGE_LABELS)
    assert all(len(d.encode()) <= 64 for d in datas)
    texts = [b.text for row in m.inline_keyboard for b in row]
    assert "• Предложение" in texts


class _Pool:
    def __init__(self, peer):
        self.peer = peer

    async def fetchrow(self, sql, *a):
        assert "added_by=$3" in sql, "клиента ищем только среди ботов владельца"
        return self.peer


def _patch(monkeypatch, existing):
    from services.contacts_hub import crm_engine, repository
    calls = []

    async def _ensure(pool, owner, tg, **kw):
        calls.append(("ensure", owner, tg, kw))
        return "c-1"

    async def _get(pool, owner, cid):
        return existing

    async def _upsert(pool, owner, cid, data):
        calls.append(("upsert", owner, cid, data))
        return {}
    monkeypatch.setattr(repository, "ensure_contact_by_telegram_id", _ensure)
    monkeypatch.setattr(crm_engine, "get_crm_data", _get)
    monkeypatch.setattr(crm_engine, "upsert_crm", _upsert)
    return calls


def test_new_client_goes_in_as_lead(monkeypatch):
    calls = _patch(monkeypatch, None)
    got = asyncio.run(dfm.put_in_deals(_Pool({"username": "ann", "first_name": "Аня"}), 7, 5, 99))
    assert got == "lead"
    assert calls[0] == ("ensure", 7, 99, {"username": "ann", "first_name": "Аня"})
    assert calls[1][0] == "upsert" and calls[1][3]["stage"] == "lead"


def test_existing_stage_is_not_reset(monkeypatch):
    calls = _patch(monkeypatch, {"stage": "negotiation"})
    got = asyncio.run(dfm.put_in_deals(_Pool({"username": None, "first_name": "Б"}), 7, 5, 99))
    assert got == "negotiation"
    assert not any(c[0] == "upsert" for c in calls)


def test_explicit_stage_moves(monkeypatch):
    calls = _patch(monkeypatch, {"stage": "lead"})
    got = asyncio.run(dfm.put_in_deals(_Pool({"username": None, "first_name": "Б"}), 7, 5, 99, "won"))
    assert got == "won" and calls[-1][3]["stage"] == "won"


def test_foreign_bot_client_is_refused(monkeypatch):
    calls = _patch(monkeypatch, None)
    assert asyncio.run(dfm.put_in_deals(_Pool(None), 7, 5, 99)) is None
    assert calls == []


def test_both_relay_forwards_carry_the_button():
    src = open(os.path.join(ROOT, "services", "auto_responder.py"), encoding="utf-8").read()
    assert src.count("reply_markup=add_button_markup(bot_id, chat_id)") == 2


def test_router_included_before_relay():
    main = open(os.path.join(ROOT, "main.py"), encoding="utf-8").read()
    i = main.index("dp.include_router(deal_msg_handler.router)")
    assert i < main.index("dp.include_router(relay_handler.router)")


def test_miniapp_entry_points_and_badge():
    html = miniapp_html()
    assert "k==='deals'" in html and "k==='inbox'" in html
    assert 'id="m-inbox-cnt"' in html and "loadInboxBadge()" in html
    js = open(os.path.join(ROOT, "mini_app", "screens", "crm_inbox.js"), encoding="utf-8").read()
    assert "async function loadInboxBadge" in js and "'m-inbox-cnt'" in js


def test_round_trip_on_real_schema():
    if not DSN:
        pytest.skip("нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")
    import asyncpg
    owner, other, bot_id, client = 88_300_001, 88_300_002, 88_300_501, 88_300_901

    async def _go():
        try:
            pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        except Exception as exc:
            pytest.skip(f"Postgres недоступен: {str(exc)[:120]}")
        try:
            await pool.execute("DELETE FROM contact_crm WHERE owner_id = ANY($1::bigint[])", [owner, other])
            await pool.execute("DELETE FROM unified_contacts WHERE owner_id = ANY($1::bigint[])", [owner, other])
            await pool.execute("DELETE FROM managed_bots WHERE bot_id=$1", bot_id)
            await pool.execute(
                "INSERT INTO managed_bots(token, bot_id, added_by) VALUES($1,$2,$3)",
                f"t-{bot_id}", bot_id, owner)
            from database import db
            await db.get_or_create_relay_session(pool, bot_id, client, "ann", "Аня")
            assert await dfm.put_in_deals(pool, other, bot_id, client) is None
            assert await dfm.put_in_deals(pool, owner, bot_id, client) == "lead"
            assert await dfm.put_in_deals(pool, owner, bot_id, client, "proposal") == "proposal"
            assert await dfm.put_in_deals(pool, owner, bot_id, client) == "proposal"
            st = await pool.fetchval(
                "SELECT cc.stage FROM contact_crm cc JOIN unified_contacts uc ON uc.id=cc.contact_id "
                "WHERE cc.owner_id=$1 AND uc.telegram_user_id=$2", owner, client)
            assert st == "proposal"
        finally:
            await pool.execute("DELETE FROM managed_bots WHERE bot_id=$1", bot_id)
            await pool.execute("DELETE FROM contact_crm WHERE owner_id = ANY($1::bigint[])", [owner, other])
            await pool.execute("DELETE FROM unified_contacts WHERE owner_id = ANY($1::bigint[])", [owner, other])
            await pool.close()
    asyncio.run(_go())
