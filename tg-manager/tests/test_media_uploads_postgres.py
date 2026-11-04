"""Медиа с устройства: тип по содержимому, квота, срок жизни (живой Postgres)."""
import asyncio
import os

import pytest

from services import media_uploads as mu

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OWNER = 661001
requires_pg = pytest.mark.skipif(not DSN, reason="нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 64
MP4 = b"\0\0\0\x18ftypisom" + b"\0" * 64

_LOOP = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
    return _LOOP.run_until_complete(coro)


def test_type_comes_from_the_bytes_not_the_name():
    assert mu.sniff_mime(PNG[:16]) == "image/png"
    assert mu.sniff_mime(b"\xff\xd8\xff\xe0" + b"\0" * 12) == "image/jpeg"
    assert mu.sniff_mime(b"RIFF\0\0\0\0WEBPVP8 ") == "image/webp"
    assert mu.sniff_mime(MP4[:16]) == "video/mp4"
    assert mu.sniff_mime(b"\0\0\0\x14ftypqt  ") == "video/quicktime"
    for junk in (b"<html><script>", b"%PDF-1.7", b"MZ\x90\0", b"<svg xmlns="):
        assert mu.sniff_mime(junk) is None
    assert not mu.valid_token("../../etc/passwd")
    assert mu.public_url("https://x.io/", "t") == "https://x.io/media/u/t"


@pytest.fixture(scope="module")
def pool():
    import asyncpg

    async def _mk():
        setup = await asyncpg.connect(DSN)
        try:
            await setup.execute("CREATE SCHEMA IF NOT EXISTS mediatest")
        finally:
            await setup.close()
        p = await asyncpg.create_pool(DSN, min_size=1, max_size=4,
                                      server_settings={"search_path": "mediatest"})
        sql = open(os.path.join(ROOT, "schema_v296_media_uploads.sql"), encoding="utf-8").read()
        for chunk in sql.split(";"):
            body = "\n".join(ln for ln in chunk.split("\n") if not ln.strip().startswith("--")).strip()
            if body:
                await p.execute(body)
        return p

    p = _run(_mk())
    yield p
    _run(p.execute("DELETE FROM media_uploads"))
    _run(p.close())


@requires_pg
def test_an_upload_gets_a_link_and_is_served_back(pool):
    _run(pool.execute("DELETE FROM media_uploads"))
    res = _run(mu.store(pool, OWNER, PNG))
    assert mu.valid_token(res["token"]) and res["mime"] == "image/png"
    assert _run(mu.load_public(pool, res["token"])) == ("image/png", PNG)
    assert _run(mu.load_public(pool, "x" * mu.TOKEN_LEN)) is None


@requires_pg
def test_not_media_and_empty_files_are_refused(pool):
    for blob in (b"", b"<html><script>alert(1)</script>"):
        with pytest.raises(mu.UploadRejected):
            _run(mu.store(pool, OWNER, blob))


@requires_pg
def test_the_quota_holds_and_old_files_expire(pool, monkeypatch):
    _run(pool.execute("DELETE FROM media_uploads"))
    monkeypatch.setattr(mu, "OWNER_MAX_FILES", 2)
    first = _run(mu.store(pool, OWNER, MP4))
    _run(mu.store(pool, OWNER, PNG))
    with pytest.raises(mu.UploadRejected):
        _run(mu.store(pool, OWNER, PNG))
    _run(mu.store(pool, OWNER + 1, PNG))  # квота — своя у каждого
    _run(pool.execute(
        "UPDATE media_uploads SET created_at = now() - interval '8 days' WHERE token=$1",
        first["token"]))
    assert _run(mu.load_public(pool, first["token"])) is None
    _run(mu.store(pool, OWNER, PNG))  # просроченный освободил место
    assert not _run(pool.fetchval(
        "SELECT count(*) FROM media_uploads WHERE token=$1", first["token"]))
