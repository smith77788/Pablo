"""Связывание устройства — автономный вход вне Telegram (PWA/Android).

Проверяем сердце: код нормализуется, токен устройства подписан и проверяется,
чужая/битая подпись и просрочка отклоняются, отпечаток стабилен. Плюс обёртки
БД на заглушке: погашение кода атомарно и одноразово, отзыв блокирует обмен.
"""
from __future__ import annotations

import asyncio
import time

from services import device_pairing as D

SECRET = "bot-token-secret-123"


# ── Код связывания ───────────────────────────────────────────────────────────

def test_new_code_shape_and_alphabet():
    c = D.new_code()
    assert len(c) == 9 and c[4] == "-"
    body = c.replace("-", "")
    assert all(ch in "23456789ABCDEFGHJKMNPQRSTUVWXYZ" for ch in body)
    assert "0" not in body and "O" not in body and "1" not in body


def test_normalize_accepts_lowercase_and_spaces():
    c = D.new_code()
    messy = " " + c.replace("-", "").lower() + " "
    assert D.normalize_code(messy) == c


def test_normalize_rejects_wrong_length():
    assert D.normalize_code("ABC") == ""
    assert D.normalize_code("") == ""
    assert D.normalize_code(None) == ""


# ── Токен устройства ─────────────────────────────────────────────────────────

def test_token_roundtrip():
    tok = D.make_device_token(777, SECRET)
    assert tok.startswith("dev:")
    assert D.parse_device_token(tok, SECRET) == 777


def test_token_rejects_wrong_secret():
    tok = D.make_device_token(777, SECRET)
    assert D.parse_device_token(tok, "other-secret") is None


def test_token_rejects_tamper():
    tok = D.make_device_token(777, SECRET)
    # подменяем owner_id, не трогая подпись
    parts = tok.split(":")
    parts[1] = "888"
    assert D.parse_device_token(":".join(parts), SECRET) is None


def test_token_expires():
    old = D.make_device_token(777, SECRET, ts=int(time.time()) - 31 * 86400)
    assert D.parse_device_token(old, SECRET) is None


def test_token_garbage_is_none():
    for bad in ("", "не токен", "dev:1:2:3", "x:1:2:3:4"):
        assert D.parse_device_token(bad, SECRET) is None


def test_different_devices_have_different_fingerprints():
    a = D.make_device_token(777, SECRET)
    b = D.make_device_token(777, SECRET)
    assert a != b                                    # nonce различается
    assert D.token_fingerprint(a) != D.token_fingerprint(b)
    assert D.token_fingerprint(a) == D.token_fingerprint(a)


# ── Обёртки БД на заглушке ───────────────────────────────────────────────────

class _Pool:
    """Заглушка device_pairings: одна строка-код, погашение атомарно."""

    def __init__(self):
        self.rows = []          # [{id, owner_id, code, expired, paired, token_fp, revoked}]
        self._id = 0

    async def execute(self, q, *a):
        if "INSERT INTO device_pairings" in q:
            self._id += 1
            self.rows.append({"id": self._id, "owner_id": a[0], "code": a[1],
                              "expired": False, "paired": False,
                              "token_fp": None, "revoked": False})
        elif "SET code=NULL" in q and "paired_at IS NULL" in q and "owner_id=$1" in q:
            for r in self.rows:
                if r["owner_id"] == a[0] and r["code"] and not r["paired"]:
                    r["code"] = None
        elif "SET token_fp=$2" in q:
            for r in self.rows:
                if r["id"] == a[0]:
                    r["token_fp"] = a[1]
        elif "last_seen_at=now()" in q:
            pass
        return "OK"

    async def fetchrow(self, q, *a):
        if "UPDATE device_pairings" in q and "RETURNING owner_id, id" in q:
            for r in self.rows:
                if r["code"] == a[0] and not r["expired"] and not r["paired"]:
                    r["code"] = None
                    r["paired"] = True
                    return {"owner_id": r["owner_id"], "id": r["id"]}
            return None
        if "revoked_at IS NULL LIMIT 1" in q:          # device_active
            for r in self.rows:
                if r["token_fp"] == a[0] and not r["revoked"]:
                    return {"?column?": 1}
            return None
        if "SET revoked_at=now()" in q:                # revoke
            for r in self.rows:
                if r["id"] == a[0] and r["owner_id"] == a[1] and not r["revoked"]:
                    r["revoked"] = True
                    return {"id": r["id"]}
            return None
        return None

    async def fetch(self, q, *a):
        return []


def _redeem_flow(pool, code, owner_expected):
    red = asyncio.run(pool_redeem(pool, code))
    return red


async def pool_redeem(pool, code):
    return await D.redeem_code(pool, code)


def test_redeem_is_one_shot():
    pool = _Pool()
    code = asyncio.run(D.create_pairing_code(pool, 42))
    first = asyncio.run(D.redeem_code(pool, code))
    assert first and first["owner_id"] == 42
    # второй обмен того же кода — уже нет
    assert asyncio.run(D.redeem_code(pool, code)) is None


def test_creating_new_code_voids_previous():
    pool = _Pool()
    c1 = asyncio.run(D.create_pairing_code(pool, 42))
    asyncio.run(D.create_pairing_code(pool, 42))       # гасит c1
    assert asyncio.run(D.redeem_code(pool, c1)) is None


def test_attach_fp_then_device_active_then_revoke():
    pool = _Pool()
    code = asyncio.run(D.create_pairing_code(pool, 42))
    red = asyncio.run(D.redeem_code(pool, code))
    tok = D.make_device_token(red["owner_id"], SECRET)
    fp = D.token_fingerprint(tok)
    asyncio.run(D.attach_token_fp(pool, red["id"], fp))
    assert asyncio.run(D.device_active(pool, fp)) is True
    assert asyncio.run(D.revoke_device(pool, 42, red["id"])) is True
    assert asyncio.run(D.device_active(pool, fp)) is False


def test_revoke_wrong_owner_fails():
    pool = _Pool()
    code = asyncio.run(D.create_pairing_code(pool, 42))
    red = asyncio.run(D.redeem_code(pool, code))
    assert asyncio.run(D.revoke_device(pool, 999, red["id"])) is False
