"""Настройки модулей, которые исполнитель умел, а экран не давал задать.

Жалоба владельца: «во многих модулях недостаточно пользовательских настроек».
Сверка параметров, которые читают исполнители `op_worker`, с тем, что вообще
шлёт мини-апп, нашла настоящие умения без двери:

* публикация: картинка к посту по описанию (`media_prompt`, им пользовался
  только виртуальный администратор), «без картинки не публиковать»
  (`require_media`) и «сколько раз» у повтора (`repeat_count`);
* накрутка реакций: разнобой реакций (`emojis`). Экран всегда слал ОДНО эмодзи
  (👍 по умолчанию), поэтому разнобой исполнителя не включался ни разу, хотя
  N аккаунтов с одной реакцией — главный признак накрутки;
* проверка всех аккаунтов: быстрая, без @SpamBot (`check_spambot`);
* bot-to-bot: масштаб после канарейки. Экран умел только 2 бота.

Плюс новое умение: «растянуть накрутку по времени» (`spread_minutes`).

Каждый тест проверяет ПУТЬ: экран кладёт ключ → ручка кладёт его в params
операции → исполнитель его читает.
"""
from __future__ import annotations

import asyncio
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _js_fn(src: str, name: str) -> str:
    """Тело JS-функции верхнего уровня: от объявления до следующего объявления."""
    i = src.index(f"function {name}(")
    m = re.compile(r"\n(?:async )?function |\n// ── ").search(src, i + 1)
    return src[i:m.start() if m else len(src)]


class _Pool:
    async def fetchval(self, *a, **k):
        return 3

    async def fetch(self, sql, *a, **k):
        if "FROM tg_accounts" in sql:
            return [{"id": 11}, {"id": 12}]
        if "managed_channels" in sql:
            return [{"username": "c1", "channel_id": -1001}]
        return []

    async def fetchrow(self, *a, **k):
        return None

    async def execute(self, *a, **k):
        return "OK"


def _post(monkeypatch, path, body, *, ai_image=True):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from services import mini_app_api, operation_bus, va_image

    monkeypatch.setattr(mini_app_api, "_get_uid", lambda request: 777)
    monkeypatch.setattr(va_image, "image_generation_available", lambda: ai_image)
    submitted: list[tuple[str, dict]] = []

    async def _submit(pool, uid, op_type, params, **k):
        submitted.append((op_type, params))
        return 1

    monkeypatch.setattr(operation_bus, "submit", _submit)

    async def _go():
        app = web.Application()
        mini_app_api.setup_routes(app, _Pool())
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            r = await client.post(path, json=body)
            return r.status, await r.json(), submitted
        finally:
            await client.close()

    return asyncio.run(_go())


# ── публикация ────────────────────────────────────────────────────────────────

def test_publish_image_prompt_reaches_the_operation(monkeypatch):
    status, _, sub = _post(monkeypatch, "/api/miniapp/mass_publish", {
        "text": "пост", "media_prompt": "кофейня утром", "require_media": True})
    assert status == 200, sub
    params = sub[0][1]
    assert params["media_prompt"] == "кофейня утром"
    assert params["media_type"] == "photo"
    assert params["require_media"] is True


def test_publish_without_require_keeps_text_fallback(monkeypatch):
    _, _, sub = _post(monkeypatch, "/api/miniapp/mass_publish", {
        "text": "пост", "media_prompt": "кофейня"})
    assert "require_media" not in sub[0][1]


def test_publish_image_without_a_provider_is_refused_not_dropped(monkeypatch):
    # Без провайдера генерация молча вернула бы None, и пост ушёл бы текстом,
    # хотя владелец просил картинку. Отказ — до постановки.
    status, body, sub = _post(monkeypatch, "/api/miniapp/mass_publish", {
        "text": "пост", "media_prompt": "кофейня"}, ai_image=False)
    assert status == 400 and sub == []
    assert "картин" in body.get("error", "").lower()


def test_publish_repeat_count_reaches_the_operation(monkeypatch):
    _, _, sub = _post(monkeypatch, "/api/miniapp/mass_publish", {
        "text": "пост", "repeat_interval_min": 1440, "repeat_count": 7})
    assert sub[0][1]["repeat_count"] == 7


def test_publish_screen_sends_the_new_settings():
    html = _read("mini_app/index.html")
    for key in ("payload.media_prompt", "payload.require_media", "payload.repeat_count"):
        assert key in html, key
    for el in ('id="mpImagePrompt"', 'id="mpImageRequired"', 'id="mpRepeatCount"'):
        assert el in html, el


def test_publish_executor_reads_the_image_prompt():
    src = _read("services/op_worker.py")
    body = src[src.index("async def _exec_mass_publish("):]
    body = body[:body.index("\nasync def ", 10)]
    assert 'params.get("media_prompt")' in body
    assert "require_download(params" in body


