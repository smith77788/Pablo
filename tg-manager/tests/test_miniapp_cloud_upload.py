"""Облако: файл крупнее ~750 КБ вообще не загружался.

Мини-апп отправлял файл как base64 внутри JSON. aiohttp такое тело буферизует
целиком и рубит его на `client_max_size` — 1 МБ по умолчанию, а приложение
(`payment_webhook.make_app`, там же регистрируются роуты мини-аппа) создаётся
без этого параметра. base64 раздувает данные на треть, то есть настоящий потолок
был ~750 КБ — при обещанной пользователю квоте в 100 ГБ.

Хуже отказа был его вид: `client_max_size` срабатывает внутри `request.json()`,
исключение ловил общий `except Exception` хендлера и возвращал 500, а мини-апп
показывает на 500 «Внутренняя ошибка сервиса». Со стороны это читалось как
поломка облака, а не как превышение размера.

Здесь поднимается НАСТОЯЩИЙ aiohttp-сервер с такими же настройками, что в
проде, и проверяется: multipart-путь принимает файл в мегабайты, а JSON-путь
отказывает честно, по-русски и с кодом 413.

Второй разобранный здесь дефект — потолок хранилища. `_effective_limit`
возвращал 0 («безлимит») всем, у кого нет строки в tg_cloud_quota. Доступ к
облаку даёт ещё и тариф pro, и такой пользователь получал НЕОГРАНИЧЕННОЕ
хранилище: `store_file` проверяет квоту под `if limit > 0`, то есть при нуле не
проверяет вовсе, а мини-апп писал ему «безлимит».
"""
from __future__ import annotations

import asyncio
import base64
import json
import urllib.parse
from pathlib import Path

import pytest
from aiohttp import ClientSession, FormData, web

from services import mini_app_api as M
from services import tg_cloud

INDEX = Path(__file__).resolve().parents[1] / "mini_app" / "index.html"

UID = 809101
PORT = 18931


class _Pool:
    async def fetch(self, q, *a): return []
    async def fetchrow(self, q, *a): return None
    async def fetchval(self, q, *a): return None
    async def execute(self, q, *a): return "OK"


def test_default_app_body_limit_is_one_megabyte():
    """Страховка измерителя: без этого предела вся история теста беспредметна."""
    assert web.Application()._client_max_size == 1024 * 1024


def _serve(monkeypatch, received: list):
    """Настоящий сервер с роутами мини-аппа. Возвращает корутину запуска."""
    monkeypatch.setattr(M, "_get_uid", lambda request: UID)

    async def _fake_store(pool, owner_id, name, data, *args, **kw):
        received.append({"owner": owner_id, "name": name, "size": len(data),
                         "data": data})
        return {"id": 1, "name": name, "size_bytes": len(data), "status": "stored"}

    monkeypatch.setattr(tg_cloud, "store_file", _fake_store)

    app = web.Application()          # ровно как payment_webhook.make_app
    M.setup_routes(app, _Pool())
    return app


async def _with_server(app, fn):
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", PORT)
    await site.start()
    try:
        return await fn(f"http://127.0.0.1:{PORT}")
    finally:
        await runner.cleanup()


