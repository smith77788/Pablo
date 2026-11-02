"""Мини-апп отдаётся сжатым и с ETag.

Находка аудита №7. index.html ~1,25 МБ отдавался КАЖДОМУ открытию целиком и без
сжатия (`web.FileResponse` без компрессии и заголовков кэша). Пользователи
открывают приложение с телефона по мобильной сети — там лишний мегабайт стоит
дороже всего. gzip даёт ~×4,6, ETag убирает повторную передачу вовсе.

Проверяем по РЕАЛЬНОМУ HTTP через aiohttp-тестклиент, а не чтением исходника:
заголовок `Content-Encoding` можно поставить и не сжав тело.

ВАЖНО про измерение: клиент aiohttp по умолчанию сам распаковывает ответ и сам
подставляет `Accept-Encoding: gzip`. Поэтому тут `auto_decompress=False` (иначе
меряли бы РАСПАКОВАННЫЙ размер и «экономия» выходила бы 0%) и явный
`Accept-Encoding: identity` для проверки несжатой отдачи.
"""
from __future__ import annotations

import gzip
import os

import pytest

pytest.importorskip("aiohttp")
from aiohttp import web                                    # noqa: E402
from aiohttp.test_utils import TestClient, TestServer      # noqa: E402
from tests.miniapp_source import api_source

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(ROOT, "mini_app", "index.html")


def _build_app() -> web.Application:
    """Поднять ровно тот блок отдачи статики, что работает в проде.

    Импортировать mini_app_api целиком нельзя (тянет всю платформу), поэтому
    вырезаем блок по границам и исполняем — так тест ломается, если блок в
    проде подменят на несжатую отдачу.
    """
    import logging
    src = api_source()
    i = src.index("    _static_dir = os.path.join")
    j = src.index('        log.info("Mini App static served')
    block = src[i:j].replace(
        "os.path.dirname(os.path.dirname(__file__))", repr(ROOT))
    ns = {"os": os, "web": web, "log": logging.getLogger("t")}
    app = web.Application()
    exec("def _setup(app):\n" + "\n".join("    " + ln for ln in block.split("\n")), ns)
    ns["_setup"](app)
    return app


@pytest.fixture
async def client():
    cli = TestClient(TestServer(_build_app()), auto_decompress=False)
    await cli.start_server()
    yield cli
    await cli.close()


async def test_index_is_gzipped_over_the_wire(client):
    raw = os.path.getsize(INDEX)
    r = await client.get("/miniapp", headers={"Accept-Encoding": "gzip"})
    body = await r.read()
    assert r.status == 200
    assert r.headers.get("Content-Encoding") == "gzip"
    assert len(body) < raw / 3, (
        f"мини-апп ушёл почти несжатым: {len(body)} из {raw}")
    # Тело действительно валидный gzip исходного файла, а не просто заголовок.
    assert gzip.decompress(body) == open(INDEX, "rb").read()


async def test_repeat_open_gets_304_and_no_body(client):
    r1 = await client.get("/miniapp", headers={"Accept-Encoding": "gzip"})
    etag = r1.headers.get("ETag")
    assert etag, "нет ETag — повторное открытие снова качает всё"
    r2 = await client.get("/miniapp",
                          headers={"Accept-Encoding": "gzip", "If-None-Match": etag})
    assert r2.status == 304
    assert await r2.read() == b""


async def test_client_without_gzip_still_gets_full_file(client):
    """Деградация: клиент без gzip обязан получить рабочий несжатый файл."""
    r = await client.get("/miniapp", headers={"Accept-Encoding": "identity"})
    body = await r.read()
    assert r.status == 200
    assert r.headers.get("Content-Encoding") != "gzip"
    assert len(body) == os.path.getsize(INDEX)


# ── Скрипты экранов и стили ──────────────────────────────────────────────────
# Замер холодного старта (2026-11-02): index.html уже уходил сжатым, а ~30
# файлов screens/*.js и ui_kit.css (~620 КБ) — как есть, на каждое открытие.

SCREENS = os.path.join(ROOT, "mini_app", "screens")


def _assets():
    out = ["ui_kit.css"] + ["screens/" + n for n in sorted(os.listdir(SCREENS)) if n.endswith(".js")]
    assert len(out) > 10
    return out


async def test_screen_scripts_are_gzipped_and_valid(client):
    raw_total = wire_total = 0
    for rel in _assets():
        path = os.path.join(ROOT, "mini_app", rel)
        r = await client.get("/miniapp/" + rel, headers={"Accept-Encoding": "gzip"})
        body = await r.read()
        assert r.status == 200, rel
        assert r.headers.get("Content-Encoding") == "gzip", rel
        assert gzip.decompress(body) == open(path, "rb").read(), rel
        ctype = r.headers.get("Content-Type", "")
        assert ("javascript" in ctype) if rel.endswith(".js") else ("text/css" in ctype), rel
        raw_total += os.path.getsize(path)
        wire_total += len(body)
    # Итог замера: по сети уходит меньше трети исходных байт.
    assert wire_total < raw_total / 3, f"{wire_total} из {raw_total}"


async def test_screen_script_repeat_open_is_304(client):
    rel = _assets()[1]
    r1 = await client.get("/miniapp/" + rel, headers={"Accept-Encoding": "gzip"})
    etag = r1.headers.get("ETag")
    assert etag
    r2 = await client.get("/miniapp/" + rel,
                          headers={"Accept-Encoding": "gzip", "If-None-Match": etag})
    assert r2.status == 304 and await r2.read() == b""


async def test_screen_script_without_gzip_is_whole(client):
    rel = _assets()[1]
    r = await client.get("/miniapp/" + rel, headers={"Accept-Encoding": "identity"})
    assert r.status == 200 and r.headers.get("Content-Encoding") != "gzip"
    assert len(await r.read()) == os.path.getsize(os.path.join(ROOT, "mini_app", rel))


async def test_asset_route_does_not_escape_the_folder(client):
    for bad in ("/miniapp/../services/onboarding.js", "/miniapp/screens/%2e%2e/%2e%2e/main.js",
                "/miniapp/nope.js"):
        r = await client.get(bad, headers={"Accept-Encoding": "gzip"})
        assert r.status == 404, bad


async def test_other_static_still_served(client):
    r = await client.get("/miniapp/manifest.webmanifest")
    assert r.status == 200
