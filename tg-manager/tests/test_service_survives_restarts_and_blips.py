"""Перезапуск и секундный сбой сети не должны быть видны пользователю.

Три дыры, из-за которых сервис «работает с перебоями» (документ требований
надёжности, R2/R5/R6):

1. Режим вебхука ставился с `drop_pending_updates=True`: всё, что люди писали
   боту в минуту деплоя (а деплой едет на каждый принятый PR), выбрасывалось.
   Старый контейнер на остановке звал `delete_webhook()` и снимал вебхук,
   который уже поставил новый, — бот глох до следующего деплоя.
2. Вебхуки ботов клиентов включались только по PUBLIC_BASE_URL/WEBHOOK_URL.
   Без них все боты клиентов шли опросом раз в 10 секунд, хотя публичный адрес
   у сервера есть — на нём живёт мини-апп.
3. Мини-апп показывал красную ошибку на первом же обрыве или 502, не
   повторив чтение.
"""
from __future__ import annotations

import ast
import asyncio
import json
import pathlib
import shutil
import subprocess
import time
from datetime import datetime, timezone

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from tests.miniapp_source import js_function

ROOT = pathlib.Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


# ── 1. Главный бот: накопленное не выбрасывается, вебхук не снимается ───────

def _main_calls(attr: str) -> list[ast.Call]:
    tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
    return [n for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == attr]


def _kw(call: ast.Call, name: str):
    for k in call.keywords:
        if k.arg == name:
            return ast.literal_eval(k.value)
    return "<нет>"


def test_webhook_mode_keeps_updates_that_piled_up_during_restart():
    calls = _main_calls("set_webhook")
    assert calls, "set_webhook пропал из main.py"
    for c in calls:
        assert _kw(c, "drop_pending_updates") is False, (
            "вебхук ставится с выбросом накопленных апдейтов: сообщения, "
            "отправленные боту во время деплоя, пропадут без ответа")


def test_polling_gets_no_fake_drop_flag():
    """У start_polling в aiogram 3 нет drop_pending_updates: аргумент уходит в
    данные обработчиков и только вводит читателя в заблуждение."""
    for c in _main_calls("start_polling"):
        assert _kw(c, "drop_pending_updates") == "<нет>"


def test_old_container_does_not_remove_the_new_containers_webhook():
    assert not _main_calls("delete_webhook"), (
        "main.py снимает вебхук главного бота: на деплое старый контейнер "
        "снимет вебхук, который уже поставил новый, и бот оглохнет")


def _msg(age_sec: float, chat_type: str = "private"):
    when = datetime.fromtimestamp(time.time() - age_sec, tz=timezone.utc)
    from aiogram.types import Chat, Message
    return Message(message_id=1, date=when, chat=Chat(id=1, type=chat_type), text="/start")


def _through(event) -> bool:
    from bot.middlewares.stale_update import StaleUpdateMiddleware
    seen = []

    async def handler(e, d):
        seen.append(e)

    asyncio.run(StaleUpdateMiddleware()(handler, event, {}))
    return bool(seen)


def test_message_from_a_short_restart_is_answered(monkeypatch):
    monkeypatch.delenv("STALE_UPDATE_MAX_AGE_SEC", raising=False)
    assert _through(_msg(90)), "сообщение, пришедшее за минуту деплоя, выброшено"


def test_yesterdays_private_message_is_skipped_and_counted(monkeypatch):
    from services import metrics
    from bot.middlewares import stale_update as su
    monkeypatch.delenv("STALE_UPDATE_MAX_AGE_SEC", raising=False)
    assert not _through(_msg(86_400)), "на вчерашнее /start бот ответил бы сейчас"
    assert su.SKIPPED in metrics.render(), "пропуск не виден в метриках"


def test_old_group_message_still_reaches_the_chat_moderator(monkeypatch):
    monkeypatch.delenv("STALE_UPDATE_MAX_AGE_SEC", raising=False)
    assert _through(_msg(86_400, "supergroup")), (
        "старое сообщение в группе отброшено: спам, пришедший за простой, "
        "модератор не удалит")


def test_max_age_cannot_be_typoed_into_dropping_fresh_messages(monkeypatch):
    from bot.middlewares import stale_update as su
    monkeypatch.setenv("STALE_UPDATE_MAX_AGE_SEC", "5")
    assert su.max_age_sec() == 60
    monkeypatch.setenv("STALE_UPDATE_MAX_AGE_SEC", "мусор")
    assert su.max_age_sec() == 900


# ── 2. Боты клиентов: вебхук по адресу мини-аппа ────────────────────────────

_ENV = ("PUBLIC_BASE_URL", "WEBHOOK_URL", "MANAGED_BOTS_DELIVERY",
        "MINI_APP_URL", "RAILWAY_PUBLIC_DOMAIN")


@pytest.fixture
def mbw(monkeypatch):
    from services import managed_bot_webhooks as m
    for k in _ENV:
        monkeypatch.delenv(k, raising=False)
    m._probe_cache.clear()
    yield m
    m._probe_cache.clear()


