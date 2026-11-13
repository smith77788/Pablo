"""Ленивая загрузка кода мини-аппа (services/miniapp_lazy.py).

Первое открытие тянуло ~510 КБ brotli, из них ~420 КБ — код разделов, которые
на старте не вызываются. Сервер режет страницу: функции, на которые нет
ничего, кроме прямых вызовов, уходят в куски `lazy/<хэш>.js`, на их место
встают синхронные подгрузчики.

Здесь проверяется:
* лексер и поиск верхнеуровневых функций — на ловушках и на настоящем
  мини-аппе против дерева TypeScript (набор имён совпадает один в один);
* правила безопасности: ссылка-значение, `window.имя = …`, `new имя`,
  `имя.свойство`, дубль, имя переменной, `window[k] = …` — функция остаётся
  на месте (или страница не режется вовсе);
* остаток страницы и каждый кусок разбираются без ошибок, а вместе они
  содержат ровно те функции, что были;
* сервер: первая отдача — полная страница и нарезка в фоне; дальше —
  ленивая с загрузчиком; кусок по хэшу кэшируется навсегда; `?full=1` и
  MINIAPP_LAZY=0 — полная;
* бюджет первого открытия.

Поведение в браузере (паритет функций и обход всех экранов) —
deploy/scripts/lazy_check.mjs.
"""
from __future__ import annotations

import asyncio
import gzip
import json
import os
import re
import subprocess

import pytest

from services import miniapp_lazy as lz
from services import miniapp_minify as mm
from tests.test_miniapp_minify import _typescript

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "mini_app")


def _names(src: str) -> list[str]:
    return [f.name for f in lz.top_level_functions(lz.tokenize(src))]


def test_finds_only_top_level_declarations():
    src = (
        "'use strict';\n"
        "function a(x, y = 1) { const s = '}function no(){'; return `${ {k: 1}.k }}`; }\n"
        "async function b() { function inner() {} return /}{/.test('x') }\n"
        "function* g() {}\n"
        "const e = function named() {};\n"
        "const f = async function () {};\n"
        "x = 1\nfunction c() { if (a) { return { d: function () {} } } }\n"
        "const r = 4 / 2 / 1; function d() { return r }\n"
        "foo(function cb() {});\n"
    )
    assert _names(src) == ["a", "b", "g", "c", "d"]
    fs = {f.name: f for f in lz.top_level_functions(lz.tokenize(src))}
    assert src[fs["b"].start:fs["b"].end].startswith("async function b()")
    assert fs["a"].length == 1                       # y = 1 — уже не считается
    assert src[fs["c"].start:fs["c"].end].endswith("} } }")


def _split(index_js: str, screens: dict[str, str] | None = None, boot=None) -> lz.Split:
    screens = screens or {}
    html = ("<html><head><script>/*мелкий*/</script></head><body>"
            "<button onclick=\"fromHtml(1)\">x</button>"
            f"<script>{index_js}</script>"
            + "".join(f'<script src="{k}"></script>' for k in screens)
            + "</body></html>")
    return lz.split_page(html, screens.get, lambda t, _l: t, boot=boot, min_inline=0)


def _lazy(sp: lz.Split) -> set[str]:
    return {n for c in sp.chunks for n in c.names}


def test_only_call_only_functions_go_lazy():
    js = (
        "'use strict';\n"
        "function called() { return 1 }\n"
        "function fromHtml(n) { return n }\n"
        "function asValue() {}\n"
        "function wrapped() {}\n"
        "function withProp() {}\n"
        "function ctor() {}\n"
        "function twice() {}\n"
        "function shadowed() {}\n"
        "function viaWindow() {}\n"
        "function inString() {}\n"
        "function guarded() {}\n"
        "const v = called() + (typeof guarded === 'function' ? 1 : 0);\n"
        "setTimeout(asValue, 1);\n"
        "window.wrapped = function () {};\n"
        "withProp.cache = {};\n"
        "new ctor();\n"
        "let shadowed2 = 1;\n"
        "window.viaWindow();\n"
        "el.innerHTML = '<b onclick=\"inString.x()\">';\n"
    )
    sp = _split(js, {"screens/a.js": "function twice() {}\nvar shadowed = 2;\n"})
    lazy = _lazy(sp)
    assert {"called", "fromHtml", "guarded", "viaWindow"} <= lazy
    for nm in ("asValue", "wrapped", "withProp", "ctor", "twice", "shadowed", "inString"):
        assert nm not in lazy, nm
        assert nm in sp.kept, nm


