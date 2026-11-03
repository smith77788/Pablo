"""«Настройки»: свои данные, валюта и часовой пояс — там, где их ищут.

Живой обход мини-аппа (03.11.2026):

* выгрузка и удаление своих данных были только командами бота (/my_data,
  /delete_my_data) — в «Настройках» мини-аппа их не было;
* валюта отчётов пряталась в «Когортах и LTV»;
* часовой пояс «Тихих часов» молча брался у устройства — открыл мини-апп на
  компьютере в другом поясе, и тишина сдвинулась;
* «🔐 Секрет вебхука → Изменить» у владельца был мёртвой кнопкой: тост
  «настраивается в Railway».

Удаление необратимо: сервер требует то же слово, что и бот.
"""
from __future__ import annotations

import asyncio
import io
import zipfile

from tests.miniapp_source import js_function, miniapp_source


def _call(monkeypatch, method, path, body=None):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from services import mini_app_api, owner_data

    monkeypatch.setattr(mini_app_api, "_get_uid", lambda request: 777)
    deleted: list[int] = []

    async def _export(pool, uid):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("README.txt", "ok")
        return buf.getvalue(), {"README": 1}

    async def _delete(pool, uid):
        deleted.append(uid)
        return {"deleted": {"tg_accounts": 3, "managed_bots": 2}}

    monkeypatch.setattr(owner_data, "export_owner_data", _export)
    monkeypatch.setattr(owner_data, "delete_owner_data", _delete)

    class _Pool:
        async def fetch(self, *a, **k): return []
        async def fetchrow(self, *a, **k): return None
        async def fetchval(self, *a, **k): return 0
        async def execute(self, *a, **k): return "OK"

    async def _go():
        app = web.Application()
        mini_app_api.setup_routes(app, _Pool())
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            r = await (client.post(path, json=body) if method == "POST" else client.get(path))
            return r.status, await r.read(), r.headers.get("Content-Type", "")
        finally:
            await client.close()

    status, raw, ctype = asyncio.run(_go())
    return status, raw, ctype, deleted


def test_delete_without_the_word_deletes_nothing(monkeypatch):
    status, _, _, deleted = _call(monkeypatch, "POST", "/api/miniapp/my_data/delete", {"confirm": "да"})
    assert status == 400 and deleted == []


def test_delete_with_the_word_deletes(monkeypatch):
    status, raw, _, deleted = _call(monkeypatch, "POST", "/api/miniapp/my_data/delete",
                                    {"confirm": "ПОДТВЕРЖДАЮ"})
    assert status == 200 and deleted == [777] and b'"deleted_rows": 5' in raw.replace(b'":5', b'": 5')


def test_export_is_a_zip(monkeypatch):
    status, raw, ctype, _ = _call(monkeypatch, "GET", "/api/miniapp/my_data/export")
    assert status == 200 and "zip" in ctype
    assert zipfile.ZipFile(io.BytesIO(raw)).namelist() == ["README.txt"]


def test_settings_screen_has_data_region_and_no_dead_button():
    src = miniapp_source()
    for marker in ('id="set-currency"', 'id="set-tz"', 'onclick="exportMyData()"',
                   'onclick="deleteMyData()"'):
        assert marker in src, marker
    assert "openWebhookSecretModal" not in src, "мёртвая кнопка секрета вебхука вернулась"
    save = js_function("saveSettings")
    assert "getElementById('set-tz')" in save, "пояс снова молча берётся у устройства"
    assert "money_settings" in save
    assert "ПОДТВЕРЖДАЮ" in js_function("deleteMyData")