def test_address_is_guessed_from_the_mini_app(mbw, monkeypatch):
    assert mbw.guessed_base_url() is None
    monkeypatch.setenv("RAILWAY_PUBLIC_DOMAIN", "infragram.up.railway.app")
    assert mbw.guessed_base_url() == "https://infragram.up.railway.app"
    monkeypatch.setenv("MINI_APP_URL", "https://app.example.com/miniapp/")
    assert mbw.guessed_base_url() == "https://app.example.com", (
        "без PUBLIC_BASE_URL боты клиентов остаются на опросе раз в 10 с")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://pub.example.com")
    assert mbw.guessed_base_url() is None, "явный адрес главнее угаданного"
    monkeypatch.delenv("PUBLIC_BASE_URL")
    monkeypatch.setenv("MANAGED_BOTS_DELIVERY", "polling")
    assert mbw.guessed_base_url() is None, "принудительный опрос — закон"


def _serve(app: web.Application):
    async def go(check):
        server = TestServer(app)
        await server.start_server()
        try:
            import aiohttp
            async with aiohttp.ClientSession() as http:
                return await check(http, str(server.make_url("")).rstrip("/"))
        finally:
            await server.close()
    return go


def test_probe_accepts_only_our_own_hook_receiver(mbw):
    ours = web.Application()
    ours.router.add_post(mbw.HOOK_PREFIX + "{bot_id}",
                         mbw.make_webhook_route(pool=None))
    foreign = web.Application()

    async def yes(http, base):
        return await mbw._guessed_base_is_ours(http, base)

    assert asyncio.run(_serve(ours)(yes)) is True
    mbw._probe_cache.clear()
    assert asyncio.run(_serve(foreign)(yes)) is False, (
        "чужой хостинг страницы принят за наш приёмник вебхуков")


class _Pool:
    def __init__(self):
        self.queries: list[str] = []

    async def fetch(self, q, *a):
        self.queries.append(q)
        return [{"bot_id": 7, "token": "t"}]

    async def execute(self, q, *a):
        self.queries.append(q)
        return "OK"


def test_unconfirmed_guess_changes_nothing(mbw, monkeypatch):
    """Проверка адреса не прошла (блип) — сверка не снимает ботов с вебхука."""
    monkeypatch.setenv("MINI_APP_URL", "https://app.example.com/miniapp/")

    async def no(http, base):
        return False

    async def must_not(*a, **k):
        raise AssertionError("бот снят с вебхука из-за неподтверждённого адреса")

    monkeypatch.setattr(mbw, "_guessed_base_is_ours", no)
    monkeypatch.setattr(mbw, "unregister_webhook", must_not)
    pool = _Pool()
    stats = asyncio.run(mbw.sync(pool, http=None))
    assert stats == {"on": 0, "off": 0, "fallback": 0}
    assert pool.queries == []


def test_confirmed_guess_moves_bots_to_webhook(mbw, monkeypatch):
    monkeypatch.setenv("MINI_APP_URL", "https://app.example.com/miniapp/")
    used: list[str] = []

    async def yes(http, base):
        return True

    async def reg(http, token, bot_id, base):
        used.append(base)
        return True

    async def info(http, token, method, **kw):
        return {"result": {"url": mbw.hook_url("https://app.example.com", 7)}}

    monkeypatch.setattr(mbw, "_guessed_base_is_ours", yes)
    monkeypatch.setattr(mbw, "register_webhook", reg)
    from services import bot_api
    monkeypatch.setattr(bot_api, "_call", info)
    stats = asyncio.run(mbw.sync(_Pool(), http=None))
    assert used == ["https://app.example.com"], (
        "подтверждённый адрес мини-аппа не использован для вебхуков ботов")
    assert stats["on"] == 1


# ── 3. Мини-апп: чтение переживает секундный сбой ───────────────────────────