def test_boot_functions_stay_in_the_page():
    sp = _split("function hot() {}\nfunction cold() {}\nhot();\n", boot={"hot"})
    assert _lazy(sp) == {"cold"}
    assert "function hot()" in sp.html and "function cold()" not in sp.html


def test_dynamic_window_assignment_refuses_the_split():
    with pytest.raises(ValueError):
        _split("function a() {}\nwindow[k] = function () {};\n")
    with pytest.raises(ValueError):
        _split("function a() {}\nObject.assign(window, {a: 1});\n")
    # Чтение и вызов по имени — можно: так плитки и сервер зовут разделы.
    sp = _split("function a() {}\nif (typeof window[k] === 'function') window[k]();\n")
    assert _lazy(sp) == {"a"}


def test_strict_mode_follows_the_source():
    sp = _split("'use strict';\nfunction s() {}\n",
                {"screens/x.js": "function loose() { return this }\n"})
    by = {c.names[0]: c for c in sp.chunks}
    assert by["s"].strict and by["s"].body.startswith('"use strict";')
    assert not by["loose"].strict and not by["loose"].body.startswith('"use strict"')


def test_loader_comes_before_every_cut_script_and_screen_residue_is_inline():
    sp = _split("function a() {}\n", {"screens/x.js": "function b() {}\nconst k = 1;\n"})
    i = sp.html.index("__lazyLoadAll")
    assert i < sp.html.index("const k = 1")
    assert 'src="screens/x.js"' not in sp.html


def test_markup_containers_become_empty_shells():
    filler = "<p>" + "x" * 300 + "</p>"
    html = ("<body><div id=\"app\">"
            f"<div class=\"screen show\" id=\"s-start\">{filler}</div>"
            f"<div class=\"screen\" id=\"s-home\">{filler}</div>"
            f"<div class=\"screen\" id=\"s-a\"><div><input id=\"aIn\" class=\"fld\"></div>{filler}"
            f"<div class=\"mbg\" id=\"inner-modal\">{filler}</div></div>"
            f"<div class=\"mbg\" id=\"m-b\"><span data-key=\"k1\">{filler}</span></div>"
            f"<div class=\"screen\" id=\"s-script\"><script>var q = '<div>';</script>{filler}</div>"
            "<div class=\"screen\" id=\"s-tiny\"><b>1</b></div>"
            "</div></body>")
    page, inner, ids, words = lz.defer_markup(html, {"s-home"})
    assert set(inner) == {"s-a", "m-b"}          # показанный, оставленный, со скриптом, пустой — на месте
    assert '<div class="screen" id="s-a" data-lz="1"></div>' in page
    assert '<div class="mbg" id="m-b" data-lz="1"></div>' in page
    assert "inner-modal" in inner["s-a"] and "inner-modal" not in page
    assert ids["s-a"] == ["aIn", "inner-modal"]
    assert {"input", "fld", "span", "data-key", "k1", "p"} <= words
    assert "screen" not in words                 # класс каркаса остаётся в странице
    # Вынули и вставили назад — та же страница.
    back = page
    for cid, part in inner.items():
        back = back.replace(f'id="{cid}" data-lz="1">', f'id="{cid}">' + part, 1)
    assert back == html


def test_markup_chunk_is_a_script_that_registers_html():
    sp = _split("function a() {}\n")
    filler = "<i>" + "y" * 300 + "</i>"
    html = (f"<body><div class=\"screen\" id=\"s-x\">{filler}</div>"
            "<script>function a() {}\nfunction b() {}\nb();\n</script></body>")
    sp = lz.split_page(html, lambda _r: None, lambda t, _l: t, min_inline=0)
    assert sp.deferred == 1 and len(sp.markup) == 1
    body = sp.markup[0].body
    assert body.startswith("__lzHtml(") and body.endswith(");")
    assert json.loads(body[len("__lzHtml("):-2])["s-x"] == filler
    assert sp.html.index('data-lz="1"') < sp.html.index("__lzHtml")   # загрузчик — после разметки


