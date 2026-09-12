"""Флот-транспорт облака: реплики кусков на РАЗНЫХ аккаунтах + сборка, переживающая
баны. Telegram-сеамы (загрузка/скачивание/удаление/канал-склад) застаблены —
telethon в песочнице не собрать; проверяем ОРКЕСТРАЦИЮ (распределение по разным
хранителям, манифест, mark_dead→heal на живой аккаунт) на настоящем Postgres.
"""
from __future__ import annotations

import os

import pytest

from services import tg_cloud
from services import tg_cloud_fleet as fleet

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")


@pytest.mark.skipif(not DSN, reason="нужен живой Postgres: INFRAGRAM_TEST_DSN")
def test_fleet_replicas_distinct_survive_ban_and_heal_postgres(monkeypatch):
    import asyncio
    import asyncpg

    OWNER = 809100

    async def go():
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=3)

        async def _cl():
            await pool.execute("DELETE FROM tg_cloud_files WHERE owner_id=$1", OWNER)
            await pool.execute("DELETE FROM tg_cloud_storekeepers WHERE owner_id=$1", OWNER)
            await pool.execute("DELETE FROM tg_accounts WHERE owner_id=$1", OWNER)

        # ── фейковое «облако Telegram»: (acc_id, channel, msg) → blob ──
        store: dict = {}
        seq = {"n": 1000}

        async def fake_ensure(pool_, acc):
            return int(acc["id"]) * 10, 0          # channel_id, access_hash

        async def fake_upload(acc, channel_id, access_hash, blob):
            seq["n"] += 1
            mid = seq["n"]
            store[(int(acc["id"]), int(channel_id), mid)] = bytes(blob)
            return mid

        async def fake_download(acc, channel_id, access_hash, message_id):
            return store.get((int(acc["id"]), int(channel_id), int(message_id)), b"")

        async def fake_delete(acc, channel_id, access_hash, message_id):
            store.pop((int(acc["id"]), int(channel_id), int(message_id)), None)

        monkeypatch.setattr(fleet, "_ensure_storage_channel", fake_ensure)
        monkeypatch.setattr(fleet, "_upload_blob", fake_upload)
        monkeypatch.setattr(fleet, "_download_blob", fake_download)
        monkeypatch.setattr(fleet, "_delete_blob", fake_delete)

        import bot.handlers.admin as admin_mod
        admin_mod._session_admins.add(OWNER)
        try:
            await _cl()
            acc_ids = []
            for i in range(4):
                acc_ids.append(int(await pool.fetchval(
                    "INSERT INTO tg_accounts(owner_id,phone,session_str,is_active,acc_status) "
                    "VALUES($1,$2,$3,TRUE,'active') RETURNING id",
                    OWNER, f"+7999{OWNER%100}{i:04d}", f"ENCSKIP{i}")))

            payload = os.urandom(4500)           # 3 куска при chunk_size=2000
            t = fleet.FleetTransport(OWNER)
            f = await tg_cloud.store_file(pool, OWNER, "fleet.bin", payload, transport=t,
                                          chunk_size=2000, replicas=2)
            fid = f["id"]

            # Реплики каждого куска — на РАЗНЫХ аккаунтах.
            rows = await pool.fetch(
                "SELECT c.ord, count(DISTINCT l.acc_id) AS accs, count(*) AS locs "
                "FROM tg_cloud_chunks c JOIN tg_cloud_chunk_locs l ON l.chunk_id=c.id "
                "WHERE c.file_id=$1 GROUP BY c.ord ORDER BY c.ord", fid)
            assert rows and all(r["locs"] == 2 and r["accs"] == 2 for r in rows), [dict(r) for r in rows]

            # Сборка из флота — байт-в-байт (свежий транспорт: берёт аккаунты из БД).
            assert await tg_cloud.retrieve_file(pool, OWNER, fid, fleet.FleetTransport(OWNER)) == payload

            # ── бан одного хранителя: половина реплик мертва, файл ещё собирается ──
            victim = acc_ids[0]
            n_dead = await tg_cloud.mark_account_dead(pool, victim)
            assert n_dead >= 1
            assert (await tg_cloud.file_durability(pool, fid))["min_live"] >= 1
            assert await tg_cloud.retrieve_file(pool, OWNER, fid, fleet.FleetTransport(OWNER)) == payload

            # ── heal: долить реплики на ЖИВОЙ аккаунт, не на забаненный ──
            res = await tg_cloud.heal_file(pool, OWNER, fid, fleet.FleetTransport(OWNER), replicas=2)
            assert res["unhealable"] == 0, res
            assert (await tg_cloud.file_durability(pool, fid))["min_live"] == 2
            # забаненный аккаунт НЕ получил новых живых локаций
            live_on_victim = await pool.fetchval(
                "SELECT count(*) FROM tg_cloud_chunk_locs l JOIN tg_cloud_chunks c ON c.id=l.chunk_id "
                "WHERE c.file_id=$1 AND l.acc_id=$2 AND l.status='stored'", fid, victim)
            assert int(live_on_victim) == 0
            assert await tg_cloud.retrieve_file(pool, OWNER, fid, fleet.FleetTransport(OWNER)) == payload

            await _cl()
        finally:
            admin_mod._session_admins.discard(OWNER)
            await pool.close()

    asyncio.new_event_loop().run_until_complete(go())