def _node(prog: str):
    out = subprocess.run([NODE, "-e", prog], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


_STUBS = """
function _resp(status, headers, body) {
  headers = headers || {};
  return { status, ok: status >= 200 && status < 300,
    headers: { get: k => headers[k] !== undefined ? String(headers[k])
                        : (k === 'Content-Type' ? 'application/json' : null) },
    clone() { return this; },
    async json() { return body || {}; }, async text() { return JSON.stringify(body || {}); } };
}
"""


@pytest.mark.skipif(not NODE, reason="нужен node")
def test_retry_rule_covers_blips_and_nothing_else():
    prog = _STUBS + js_function("_serverBooting") + js_function("_readRetryDelay") + """
const READ_RETRY_MAX = 2, READ_RETRY_BASE_MS = [800, 2500];
(async () => {
  const d = async (r, e, a) => await _readRetryDelay(r, e, a || 0);
  const out = {
    net: await d(null, new Error('Нет соединения с сервером')),
    timeout: await d(null, new Error('Сервер не отвечает (30с). Попробуйте позже.')),
    bad_gw: await d(_resp(502)), gw_to: await d(_resp(504)), unavail: await d(_resp(503)),
    booting: await d(_resp(503, {}, {starting: true})),
    rl_short: await d(_resp(429, {'Retry-After': 3})),
    rl_long: await d(_resp(429, {'Retry-After': 60})),
    client: await d(_resp(400)), server: await d(_resp(500)),
    third: await d(_resp(502), null, 2),
  };
  console.log(JSON.stringify(out));
})();
"""
    o = _node(prog)
    for k in ("net", "bad_gw", "gw_to", "unavail"):
        assert isinstance(o[k], (int, float)) and 0 < o[k] < 4000, (k, o[k])
    assert 3000 <= o["rl_short"] < 3400
    for k in ("timeout", "booting", "rl_long", "client", "server", "third"):
        assert o[k] is None, (k, o[k])


def _api_raw_source() -> str:
    """Тело `_apiRaw` целиком. Общий `js_function` спотыкается о параметр по
    умолчанию `opts={}` (берёт его скобку за тело), поэтому — до закрывающей
    скобки функции в начале строки."""
    from tests.miniapp_source import miniapp_html
    html = miniapp_html()
    i = html.index("async function _apiRaw(")
    return html[i:html.index("\n}\n", i) + 2]


def _api_run(method: str, script: list) -> dict:
    prog = _STUBS + js_function("_serverBooting") + js_function("_readRetryDelay") + _api_raw_source() + f"""
const READ_RETRY_MAX = 2, READ_RETRY_BASE_MS = [1, 1];
const BOOT_RETRY_MAX = 6, BOOT_RETRY_MS = 1, BOOT_MSG = 'boot';
const FETCH_TIMEOUT_MS = 30000; let TK = 't';
async function _reAuth() {{}} function showPaywall() {{}}
const script = {json.dumps(script)}; let calls = 0;
async function fetchT(path, opts) {{
  const step = script[Math.min(calls++, script.length - 1)];
  if (step === 'net') throw new Error('Нет соединения с сервером');
  return _resp(step, {{}}, {{ok: true}});
}}
(async () => {{
  let res, err = null;
  try {{ res = await _apiRaw('/api/miniapp/x', {{method: {json.dumps(method)}}}); }}
  catch (e) {{ err = e.message; }}
  console.log(JSON.stringify({{calls, err, res}}));
}})();
"""
    return _node(prog)


@pytest.mark.skipif(not NODE, reason="нужен node")
def test_a_read_survives_one_dropped_connection_and_one_bad_gateway():
    o = _api_run("GET", ["net", 502, 200])
    assert o["err"] is None, f"чтение упало на сбое, который прошёл сам: {o}"
    assert o["calls"] == 3


@pytest.mark.skipif(not NODE, reason="нужен node")
def test_a_write_is_never_repeated():
    o = _api_run("POST", ["net", 200])
    assert o["calls"] == 1, "запись повторена: операция могла запуститься дважды"
    assert o["err"]


@pytest.mark.skipif(not NODE, reason="нужен node")
def test_a_lasting_outage_still_reports_honestly():
    o = _api_run("GET", [502])
    assert o["calls"] == 3 and o["err"], o


# ── 4. Быстрые переходы по мини-аппу не упираются в 429 ─────────────────────

class _Req:
    path = "/api/miniapp/operations"
    headers: dict = {}

    @property
    def transport(self):
        class _T:
            def get_extra_info(self, _name):
                return ("203.0.113.9", 1)
        return _T()


def _limiter(monkeypatch, **env):
    import importlib
    import services.security as sec
    for k in ("RATE_LIMIT_USER_MAX", "RATE_LIMIT_MAX_REQUESTS"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "true")
    return importlib.reload(sec)


def _allowed(sec, n: int, uid) -> int:
    async def go():
        return sum([await sec.check_rate_limit_for_path(_Req(), uid) for _ in range(n)])
    return asyncio.run(go())


def test_signed_in_user_can_click_through_screens_quickly(monkeypatch):
    """Экран открывается пачкой из 5–15 запросов; 30 экранов за минуту — это
    300 запросов, и ни один не должен получить 429 (раньше потолок был 120)."""
    sec = _limiter(monkeypatch)
    assert _allowed(sec, 300, uid=777) == 300


def test_anonymous_traffic_keeps_the_strict_limit(monkeypatch):
    sec = _limiter(monkeypatch)
    assert _allowed(sec, 200, uid=None) == 120


def test_user_limit_is_configurable_but_never_below_the_anonymous_one(monkeypatch):
    sec = _limiter(monkeypatch, RATE_LIMIT_USER_MAX="10")
    assert _allowed(sec, 200, uid=5) == 120
    sec = _limiter(monkeypatch, RATE_LIMIT_USER_MAX="1000")
    assert _allowed(sec, 1200, uid=6) == 1000
