"""Резерв перелива: мини-апп заранее говорит, какой канал исполнитель не возьмёт.

Раньше список резерва показывал все свои каналы одинаково, а исполнитель
молча пропускал занятые другой кампанией, заполненные или выбывшие в этой и
каналы, чей аккаунт-админ недоступен. Оператор отмечал пять каналов, а
работало два, — и узнавал об этом только из итога.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from services import invite_overflow as ovf

ROOT = Path(__file__).resolve().parents[1]


def _ch(cid, **kw):
    r = {"channel_id": cid, "title": f"C{cid}", "username": "", "members_count": 0,
         "is_admin": True, "is_creator": False, "is_active": True,
         "acc_status": "active", "has_session": True}
    r.update(kw)
    return r


class _Pool:
    def __init__(self, chans, chain):
        self.chans, self.chain = chans, chain

    async def fetch(self, q, *a):
        if "FROM managed_channels" in q:
            return self.chans
        if "FROM invite_overflow_channels" in q:
            return self.chain
        return []


def test_unusable_reserve_channels_carry_a_reason():
    pool = _Pool(
        [_ch(1), _ch(2), _ch(3), _ch(4), _ch(5, acc_status="banned"),
         _ch(6, is_admin=False), _ch(7)],
        [{"channel_id": 2, "chain_key": "other", "status": "active", "reason": ""},
         {"channel_id": 3, "chain_key": "@main", "status": "full", "reason": ""},
         {"channel_id": 4, "chain_key": "@main", "status": "burned", "reason": "закрыт"},
         {"channel_id": 7, "chain_key": "@main", "status": "active", "reason": ""}])
    res = {c["channel_id"]: c for c in asyncio.run(ovf.reserve_options(pool, 1, "@main"))}
    assert res[1]["usable"] and res[7]["usable"], "свободный и уже в работе этой кампании"
    assert "другой кампанией" in res[2]["reason"]
    assert "заполнен" in res[3]["reason"]
    assert "выбыл" in res[4]["reason"] and "закрыт" in res[4]["reason"]
    assert "недоступен" in res[5]["reason"]
    assert "не админ" in res[6]["reason"]
    # Правило занятости — то же, что у исполнителя.
    busy = asyncio.run(ovf.busy_channel_ids(_BusyPool(pool.chain), 1, "@main"))
    assert busy == {2, 3, 4}
    assert {c for c, v in res.items() if not v["usable"]} >= busy


class _BusyPool:
    def __init__(self, chain):
        self.chain = chain

    async def fetch(self, q, *a):
        return self.chain


def test_picker_uses_the_server_verdict():
    api = (ROOT / "services" / "mini_app_api.py").read_text(encoding="utf-8")
    assert '"/api/miniapp/invite/reserve_options"' in api
    js = (ROOT / "mini_app" / "screens" / "invite.js").read_text(encoding="utf-8")
    assert "/api/miniapp/invite/reserve_options?group=" in js
    assert "c.reason" in js and "disabled" in js


DSN = os.getenv("INFRAGRAM_TEST_DSN", "")


@pytest.mark.skipif(not DSN, reason="нужен живой Postgres: INFRAGRAM_TEST_DSN")
def test_reserve_options_sql_runs_on_real_schema():
    import asyncpg

    async def _go():
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        try:
            return await ovf.reserve_options(pool, 990778, "@main")
        finally:
            await pool.close()
    loop = asyncio.new_event_loop()
    try:
        assert loop.run_until_complete(_go()) == []
    finally:
        loop.close()


def test_chain_panel_says_where_the_next_run_continues(tmp_path):
    """Панель цепочки: каналы кампании и откуда продолжит запуск (правило исполнителя)."""
    import json
    import shutil
    import subprocess

    if not shutil.which("node"):
        pytest.skip("нужен node")
    api = (ROOT / "services" / "mini_app_api.py").read_text(encoding="utf-8")
    assert '"chain": chain' in api
    js = (ROOT / "mini_app" / "screens" / "invite.js").read_text(encoding="utf-8")
    start = js.index("function _invRenderChain(")
    end = js.index("\nfunction invReservePick(")
    fn = js[start:end]
    script = tmp_path / "t.js"
    script.write_text(
        "const el={innerHTML:''};const document={getElementById:()=>el};"
        "const esc=s=>String(s);const num=n=>String(n);const _invGroup=()=>'@main';"
        + fn +
        "\nconst out=[];"
        f"_invRenderChain({json.dumps([{'channel_ref': '@main', 'channel_id': 1, 'status': 'full', 'invited_ok': 190}, {'channel_ref': 'https://t.me/+r', 'channel_id': 2, 'status': 'active', 'invited_ok': 40}])},"
        f"{json.dumps([{'channel_id': 2, 'title': 'Резерв-1'}])});out.push(el.innerHTML);"
        f"_invRenderChain({json.dumps([{'channel_ref': '@main', 'channel_id': 1, 'status': 'full', 'invited_ok': 190}])}, []);out.push(el.innerHTML);"
        "_invRenderChain([], []);out.push(el.innerHTML);"
        "console.log(JSON.stringify(out));", encoding="utf-8")
    out = json.loads(subprocess.run(["node", str(script)], capture_output=True, text=True,
                                    check=True).stdout)
    assert "продолжит в «Резерв-1»" in out[0] and "✅ заполнен · 190" in out[0]
    assert "сразу возьмёт канал из резерва" in out[1]
    assert out[2] == ""
