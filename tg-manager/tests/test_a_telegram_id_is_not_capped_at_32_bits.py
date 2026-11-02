"""Идентификатор Telegram не обрезается 32 битами.

`security.validate_integer` по умолчанию берёт границы 0 … 2**31-1 — это
граница для счётчиков и внутренних номеров строк. Идентификаторы Telegram в
неё не помещаются:

* у групп и каналов id отрицательные (`-1001234567890`);
* у ботов, созданных с 2023 года, id десятизначные (`7012345678` — именно
  такой пример приводит наша же документация API), у многих пользователей тоже.

Такой id молча становился None, и ручка отвечала, будто его не передали:

* экран «Рассылка» мини-аппа (`/api/miniapp/broadcast`, получатели, A/B,
  отложенная) — «Нужны идентификатор бота и текст» для ЛЮБОГО бота из
  фабрики ботов;
* REST API `send_message` / `click_button` / `get_messages` — ни в одну группу
  и ни в один канал отправить было нельзя;
* REST API с переданным `owner_id` больше 2**31 — «owner_id должен быть
  положительным числом».

Теперь такие id разбирает `security.tg_id` (64 бита со знаком, ноль — «не
передан»), а храповик ниже не даёт снова звать `validate_integer` на id из
Telegram без явной верхней границы.
"""
from __future__ import annotations

import ast
import asyncio
import json
import os
import re

import pytest

from services.security import tg_id

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

GROUP = -1001234567890
NEW_BOT = 7012345678
BIG_USER = 7412345678


# ── помощник ──────────────────────────────────────────────────────────────────

def test_tg_id_accepts_real_telegram_ids():
    assert tg_id(GROUP) == GROUP
    assert tg_id(str(NEW_BOT)) == NEW_BOT
    assert tg_id(BIG_USER, positive=True) == BIG_USER


@pytest.mark.parametrize("bad", [None, "", "abc", 0, "0"])
def test_tg_id_rejects_missing(bad):
    assert tg_id(bad) is None


def test_positive_tg_id_rejects_a_group_id():
    assert tg_id(GROUP, positive=True) is None


# ── рассылка из мини-аппа ─────────────────────────────────────────────────────

class _Pool:
    def __init__(self):
        self.asked_bot: list[int] = []

    async def fetchrow(self, sql, *a):
        if "FROM managed_bots" in sql:
            self.asked_bot.append(a[0])
        return None

    async def fetchval(self, *a, **k):
        return 0

    async def fetch(self, *a, **k):
        return []

    async def execute(self, *a, **k):
        return "OK"


def _broadcast(monkeypatch, body):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from services import mini_app_api, mini_app_broadcast

    monkeypatch.setattr(mini_app_api, "_get_uid", lambda request: 777)
    pool = _Pool()

    async def _go():
        app = web.Application()
        mini_app_broadcast.setup_routes(app, pool)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            r = await client.post("/api/miniapp/broadcast", json=body)
            return r.status, await r.json()
        finally:
            await client.close()

    status, data = asyncio.run(_go())
    return status, data, pool


def test_a_new_bot_can_broadcast(monkeypatch):
    status, data, pool = _broadcast(monkeypatch, {"bot_id": NEW_BOT, "text": "привет"})
    # Бота в поддельной базе нет — значит дошли до поиска бота, а не отбились
    # на разборе id как «не передан».
    assert pool.asked_bot == [NEW_BOT], (status, data)
    assert status == 404, (status, data)


# ── REST API ──────────────────────────────────────────────────────────────────

def test_rest_owner_id_above_32_bits(monkeypatch):
    from services import rest_api

    admin = {"admin": True, "owner_id": None}
    assert rest_api.resolve_owner(admin, BIG_USER) == (BIG_USER, None)
    key = {"admin": False, "owner_id": BIG_USER}
    assert rest_api.resolve_owner(key, str(BIG_USER)) == (BIG_USER, None)


def test_rest_send_message_to_a_group(monkeypatch):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from database import db
    from services import api_keys, rest_api
    from tests.test_integrations_api_keys import _KeyPool

    monkeypatch.setattr(rest_api, "ADMIN_SECRET", "служебный", raising=False)
    pool = _KeyPool()
    seen: list[tuple] = []

    async def _acc(pool_, account_id, owner_id):
        seen.append((account_id, owner_id))
        return None   # аккаунта нет → 404, но ПОСЛЕ разбора chat_id

    monkeypatch.setattr(db, "get_account_for_telethon", _acc)

    async def _go():
        k = await api_keys.create_key(pool, BIG_USER, "n8n")
        app = web.Application()
        rest_api.add_routes(app, pool, bot=None)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            r = await client.post(
                "/api/v1/send_message", headers={"X-Api-Key": k["key"]},
                data=json.dumps({"account_id": 17, "chat_id": GROUP, "text": "Добрый день"}))
            return r.status, await r.text()
        finally:
            await client.close()

    status, text = asyncio.run(_go())
    assert seen == [(17, BIG_USER)], f"запрос в группу отбит до поиска аккаунта: {status} {text}"
    assert status == 404


# ── храповик ──────────────────────────────────────────────────────────────────

_TG_KEY = re.compile(
    r"""["'](?:bot_id|chat_id|channel_id|user_id|owner_id|peer_id|tg_id|"""
    r"""telegram_id|tg_user_id|origin_bot)["']""")


def _scan(src: str, where: str) -> list[str]:
    out = []
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return out
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and getattr(node.func, "id", getattr(node.func, "attr", ""))
                == "validate_integer" and node.args):
            continue
        arg = ast.get_source_segment(src, node.args[0]) or ""
        if not _TG_KEY.search(arg):
            continue
        if any(kw.arg == "max_val" for kw in node.keywords) or len(node.args) >= 3:
            continue
        out.append(f"{where}:{node.lineno}: {arg}")
    return out


def _offenders() -> list[str]:
    out = []
    for sub in ("services", "bot"):
        for dirpath, _, files in os.walk(os.path.join(ROOT, sub)):
            for name in files:
                if name.endswith(".py"):
                    path = os.path.join(dirpath, name)
                    with open(path, encoding="utf-8") as f:
                        out += _scan(f.read(), os.path.relpath(path, ROOT))
    return out


def test_no_telegram_id_goes_through_the_32_bit_default():
    bad = _offenders()
    assert not bad, (
        "id из Telegram разбирается validate_integer с границей по умолчанию "
        "(2**31-1): группы и новые боты отбрасываются как «не передан». "
        "Используйте security.tg_id:\n" + "\n".join(bad))


def test_the_ratchet_sees_the_old_form():
    assert _scan('x = validate_integer(body.get("bot_id"), min_val=1)\n', "probe")
    assert _scan('x = sec.validate_integer(data.get("chat_id"))\n', "probe")
    assert not _scan('x = validate_integer(body.get("bot_id"), min_val=1, '
                     'max_val=2**63 - 1)\n', "probe")
    assert not _scan('x = validate_integer(body.get("count"))\n', "probe")
