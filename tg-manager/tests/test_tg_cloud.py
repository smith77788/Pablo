"""Telegram-облако: шифрование → нарезка → хранение → сборка + доступ/квота.

Живой Postgres: суть — файл сохраняется зашифрованными кусками, собирается
обратно байт-в-байт с проверкой SHA-256; доступ закрыт без оплаты; квота режет
превышение. Заглушка пула типы связывания не проверяет — BYTEA/round-trip и
ON CONFLICT в квоте видны только на настоящей БД.
"""
from __future__ import annotations

import os

import pytest

from services import tg_cloud
from services import token_vault

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")


def test_iter_chunks_and_crypto_roundtrip_pure():
    """Чистые помощники без БД: нарезка покрывает все байты, шифр обратим."""
    data = b"A" * 2500
    chunks = list(tg_cloud.iter_chunks(data, 1000))
    assert [o for o, _ in chunks] == [0, 1, 2]
    assert b"".join(c for _, c in chunks) == data
    assert [len(c) for _, c in chunks] == [1000, 1000, 500]

    blob = tg_cloud.pack_chunk(b"secret bytes")
    assert blob != b"secret bytes"                    # реально зашифровано
    assert tg_cloud.unpack_chunk(blob) == b"secret bytes"
    # порча тега/шифротекста → расшифровка бросает (целостность)
    with pytest.raises(Exception):
        tg_cloud.unpack_chunk(blob[:-1] + bytes([blob[-1] ^ 0xFF]))


@pytest.mark.skipif(not DSN, reason="нужен живой Postgres: INFRAGRAM_TEST_DSN")
def test_cloud_store_retrieve_quota_access_postgres():
    import asyncio
    import asyncpg

    ADMIN = 809001     # владелец платформы — безлимит
    PAID = 809002      # платный доступ с лимитом
    NOACCESS = 809003  # без доступа

    async def go():
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=3)

        async def _cl(oid):
            await pool.execute("DELETE FROM tg_cloud_files WHERE owner_id=$1", oid)
            await pool.execute("DELETE FROM tg_cloud_blobs WHERE owner_id=$1", oid)
            await pool.execute("DELETE FROM tg_cloud_quota WHERE owner_id=$1", oid)

        # ADMIN действительно админ (безлимит), остальные — нет.
        import bot.handlers.admin as admin_mod
        admin_mod._session_admins.add(ADMIN)
        try:
            for oid in (ADMIN, PAID, NOACCESS):
                await _cl(oid)

            # ── доступ: без оплаты нельзя ────────────────────────────────────
            assert await tg_cloud.has_access(pool, NOACCESS) is False
            with pytest.raises(tg_cloud.AccessDenied):
                await tg_cloud.store_file(pool, NOACCESS, "x.bin", b"data")

            # ── round-trip у админа: несколько кусков, сборка байт-в-байт ─────
            payload = os.urandom(5000)
            f = await tg_cloud.store_file(pool, ADMIN, "big.bin", payload, chunk_size=2000)
            assert f["status"] == "stored" and f["chunk_count"] == 3
            assert f["sha256"] == tg_cloud.sha256_hex(payload)
            # blob в БД зашифрован (не равен исходному куску)
            raw0 = await pool.fetchval(
                "SELECT data FROM tg_cloud_blobs WHERE owner_id=$1 ORDER BY locator LIMIT 1", ADMIN)
            assert bytes(raw0) != payload[:2000]
            back = await tg_cloud.retrieve_file(pool, ADMIN, f["id"])
            assert back == payload
            # список видит файл, удаление чистит
            assert any(x["id"] == f["id"] for x in await tg_cloud.list_files(pool, ADMIN))
            assert await tg_cloud.delete_file(pool, ADMIN, f["id"]) is True
            assert await pool.fetchval(
                "SELECT count(*) FROM tg_cloud_chunks WHERE file_id=$1", f["id"]) == 0

            # ── квота: платный с лимитом, превышение режется ─────────────────
            await tg_cloud.set_paid(pool, PAID, True, limit_bytes=4000)
            assert await tg_cloud.has_access(pool, PAID) is True
            f2 = await tg_cloud.store_file(pool, PAID, "ok.bin", os.urandom(3000))
            assert f2["status"] == "stored"
            q = await tg_cloud.get_quota(pool, PAID)
            assert q["used_bytes"] == 3000
            with pytest.raises(tg_cloud.QuotaExceeded):
                await tg_cloud.store_file(pool, PAID, "toobig.bin", os.urandom(2000))
            # удаление возвращает место
            await tg_cloud.delete_file(pool, PAID, f2["id"])
            assert (await tg_cloud.get_quota(pool, PAID))["used_bytes"] == 0

            for oid in (ADMIN, PAID, NOACCESS):
                await _cl(oid)
        finally:
            admin_mod._session_admins.discard(ADMIN)
            await pool.close()

    asyncio.new_event_loop().run_until_complete(go())


