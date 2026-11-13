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
async def client(monkeypatch):
    # Здесь проверяется сжатие ПОЛНОЙ страницы. Ленивая (нарезанная) версия
    # подменяет её через ~1 с после первого открытия — её отдачу и куски
    # проверяет tests/test_miniapp_lazy.py.
    monkeypatch.setenv("MINIAPP_LAZY", "0")
    cli = TestClient(TestServer(_build_app()), auto_decompress=False)
    await cli.start_server()
    yield cli
    await cli.close()



def _served(path) -> bytes:
    """Файл таким, каким его отдаёт сервер: без комментариев и отступов."""
    from services import miniapp_minify as mm
    src = open(path, encoding="utf-8").read()
    p = str(path)
    if p.endswith(".html"):
        # Страница уходит со ссылками `?v=<версия>` на свои скрипты и стили
        # (services/miniapp_assets.py) — ровно это и сравниваем.
        from services import miniapp_assets as ma
        html = mm.minify_html(src)
        return ma.stamp_versions(
            html, ma.versions_for(os.path.dirname(p), html)).encode("utf-8")
    fn = mm.strip_js if p.endswith(".js") else mm.strip_css
    return fn(src).encode("utf-8")


async def test_index_is_gzipped_over_the_wire(client):
    raw = os.path.getsize(INDEX)
    r = await client.get("/miniapp", headers={"Accept-Encoding": "gzip"})
    body = await r.read()
    assert r.status == 200
    assert r.headers.get("Content-Encoding") == "gzip"
    assert len(body) < raw / 3, (
        f"мини-апп ушёл почти несжатым: {len(body)} из {raw}")
    # Тело — валидный gzip того, что сервер отдаёт: исходника без комментариев
    # и отступов (services/miniapp_minify.py), а не просто заголовок.
    assert gzip.decompress(body) == _served(INDEX)


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
    assert body == _served(INDEX)


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
        assert gzip.decompress(body) == _served(path), rel
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


# ── Повторное открытие без сети ──────────────────────────────────────────────
# Замер 08.11.2026 (Chromium, процессор ×4, мобильная сеть 150 мс): повторное
# открытие главной стоило ~1,5 с, из них 36 перепроверок скриптов и стилей по
# сети, по одной задержке на файл. Теперь ссылки несут версию файла, ответ на
# адрес с текущей версией кэшируется навсегда, и повторное открытие берёт их
# из кэша браузера: ~0,8 с.

async def test_page_links_carry_file_versions(client):
    from services import miniapp_assets as ma
    r = await client.get("/miniapp", headers={"Accept-Encoding": "identity"})
    html = (await r.read()).decode("utf-8")
    assert r.headers.get("Cache-Control") == "no-cache", "страница обязана перепроверяться"
    import re
    stamped = re.findall(r'(?:src|href)="[A-Za-z0-9_\-/.]+\.(?:js|css)\?v=[0-9a-f]+"', html)
    assert len(stamped) > 10, "ссылки на скрипты экранов без версии"
    # asset_refs находит только ссылки БЕЗ версии — их не должно остаться.
    unversioned = ma.asset_refs(html)
    assert not unversioned, f"ссылки без версии перепроверяются на каждом открытии: {unversioned}"


async def test_versioned_asset_is_cached_forever_and_bare_is_not(client):
    from services import miniapp_assets as ma
    rel = "screens/ui_kit.js"
    v = ma.version_token(os.path.join(ROOT, "mini_app", rel))
    r = await client.get(f"/miniapp/{rel}?v={v}", headers={"Accept-Encoding": "gzip"})
    assert r.status == 200
    assert "immutable" in r.headers.get("Cache-Control", "")
    # Без версии или со старой — по-прежнему с перепроверкой: адрес без
    # отпечатка может завтра отдать другой файл.
    for q in ("", "?v=deadbeef"):
        r = await client.get(f"/miniapp/{rel}{q}", headers={"Accept-Encoding": "gzip"})
        assert r.headers.get("Cache-Control") == "no-cache", q


