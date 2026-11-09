"""Пульс здоровья одним проходом считает то же, что считали четыре подзапроса.

08.11.2026 четыре вложенных подзапроса на строку флота заменены одним
проходом по каждому журналу (агрегат с FILTER). Здесь на живой базе
сверяем счётчики с независимым расчётом в Python по тем же правилам:
окно экрана ($2 = 7 дн.) для restrictions/severe/floods, окно гейта
(QUARANTINE_DAYS) для severe_gate, и всё строго после risk_cleared_at.
Плюс срез по account_ids отдаёт ровно эти аккаунты с теми же вердиктами.
"""
from __future__ import annotations

import asyncio
import os
import random
from datetime import UTC, datetime, timedelta

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="нужен живой Postgres: INFRAGRAM_TEST_DSN")

OWNER = 990_000_000 + random.randint(1, 999_999)


def _severe(sev, et):
    et = et.lower()
    return sev == "critical" or "ban" in et or "block" in et or "restrict" in et


def test_one_pass_matches_reference():
    from services import infra_memory as im

    async def go():
        import asyncpg
        conn = await asyncpg.connect(DSN)
        try:
            now = datetime.now(UTC)
            rnd = random.Random(5)
            accs = []
            for i in range(40):
                cleared = now - timedelta(hours=rnd.choice([1, 30, 100])) if i % 3 == 0 else None
                aid = await conn.fetchval(
                    "INSERT INTO tg_accounts(owner_id, phone, session_str, is_active,"
                    " acc_status, trust_score, risk_cleared_at)"
                    " VALUES($1,$2,'s',TRUE,'ok',0.9,$3) RETURNING id",
                    OWNER, f"+7999{OWNER % 100000}{i:03d}", cleared)
                accs.append((aid, cleared))
            events, floods = [], []
            for aid, _ in accs:
                for _ in range(rnd.randint(0, 6)):
                    at = now - timedelta(hours=rnd.randint(1, 240))
                    et = rnd.choice(["flood_wait", "peer_flood", "ban", "restrict", "x"])
                    sev = rnd.choice(["warning", "critical"])
                    await conn.execute(
                        "INSERT INTO restriction_events(owner_id, account_id, event_type,"
                        " severity, created_at) VALUES($1,$2,$3,$4,$5)", OWNER, aid, et, sev, at)
                    events.append((aid, et, sev, at))
                for _ in range(rnd.randint(0, 5)):
                    at = now - timedelta(hours=rnd.randint(1, 240))
                    await conn.execute(
                        "INSERT INTO account_flood_log(account_id, created_at) VALUES($1,$2)", aid, at)
                    floods.append((aid, at))

            class _P:
                async def fetch(self, q, *a):
                    return await conn.fetch(q, *a)

            full = await im.get_account_health(_P(), OWNER)
            part = await im.get_account_health(_P(), OWNER, account_ids=[a for a, _ in accs[:7]])
            return accs, events, floods, full, part, now
        finally:
            await conn.execute("DELETE FROM restriction_events WHERE owner_id=$1", OWNER)
            await conn.execute("DELETE FROM tg_accounts WHERE owner_id=$1", OWNER)
            await conn.close()

    accs, events, floods, full, part, now = asyncio.run(go())
    by_id = {a["account_id"]: a for a in full["accounts"]}
    assert len(by_id) == len(accs)
    win, gate = timedelta(days=7), timedelta(days=im.QUARANTINE_DAYS)
    for aid, cleared in accs:
        def ok(at, cleared=cleared):
            return cleared is None or at > cleared
        ev = [e for e in events if e[0] == aid and ok(e[3])]
        restr = sum(1 for e in ev if e[3] > now - win)
        floods_n = sum(1 for f in floods if f[0] == aid and ok(f[1]) and f[1] > now - win)
        gate_n = sum(1 for e in ev if e[3] > now - gate and _severe(e[2], e[1]))
        a = by_id[aid]
        assert a["restrictions"] == restr, (aid, a["restrictions"], restr)
        assert a["floods"] == floods_n, (aid, a["floods"], floods_n)
        assert (a["status"] == "quarantine") == bool(gate_n), (aid, a["status"], gate_n)
    want = {a for a, _ in accs[:7]}
    assert {a["account_id"] for a in part["accounts"]} == want
    for a in part["accounts"]:
        assert a == by_id[a["account_id"]], "срез страницы дал другой вердикт, чем весь флот"