@pytest.mark.skipif(not DSN, reason="нужен живой Postgres: INFRAGRAM_TEST_DSN")
def test_cloud_survives_fleet_bans_and_heals_postgres():
    """Доступ к файлам переживает баны флота: пока жива хоть одна реплика куска —
    файл скачивается; реконсайлер дотягивает избыточность обратно."""
    import asyncio
    import asyncpg

    ADMIN = 809010

    async def go():
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=3)

        async def _cl():
            await pool.execute("DELETE FROM tg_cloud_files WHERE owner_id=$1", ADMIN)
            await pool.execute("DELETE FROM tg_cloud_blobs WHERE owner_id=$1", ADMIN)
            await pool.execute("DELETE FROM tg_cloud_quota WHERE owner_id=$1", ADMIN)

        import bot.handlers.admin as admin_mod
        admin_mod._session_admins.add(ADMIN)
        try:
            await _cl()
            payload = os.urandom(4500)
            # 3 куска × 3 реплики = 9 локаций.
            f = await tg_cloud.store_file(pool, ADMIN, "vault.bin", payload,
                                          chunk_size=2000, replicas=3)
            fid = f["id"]
            dur = await tg_cloud.file_durability(pool, fid)
            assert dur["min_live"] == 3 and dur["chunks"] == 3

            # ── «бан» 2 из 3 хранителей: гасим реплики 0 и 1 у КАЖДОГО куска ──
            for rep in (0, 1):
                locs = await pool.fetch(
                    "SELECT l.id, l.locator FROM tg_cloud_chunk_locs l "
                    "JOIN tg_cloud_chunks c ON c.id=l.chunk_id "
                    "WHERE c.file_id=$1 AND l.replica=$2", fid, rep)
                for lo in locs:
                    await pool.execute("DELETE FROM tg_cloud_blobs WHERE locator=$1", lo["locator"])
                    await pool.execute("UPDATE tg_cloud_chunk_locs SET status='dead' WHERE id=$1", lo["id"])
            dur = await tg_cloud.file_durability(pool, fid)
            assert dur["min_live"] == 1, dur
            # Доступ к облаку от банов НЕ зависит — только от владельца.
            assert await tg_cloud.has_access(pool, ADMIN) is True
            # Файл всё ещё собирается из уцелевшей реплики.
            assert await tg_cloud.retrieve_file(pool, ADMIN, fid) == payload

            # ── heal восстанавливает избыточность до нормы ──
            res = await tg_cloud.heal_file(pool, ADMIN, fid, replicas=3)
            assert res["restored"] == 6 and res["unhealable"] == 0, res
            assert (await tg_cloud.file_durability(pool, fid))["min_live"] == 3
            assert await tg_cloud.retrieve_file(pool, ADMIN, fid) == payload

            # ── крайний случай: убить ВСЕ реплики одного куска → сборка честно падает
            locs = await pool.fetch(
                "SELECT l.locator FROM tg_cloud_chunk_locs l "
                "JOIN tg_cloud_chunks c ON c.id=l.chunk_id WHERE c.file_id=$1 AND c.ord=0", fid)
            for lo in locs:
                await pool.execute("DELETE FROM tg_cloud_blobs WHERE locator=$1", lo["locator"])
                await pool.execute("UPDATE tg_cloud_chunk_locs SET status='dead' WHERE locator=$1", lo["locator"])
            with pytest.raises(ValueError):
                await tg_cloud.retrieve_file(pool, ADMIN, fid)
            heal2 = await tg_cloud.heal_file(pool, ADMIN, fid, replicas=3)
            assert heal2["unhealable"] == 1, heal2
            await _cl()
        finally:
            admin_mod._session_admins.discard(ADMIN)
            await pool.close()

    asyncio.new_event_loop().run_until_complete(go())