async def test_page_etag_changes_when_only_a_screen_script_changes(client):
    """Деплой, поменявший один скрипт экрана, обязан поменять ETag страницы.

    Иначе браузер получит 304 на страницу со СТАРЫМИ ссылками, а старый адрес
    закэширован навсегда — экран застрял бы на прошлой версии.
    """
    rel = os.path.join(ROOT, "mini_app", "screens", "ui_kit.js")
    r1 = await client.get("/miniapp", headers={"Accept-Encoding": "gzip"})
    e1 = r1.headers.get("ETag")
    st = os.stat(rel)
    try:
        os.utime(rel, (st.st_atime, st.st_mtime + 7))
        r2 = await client.get("/miniapp", headers={"Accept-Encoding": "gzip",
                                                    "If-None-Match": e1})
        assert r2.status == 200, "страница отдала 304 со старыми ссылками"
        assert r2.headers.get("ETag") != e1
    finally:
        os.utime(rel, (st.st_atime, st.st_mtime))


# ── Brotli ───────────────────────────────────────────────────────────────────
# Замер 09.11.2026 (3G 1,6 Мбит/с, CPU 4x): готовая главная 4,3 → 3,6 с.
# Страница 420 → 299 КБ, до первого экрана 616 → 497 КБ. Сжатие идёт в фоне,
# пока не готово — gzip (services/miniapp_brotli.py).

async def _wait_br(client, path, ae="gzip, deflate, br"):
    import asyncio
    for _ in range(400):
        r = await client.get(path, headers={"Accept-Encoding": ae})
        body = await r.read()
        if r.headers.get("Content-Encoding") == "br":
            return r, body
        assert r.headers.get("Content-Encoding") == "gzip", "пока brotli не готов — gzip"
        await asyncio.sleep(0.05)
    raise AssertionError(f"{path}: brotli так и не появился")


async def test_index_is_brotli_once_ready_and_same_page(client):
    brotli = pytest.importorskip("brotli")
    r, body = await _wait_br(client, "/miniapp")
    assert brotli.decompress(body) == _served(INDEX)
    assert r.headers.get("Vary") == "Accept-Encoding"
    gz = await (await client.get("/miniapp", headers={"Accept-Encoding": "gzip"})).read()
    assert len(body) < len(gz) * 0.85, f"brotli {len(body)} против gzip {len(gz)}"
    # Клиент без br по-прежнему получает gzip: кэш brotli ему не отдаётся.
    r2 = await client.get("/miniapp", headers={"Accept-Encoding": "gzip"})
    assert r2.headers.get("Content-Encoding") == "gzip"


async def test_screen_script_is_brotli_once_ready(client):
    brotli = pytest.importorskip("brotli")
    rel = "screens/accounts_list.js"
    _, body = await _wait_br(client, "/miniapp/" + rel)
    assert brotli.decompress(body) == _served(os.path.join(ROOT, "mini_app", rel))


async def test_br_only_client_gets_a_readable_body(client):
    """Клиент «только br», пока сжатие не готово, не получает gzip, о котором не просил."""
    from services import miniapp_brotli as mb
    mb._br_cache.clear()
    rel = "screens/bots_list.js"
    r = await client.get("/miniapp/" + rel, headers={"Accept-Encoding": "br"})
    body = await r.read()
    enc = r.headers.get("Content-Encoding")
    assert enc in (None, "br"), enc
    if enc is None:
        assert body == _served(os.path.join(ROOT, "mini_app", rel))


def test_accepts_parses_the_header():
    from services import miniapp_brotli as mb
    assert mb.accepts("gzip, deflate, br")
    assert mb.accepts("br;q=1.0, gzip;q=0.8")
    assert not mb.accepts("gzip, deflate")
    assert not mb.accepts("br;q=0, gzip")
    assert not mb.accepts("xbr, gzip")
    assert not mb.accepts(None)