# ── накрутка ──────────────────────────────────────────────────────────────────

_REACT = {"type": "reactions", "channel": "@ch", "msg_id": 5}


def test_reactions_without_a_choice_go_mixed(monkeypatch):
    # Было: ручка подставляла 👍, и все аккаунты ставили одно и то же.
    status, _, sub = _post(monkeypatch, "/api/miniapp/boost", dict(_REACT))
    assert status == 200
    params = sub[0][1]
    assert "emoji" not in params and "emojis" not in params, params


def test_reactions_own_set_reaches_the_operation(monkeypatch):
    _, _, sub = _post(monkeypatch, "/api/miniapp/boost",
                      {**_REACT, "emojis": ["🔥", "❤", "🔥", ""]})
    assert sub[0][1]["emojis"] == ["🔥", "❤"]


def test_reactions_single_emoji_still_works(monkeypatch):
    _, _, sub = _post(monkeypatch, "/api/miniapp/boost", {**_REACT, "emoji": "🎉"})
    assert sub[0][1]["emoji"] == "🎉"


@pytest.mark.parametrize("btype,extra", [
    ("views", {"channel": "@ch", "msg_ids": "1,2"}),
    ("reactions", {"channel": "@ch", "msg_id": 5}),
])
def test_spread_reaches_views_and_reactions(monkeypatch, btype, extra):
    _, _, sub = _post(monkeypatch, "/api/miniapp/boost",
                      {"type": btype, **extra, "spread_minutes": 60})
    assert sub[0][1]["spread_minutes"] == 60


def test_spread_outside_the_list_is_ignored(monkeypatch):
    _, _, sub = _post(monkeypatch, "/api/miniapp/boost",
                      {"type": "views", "channel": "@ch", "msg_ids": "1",
                       "spread_minutes": 100000})
    assert "spread_minutes" not in sub[0][1]


def test_boost_screen_sends_the_new_settings():
    html = _read("mini_app/index.html")
    body = _js_fn(html, "submitBoost")
    assert "payload.emojis" in body and "payload.spread_minutes" in body
    # 👍 больше не подставляется молча, когда выбран разнобой.
    assert "payload.emoji=document.getElementById('boostEmoji')" not in body


def test_spread_pause_is_long_and_hears_cancel(monkeypatch):
    from services import op_worker

    calls: list[tuple[str, float]] = []

    async def _cp(pool, op_id, seconds, where=""):
        calls.append(("cancellable", seconds))
        return seconds

    async def _sleep(seconds):
        calls.append(("sleep", seconds))

    monkeypatch.setattr(op_worker, "cancellable_pause", _cp)
    monkeypatch.setattr(op_worker.asyncio, "sleep", _sleep)

    # 60 минут на 11 аккаунтов = 10 промежутков по ~6 минут (±40%).
    asyncio.run(op_worker._boost_gap_pause(None, 1, 3.0, 60, 11, "t"))
    kind, sec = calls[-1]
    assert kind == "cancellable" and 0.6 * 360 <= sec <= 1.4 * 360, calls

    # Без растяжки — прежний короткий темп.
    asyncio.run(op_worker._boost_gap_pause(None, 1, 3.0, 0, 11, "t"))
    assert calls[-1] == ("sleep", 3.0)


@pytest.mark.parametrize("fn", ["_exec_boost_views", "_exec_boost_reactions"])
def test_boost_executors_use_the_spread(fn):
    src = _read("services/op_worker.py")
    body = src[src.index(f"async def {fn}("):]
    body = body[:body.index("\nasync def ", 10)]
    assert "boost_spread_minutes(params)" in body and "_boost_gap_pause(" in body
    # Обычный темп остаётся под губернатором давления флота.
    assert "_governed_delay(" in body


# ── проверка аккаунтов ────────────────────────────────────────────────────────

def test_quick_account_check_skips_spambot(monkeypatch):
    status, body, sub = _post(monkeypatch, "/api/miniapp/accounts/check", {"spambot": False})
    assert status == 200 and sub[0][1]["check_spambot"] is False


def test_account_check_without_a_choice_stays_full(monkeypatch):
    _, _, sub = _post(monkeypatch, "/api/miniapp/accounts/check", {})
    assert sub[0][1]["check_spambot"] is True


def test_account_check_screen_offers_the_choice():
    html = _read("mini_app/index.html")
    body = _js_fn(html, "massCheckAccounts")
    assert "askChoice(" in body and "spambot: mode === 'full'" in body


# ── bot-to-bot ───────────────────────────────────────────────────────────────

def test_bot_mesh_can_scale_past_the_canary():
    js = _read("mini_app/screens/bots_list.js")
    body = _js_fn(js, "enableBotMesh")
    limits = {int(x) for x in re.findall(r"limit:\s*(\d+)", body)}
    assert limits >= {2, 10, 200}, limits
    assert "only_disabled: false" in body
