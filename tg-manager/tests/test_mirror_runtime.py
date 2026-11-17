"""Бот-зеркало — полностью идентичный бот, а не только кнопка «открыть панель».

Апдейты зеркала кормятся в ТОТ ЖЕ Dispatcher через long-poll + feed_update, так
что работают все команды/меню/сценарии. А web-app кнопки, которые хендлеры шлют
из зеркала БЕЗ ?m, принимает /auth перебором токенов зеркал.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import time
import urllib.parse

from services import mirror_bot as mb
from services import mirror_runtime as mr
from services.mini_app_auth import validate_init_data

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(rel):
    return open(os.path.join(ROOT, rel), encoding="utf-8").read()


def _init(token: str, uid: int = 777) -> str:
    user = json.dumps({"id": uid, "username": "o", "first_name": "O"})
    f = {"auth_date": str(int(time.time())), "user": user, "query_id": "AA"}
    dc = "\n".join(f"{k}={v}" for k, v in sorted(f.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    f["hash"] = hmac.new(secret, dc.encode(), hashlib.sha256).hexdigest()
    return urllib.parse.urlencode(f)


MIRROR = "900800:MIRRORtokenZZZZZZZZZZZZZZZZZZZZZZZZZZZZ"
OTHER = "700600:OTHERtokenYYYYYYYYYYYYYYYYYYYYYYYYYYYYYY"


class _Pool:
    def __init__(self, tokens):
        self._tokens = tokens
    async def fetch(self, *a, **k):
        return [{"token_enc": t} for t in self._tokens]


def test_find_user_by_init_data_matches_a_mirror(monkeypatch):
    monkeypatch.setattr(mb, "decrypt_token", lambda e: e)  # token_enc == token
    init = _init(MIRROR, uid=777)
    pool = _Pool([OTHER, MIRROR])
    user = asyncio.run(mb.find_user_by_init_data(pool, init, validate_init_data))
    assert user and user["user_id"] == 777


def test_find_user_by_init_data_none_when_no_mirror_matches(monkeypatch):
    monkeypatch.setattr(mb, "decrypt_token", lambda e: e)
    init = _init("111111:SOMEONEELSEtokenXXXXXXXXXXXXXXXXXXXXXX", uid=1)
    pool = _Pool([OTHER, MIRROR])
    assert asyncio.run(mb.find_user_by_init_data(pool, init, validate_init_data)) is None


def test_runtime_fingerprint_is_stable():
    assert mr._fp(MIRROR) == mr._fp(MIRROR)
    assert mr._fp(MIRROR) != mr._fp(OTHER)


def test_runtime_run_noops_without_dispatcher():
    # Без Dispatcher супервизор просто выходит, не падая.
    asyncio.run(mr.run(object(), None, object()))


def test_auth_falls_back_to_mirror_tokens():
    src = _src("services/mini_app_api.py")
    a = src[src.index("async def auth("):]
    a = a[:a.index("async def mirror_get(")]
    assert "find_user_by_init_data(pool, init_data, validate_init_data)" in a, (
        "auth не пробует токены зеркал, когда основной не подтвердил"
    )


def test_main_spawns_mirror_runtime():
    src = _src("main.py")
    assert "mirror_runtime" in src and "_mirror_rt.run" in src
    # Запущено в обработке обновлений (webhook и polling), а не в общем блоке.
    assert src.count("_mirror_rt.run") >= 2


def test_runtime_feeds_updates_into_the_main_dispatcher():
    src = _src("services/mirror_runtime.py")
    assert "dp.feed_update(bot, upd" in src, "апдейты зеркала не идут в основной Dispatcher"
    assert "get_updates(" in src and "delete_webhook" in src