def test_multipart_upload_accepts_megabytes(monkeypatch):
    """Три мегабайта — втрое больше предела тела — должны дойти байт-в-байт."""
    received: list = []
    app = _serve(monkeypatch, received)
    payload = bytes(range(256)) * (3 * 1024 * 1024 // 256)

    async def go(base):
        fd = FormData()
        fd.add_field("name", "отчёт квартал.xlsx")
        fd.add_field("file", payload, filename="отчёт квартал.xlsx")
        async with ClientSession() as s:
            async with s.post(base + "/api/miniapp/cloud/upload", data=fd) as r:
                return r.status, await r.text()

    status, body = asyncio.run(_with_server(app, go))
    assert status == 200, f"multipart-загрузка отклонена: {body[:300]}"
    assert received and received[0]["size"] == len(payload), "файл дошёл не целиком"
    assert received[0]["data"] == payload, "байты изменились по дороге"
    assert received[0]["name"] == "отчёт квартал.xlsx"


def test_json_upload_over_the_limit_refuses_in_russian(monkeypatch):
    """Старый путь остаётся, но отказывает понятно: 413 и русский текст."""
    received: list = []
    app = _serve(monkeypatch, received)
    b64 = base64.b64encode(b"x" * (2 * 1024 * 1024)).decode()

    async def go(base):
        async with ClientSession() as s:
            async with s.post(base + "/api/miniapp/cloud/upload",
                              data=json.dumps({"name": "f.bin", "data_b64": b64}),
                              headers={"Content-Type": "application/json"}) as r:
                return r.status, await r.text()

    status, body = asyncio.run(_with_server(app, go))
    assert status == 413, f"отказ по размеру пришёл как {status}: {body[:200]}"
    assert "Maximum request body size" not in body, "англоязычный отказ от aiohttp"
    assert "предел" in body, f"нет русского объяснения: {body[:200]}"


def test_empty_upload_is_refused(monkeypatch):
    received: list = []
    app = _serve(monkeypatch, received)

    async def go(base):
        fd = FormData()
        fd.add_field("file", b"", filename="empty.bin")
        async with ClientSession() as s:
            async with s.post(base + "/api/miniapp/cloud/upload", data=fd) as r:
                return r.status, await r.text()

    status, _ = asyncio.run(_with_server(app, go))
    assert status == 400
    assert not received, "пустой файл не должен доходить до хранилища"


# ── Потолок одной загрузки известен и клиенту, и хранилищу ───────────────────

def test_store_file_refuses_above_the_ceiling(monkeypatch):
    """Слой хранилища держит потолок сам — путь бота тоже им закрыт."""
    async def _yes(pool, oid): return True
    monkeypatch.setattr(tg_cloud, "has_access", _yes)
    big = b"x" * (tg_cloud.MAX_FILE_BYTES + 1)

    async def go():
        with pytest.raises(tg_cloud.QuotaExceeded):
            await tg_cloud.store_file(_Pool(), UID, "huge.bin", big)

    asyncio.run(go())


def test_status_tells_the_client_the_ceiling():
    """Мини-апп обязан отказать ДО чтения файла — значит, должен знать предел."""
    src = M.__file__ and Path(M.__file__).read_text("utf-8")
    assert "'max_upload_bytes': tg_cloud.MAX_FILE_BYTES" in src, (
        "cloud_status не отдаёт потолок одной загрузки")


# ── Квота: безлимит только у владельца платформы ─────────────────────────────

class _NoQuotaRow(_Pool):
    """Пользователь получил доступ по тарифу — строки в tg_cloud_quota нет."""
    async def fetchrow(self, q, *a): return None


def test_plan_user_without_quota_row_is_not_unlimited(monkeypatch):
    monkeypatch.setattr(tg_cloud, "_is_admin", lambda oid: False)
    limit = asyncio.run(tg_cloud._effective_limit(_NoQuotaRow(), UID))
    assert limit > 0, (
        "потолок 0 означает не «нет доступа», а «сколько угодно»: store_file "
        "проверяет квоту под `if limit > 0`")
    assert limit == tg_cloud.DEFAULT_PAID_LIMIT


def test_platform_owner_is_still_unlimited(monkeypatch):
    monkeypatch.setattr(tg_cloud, "_is_admin", lambda oid: True)
    assert asyncio.run(tg_cloud._effective_limit(_Pool(), UID)) == 0


# ── Имя файла: заголовок ответа переживает кириллицу ─────────────────────────

def test_safe_name_drops_newlines_and_paths():
    assert M._cloud_safe_name("отчёт\r\nX-Evil: 1") == "отчётX-Evil: 1"
    assert M._cloud_safe_name("../../etc/passwd") == "passwd"
    assert M._cloud_safe_name(r"C:\Users\a\секрет.txt") == "секрет.txt"
    assert M._cloud_safe_name("") == "file"
    assert M._cloud_safe_name("..") == "file"
    assert M._cloud_safe_name(".gitignore") == ".gitignore"


def test_content_disposition_survives_cyrillic():
    """Значения заголовков читаются как latin-1: без filename* имя — мусор."""
    cd = M._content_disposition("отчёт.xlsx")
    assert "filename*=UTF-8''" in cd, "нет RFC 5987 — браузер покажет «Ð¾Ñ‚Ñ‡Ñ‘Ñ‚»"
    assert urllib.parse.quote("отчёт.xlsx", safe="") in cd
    assert cd.encode("ascii"), "сам заголовок обязан быть ASCII"


def test_download_marks_the_type_as_final():
    src = Path(M.__file__).read_text("utf-8")
    block = src[src.index("async def cloud_download"):]
    block = block[:block.index("async def cloud_delete")]
    assert '"X-Content-Type-Options": "nosniff"' in block, (
        "Content-Type берётся из имени файла — браузеру нельзя разрешать гадать")
    assert "_content_disposition(" in block


# ── Мини-апп: отказ до отправки и multipart вместо base64 ────────────────────

def _cloud_upload_js() -> str:
    src = INDEX.read_text("utf-8")
    start = src.index("async function cloudUpload(input)")
    return src[start:src.index("\n}", start) + 2]


def test_miniapp_sends_multipart_not_base64():
    body = _cloud_upload_js()
    assert "new FormData()" in body and "fd.append('file'" in body
    assert "data_b64" not in body, "base64-в-JSON снова упрётся в предел тела"
    assert "readAsDataURL" not in body, "чтение файла целиком в память браузера"


def test_miniapp_refuses_oversized_file_before_sending():
    body = _cloud_upload_js()
    assert "_CLOUD_MAX_UPLOAD" in body and "file.size >" in body, (
        "нет проверки размера до отправки — пользователь ждёт отказа в конце")


def test_miniapp_upload_has_its_own_timeout():
    body = _cloud_upload_js()
    assert "timeoutMs" in body, (
        "общий 30-секундный таймаут оборвёт загрузку файла на мегабайты")
    assert "const _ms = Number(opts.timeoutMs)" in INDEX.read_text("utf-8"), (
        "api() обязан этот таймаут принимать, иначе он ни на что не влияет")