# ── Настоящий мини-апп ────────────────────────────────────────────────────

def _read(rel):
    p = os.path.join(APP, rel)
    return open(p, encoding="utf-8").read() if os.path.exists(p) else None


@pytest.fixture(scope="module")
def real():
    html = mm.minify_html(open(os.path.join(APP, "index.html"), encoding="utf-8").read())
    sp = lz.split_page(html, _read, lambda t, _l: mm.strip_js(t), boot=lz.read_boot(APP))
    return html, sp


def test_real_app_moves_most_code_out(real):
    import brotli  # noqa: F401 — тот же пакет, что у сервера
    html, sp = real
    assert sp.lazy_names > 1500, sp.lazy_names
    # Остались на месте — по причине, и каждая причина из закрытого списка.
    assert all(re.match(r"(ссылка-значение|в строке|нужна на старте|объявлена дважды|"
                        r"имя занято|new |function )", w) for w in sp.kept.values()), sp.kept


_TS = r"""
const ts = require(process.argv[1]);
const fs = require('fs');
const items = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const out = {};
for (const [label, src] of items) {
  const f = ts.createSourceFile('x.js', src, ts.ScriptTarget.Latest, true, ts.ScriptKind.JS);
  out[label] = { errors: f.parseDiagnostics.length,
    funcs: f.statements.filter(s => s.kind === ts.SyntaxKind.FunctionDeclaration && s.name)
      .map(s => s.name.text) };
}
console.log(JSON.stringify(out));
"""


