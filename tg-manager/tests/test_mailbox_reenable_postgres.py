"""Почта: выключенный после ошибок ящик включается кнопкой.

После трёх сорванных отправок подряд strike_engine выключает ящик, и
вернуть его можно было только повторным вводом пароля или удалением.
Теперь «Включить» заново проверяет вход сохранённым паролем и включает
ящик только при удачной проверке. Запрос новый — проверка на живой базе.
"""
from __future__ import annotations

import asyncio
import os
import smtplib
from pathlib import Path

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER, STRANGER = 990871, 990872
JS = (Path(__file__).resolve().parent.parent / "mini_app" / "screens" / "mailboxes.js").read_text(encoding="utf-8")


def test_screen_offers_enable_for_disabled_boxes():
    assert "if (!mailbox.is_active) row.append(mailboxEnableButton(mailbox));" in JS
    assert "'/enable', {method: 'POST'}" in JS
    assert "ошибок отправки подряд" in JS


@pytest.mark.skipif(not DSN, reason="нужна INFRAGRAM_TEST_DSN")
def test_enable_rechecks_login_and_is_owner_scoped():
    async def go():
        import asyncpg
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer
        from services import mini_app_api as M
        from services import mini_app_mailboxes as MB
        from services.token_vault import encrypt_token

        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        orig_uid, orig_check = M._get_uid, MB.check_credentials
        M._get_uid = lambda r: OWNER
        seen = []
        verdict = {"ok": False}

        def fake_check(host, port, email, password):
            seen.append((host, port, email, password))
            if not verdict["ok"]:
                raise smtplib.SMTPAuthenticationError(535, b"bad")

        MB.check_credentials = fake_check
        try:
            await pool.execute("DELETE FROM strike_email_accounts WHERE owner_id = ANY($1::bigint[])",
                               [OWNER, STRANGER])
            ins = ("INSERT INTO strike_email_accounts(owner_id, email, smtp_host, smtp_port, smtp_pass, "
                   "is_active, fail_count) VALUES($1,$2,'smtp.x.ru',465,$3,FALSE,3) RETURNING id")
            mine = await pool.fetchval(ins, OWNER, "me@x.ru", encrypt_token("app-pass"))
            alien = await pool.fetchval(ins, STRANGER, "them@x.ru", encrypt_token("p"))
            app = web.Application()
            MB.setup_routes(app, pool)
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                # Пароль больше не подходит — ящик остаётся выключенным.
                r = await client.post(f"/api/miniapp/mailboxes/{mine}/enable")
                assert r.status == 400
                assert seen == [("smtp.x.ru", 465, "me@x.ru", "app-pass")]
                row = await pool.fetchrow("SELECT is_active, fail_count FROM strike_email_accounts WHERE id=$1", mine)
                assert (row["is_active"], row["fail_count"]) == (False, 3)
                # Вход прошёл — ящик включён, счётчик ошибок сброшен.
                verdict["ok"] = True
                r = await client.post(f"/api/miniapp/mailboxes/{mine}/enable")
                assert r.status == 200, await r.text()
                row = await pool.fetchrow("SELECT is_active, fail_count FROM strike_email_accounts WHERE id=$1", mine)
                assert (row["is_active"], row["fail_count"]) == (True, 0)
                # Чужой ящик — 404, без проверки и без изменений.
                n = len(seen)
                r = await client.post(f"/api/miniapp/mailboxes/{alien}/enable")
                assert r.status == 404 and len(seen) == n
                assert await pool.fetchval("SELECT is_active FROM strike_email_accounts WHERE id=$1", alien) is False
            finally:
                await client.close()
        finally:
            M._get_uid, MB.check_credentials = orig_uid, orig_check
            await pool.execute("DELETE FROM strike_email_accounts WHERE owner_id = ANY($1::bigint[])",
                               [OWNER, STRANGER])
            await pool.close()

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(go())
    finally:
        loop.close()