def _ts_run(tmp_path, items):
    ts = _typescript()
    if not ts:
        pytest.skip("нужны node и typescript")
    data = tmp_path / "items.json"
    data.write_text(json.dumps(items), encoding="utf-8")
    proc = subprocess.run(["node", "-e", _TS, ts, str(data)], capture_output=True, text=True,
                          timeout=600)
    assert proc.stdout.strip(), proc.stderr[-800:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_function_set_matches_typescript(tmp_path, real):
    """Нарезчик видит ровно те верхнеуровневые функции, что и TypeScript."""
    html, _sp = real
    items = [(f"index#{k}", m.group(1)) for k, m in enumerate(lz._SCRIPT_TAG.finditer(html))]
    for m in lz._SRC_TAG.finditer(html):
        items.append((m.group(1), mm.strip_js(_read(m.group(1)))))
    ts = _ts_run(tmp_path, items)
    for label, src in items:
        assert _names(src) == ts[label]["funcs"], label


def test_residue_and_chunks_parse_and_keep_every_function(tmp_path, real):
    html, sp = real
    items = [(f"page#{k}", m.group(1)) for k, m in enumerate(lz._SCRIPT_TAG.finditer(sp.html))]
    items += [(c.url, c.body) for c in sp.chunks + sp.markup]
    ts = _ts_run(tmp_path, items)
    assert all(v["errors"] == 0 for v in ts.values()), {k: v["errors"] for k, v in ts.items() if v["errors"]}
    before = []
    for k, m in enumerate(lz._SCRIPT_TAG.finditer(html)):
        before += ts_names_of(m.group(1))
    for m in lz._SRC_TAG.finditer(html):
        before += ts_names_of(mm.strip_js(_read(m.group(1))))
    after = [n for v in ts.values() for n in v["funcs"]]
    assert sorted(before) == sorted(after)


def ts_names_of(src):
    return _names(src)


def test_first_open_budget(real):
    """Бюджет первого открытия С ленивой загрузкой: страница, SDK Telegram и
    стили — то, что качается до главного экрана (gzip, как минимум у любого
    клиента). Было 660 КБ (всё сразу), стало ~150 КБ. Код и разметка новых разделов в
    этот бюджет не попадает — он уходит в куски сам; растёт бюджет только от
    разметки и того, что зовёт старт."""
    _html, sp = real
    total = len(gzip.compress(sp.html.encode(), 6))
    for rel in ("telegram-web-app.js", "ui_kit.css"):
        total += len(gzip.compress(_read(rel).encode(), 6))
    assert total < 170 * 1024, f"первое открытие {total // 1024} КБ — больше бюджета 170 КБ"


# ── Сервер ────────────────────────────────────────────────────────────────

pytest.importorskip("aiohttp")
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from tests.test_miniapp_compression import _build_app  # noqa: E402


@pytest.fixture
async def client(monkeypatch):
    monkeypatch.delenv("MINIAPP_LAZY", raising=False)
    cli = TestClient(TestServer(_build_app()), auto_decompress=False)
    await cli.start_server()
    yield cli
    await cli.close()


async def _lazy_page(client, path="/miniapp"):
    for _ in range(300):
        r = await client.get(path, headers={"Accept-Encoding": "gzip"})
        body = gzip.decompress(await r.read()).decode()
        if "__lazyLoadAll" in body:
            return r, body
        await asyncio.sleep(0.1)
    raise AssertionError("ленивая страница так и не появилась")


async def test_server_serves_full_first_then_lazy_and_chunks(client):
    r = await client.get("/miniapp", headers={"Accept-Encoding": "gzip"})
    first = gzip.decompress(await r.read()).decode()
    assert "__lazyLoadAll" not in first          # нарезка ещё в фоне — полная страница
    r, body = await _lazy_page(client)
    assert r.headers["ETag"].endswith('-lz"')
    urls = re.findall(r'"(lazy/[0-9a-f]{16}\.js)"', body)
    assert len(urls) >= 5
    c = await client.get("/miniapp/" + urls[0], headers={"Accept-Encoding": "gzip"})
    assert c.status == 200
    assert "immutable" in c.headers["Cache-Control"]
    assert "javascript" in c.headers["Content-Type"]
    assert "function " in gzip.decompress(await c.read()).decode()
    again = await client.get("/miniapp/" + urls[0], headers={"If-None-Match": c.headers["ETag"]})
    assert again.status == 304
    assert (await client.get("/miniapp/lazy/0123456789abcdef.js")).status == 404
    # Полная — по ?full=1: ею страница лечит себя, если кусок не загрузился.
    rf = await client.get("/miniapp?full=1", headers={"Accept-Encoding": "gzip"})
    assert "__lazyLoadAll" not in gzip.decompress(await rf.read()).decode()
    assert rf.headers["ETag"] != r.headers["ETag"]


async def test_switch_off_serves_full(client, monkeypatch):
    await _lazy_page(client)
    monkeypatch.setenv("MINIAPP_LAZY", "0")
    r = await client.get("/miniapp", headers={"Accept-Encoding": "gzip"})
    assert "__lazyLoadAll" not in gzip.decompress(await r.read()).decode()


async def test_broken_split_falls_back_to_full(client, monkeypatch):
    def boom(*_a, **_k):
        raise ValueError("сбой разбора")
    monkeypatch.setattr(lz, "split_page", boom)
    await client.get("/miniapp")
    for _ in range(50):
        await asyncio.sleep(0.05)
    r = await client.get("/miniapp", headers={"Accept-Encoding": "gzip"})
    assert r.status == 200
    assert "__lazyLoadAll" not in gzip.decompress(await r.read()).decode()


async def test_chunk_before_any_page_warms_up_instead_of_404(monkeypatch):
    """Свежий процесс (деплой, другая реплика): страницу отдал не он, а кусок
    просят у него. Нарезка детерминирована — он режет сам и отдаёт кусок."""
    monkeypatch.delenv("MINIAPP_LAZY", raising=False)
    a = TestClient(TestServer(_build_app()), auto_decompress=False)
    await a.start_server()
    try:
        _r, body = await _lazy_page(a)
    finally:
        await a.close()
    url = re.findall(r'"(lazy/[0-9a-f]{16}\.js)"', body)[0]
    b = TestClient(TestServer(_build_app()), auto_decompress=False)
    await b.start_server()
    try:
        r = await b.get("/miniapp/" + url, headers={"Accept-Encoding": "gzip"})
        assert r.status == 200
        assert "function " in gzip.decompress(await r.read()).decode()
    finally:
        await b.close()
