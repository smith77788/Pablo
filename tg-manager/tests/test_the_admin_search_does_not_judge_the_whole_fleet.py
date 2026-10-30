"""«Ни один аккаунт не админ этого чата» — приговор всему флоту по восьми.

Инвайт в чат требует права `invite_users`, а раздать его может только админ с
«Назначать администраторов». Мини-апп ищет такого аккаунта живьём: подключается
к чату каждым аккаунтом и смотрит права. Вызов дорогой, поэтому за один запрос
проверялись ВОСЕМЬ аккаунтов — с наименьшими id, — а экран и отказ говорили про
ВСЕХ: «Ни один из ваших аккаунтов не админ этого чата. Сделайте один свой
аккаунт админом чата…».

У владельца с двумя сотнями аккаунтов админ в первую восьмёрку попадал только
случайно. Предыдущая правка подняла вперёд аккаунты, к которым канал привязан в
«Моих каналах», — но чужую группу, которую владелец просто вписал в поле, это
не покрывает: там привязки нет, и порядок снова «первые восемь по id». То есть
продукт предлагал сделать админом аккаунт, который админом уже был, а настоящий
промоутер оставался непроверенным и операция не ставилась вовсе.

Поэтому теперь: кандидата можно назвать прямо — отмеченные в списке аккаунты
идут в проверку первыми, — а ответ несёт два числа, `scanned` и `fleet`, и
отказ говорит «проверено 8 из 212», а не судит обо всём флоте.
"""
from __future__ import annotations

import asyncio
import json
import re
from urllib.parse import urlencode

import pytest
from aiohttp import web

from services import mini_app_api as M
from tests.miniapp_source import js_function

UID = 771202
GROUP = "@chuzhaya_gruppa"
CHANNEL_ID = -1001234567890
RIGHTS = "/api/miniapp/invite/rights_check"
GRANT = "/api/miniapp/invite/grant_admin"


class _Pool:
    """Флот из `fleet` аккаунтов; админ чата — аккаунт с id `admin_id`."""

    def __init__(self, fleet: int = 212, admin_id: int = 150) -> None:
        self.fleet = fleet
        self.admin_id = admin_id
        self.queries: list[tuple[str, tuple]] = []

    async def fetch(self, q, *a):
        self.queries.append((q, a))
        if "managed_channels" in q:
            return []  # чужая группа: привязки в «Моих каналах» нет
        if "SELECT a.id" in q and "tg_accounts a" in q:
            picked = list(a[1] or []) if len(a) > 2 else []
            own = list(a[2] or []) if len(a) > 2 else []
            ids = [i for i in picked if 1 <= i <= self.fleet]
            ids += [i for i in own if i not in ids]
            ids += [i for i in range(1, self.fleet + 1) if i not in ids]
            m = re.search(r"LIMIT\s+(\d+)", q)
            ids = ids[: int(m.group(1))] if m else ids
            return [{"id": i, "owner_id": UID, "session_str": f"s{i}",
                     "device_model": None, "system_version": None,
                     "app_version": None, "lang_code": None,
                     "system_lang_code": None, "proxy_id": None,
                     "cf_relay_url": None, "phone": f"+7000{i:04d}",
                     "proxy_url": None} for i in ids]
        return []

    async def fetchval(self, q, *a):
        self.queries.append((q, a))
        if "COUNT(*)" in q:
            return self.fleet
        return None

    async def fetchrow(self, q, *a):
        self.queries.append((q, a))
        return None

    async def execute(self, q, *a):
        self.queries.append((q, a))
        return "OK"


class _Req:
    def __init__(self, body=None, **query) -> None:
        self._body = body
        self.rel_url = type("U", (), {"query": dict(query)})()
        self.headers: dict[str, str] = {}
        self.query = dict(query)
        self.query_string = urlencode(query)
        self.match_info: dict[str, str] = {}

    async def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


@pytest.fixture
def scan(monkeypatch):
    """Живая проверка прав: промоутер — только аккаунт `pool.admin_id`."""
    seen: dict[str, list[int]] = {"ids": []}

    async def _fake(rows, group, **kw):
        seen["ids"] = [int(r["id"]) for r in rows]
        out = []
        for r in rows:
            if int(r["id"]) == _fake.admin_id:
                out.append({"ok": True, "can_promote": True, "creator": True,
                            "can_invite": True, "channel_id": CHANNEL_ID})
            else:
                out.append({"ok": True, "can_promote": False, "can_invite": False})
        return out

    _fake.admin_id = 150
    monkeypatch.setattr(M, "_get_uid", lambda r: UID)
    monkeypatch.setattr(M, "_scan_admin_status", _fake)

    async def _submit(pool, uid, kind, params, **kw):
        _fake.submitted = (kind, params)
        return 9001

    from services import operation_bus
    monkeypatch.setattr(operation_bus, "submit", _submit)
    M._cache.clear()
    _fake.seen = seen
    yield _fake
    M._cache.clear()


def _route(method: str, path: str, pool):
    app = web.Application()
    M.setup_routes(app, pool)
    for route in app.router.routes():
        info = route.resource.get_info() if route.resource else {}
        if route.method == method and (info.get("path") or info.get("formatter")) == path:
            return route.handler
    raise AssertionError(f"роут {method} {path} не зарегистрирован")


def _call(method, path, pool, body=None, **query) -> dict:
    handler = _route(method, path, pool)
    resp = asyncio.run(handler(_Req(body=body, **query)))
    return json.loads(resp.body.decode("utf-8"))


# ── Ответ называет оба числа ────────────────────────────────────────────────

def test_the_check_says_how_many_of_how_many_it_looked_at(scan):
    """Проверено восемь — флот двести двенадцать, и это два разных числа."""
    pool = _Pool(fleet=212)
    d = _call("GET", RIGHTS, pool, group=GROUP)

    assert d["has_promoter"] is False, "админ лежит за восьмёркой — найтись не мог"
    assert d["scanned"] == 8, d
    assert d["fleet"] == 212, (
        "ответ не говорит, сколько аккаунтов всего: экран снова вынесет "
        "приговор всему флоту по горсти проверенных")


def test_the_refusal_does_not_speak_for_accounts_it_never_checked(scan):
    """Отказ называет числа и не утверждает «ни один аккаунт»."""
    pool = _Pool(fleet=212)
    d = _call("POST", GRANT, pool, body={"group": GROUP})

    assert d.get("no_promoter") is True, d
    msg = d["message"]
    assert "8" in msg and "212" in msg, f"отказ без чисел: {msg}"
    assert "Ни один аккаунт" not in msg, (
        "отказ по-прежнему говорит за аккаунты, которых не проверял: "
        f"{msg}")
    assert "отметьте" in msg.lower(), (
        f"отказ не говорит, что делать, когда проверены не все: {msg}")


def test_a_full_fleet_scan_keeps_the_plain_advice(scan):
    """Флот меньше горстки — проверены все, и совет прежний, без «не все»."""
    pool = _Pool(fleet=3)
    d = _call("POST", GRANT, pool, body={"group": GROUP})

    msg = d["message"]
    assert "Проверено аккаунтов: 3" in msg, msg
    assert " из " not in msg, f"нечего делить: проверены все три — {msg}"
    assert "админом в Telegram" in msg, msg


# ── Названный аккаунт проверяется первым ────────────────────────────────────

def test_a_named_account_is_checked_first(scan):
    """Владелец отметил свой аккаунт-админ — промоутер находится."""
    pool = _Pool(fleet=212, admin_id=150)
    d = _call("POST", GRANT, pool, body={"group": GROUP, "account_ids": [150]})

    assert d.get("ok") is True, d
    assert 150 in scan.seen["ids"], "отмеченный аккаунт не попал в проверку"
    assert scan.seen["ids"][0] == 150, (
        f"отмеченный аккаунт проверяется не первым: {scan.seen['ids']}")
    assert d["op_id"] == 9001
    assert scan.submitted[1]["owner_acc_id"] == 150, scan.submitted


def test_the_named_account_also_reaches_the_live_check(scan):
    """Тот же путь у баннера прав: иначе он покажет «нет админа» рядом с «есть»."""
    pool = _Pool(fleet=212, admin_id=150)
    d = _call("GET", RIGHTS, pool, group=GROUP, account_ids="150,7")

    assert d["has_promoter"] is True, d
    assert d["promoter_label"] == "+70000150", d
    assert scan.seen["ids"][:2] == [150, 7], scan.seen["ids"]


def test_a_foreign_account_id_cannot_be_smuggled_in(scan):
    """Отметка — только подсказка порядка: скоуп владельца остаётся в WHERE."""
    pool = _Pool(fleet=212)
    _call("GET", RIGHTS, pool, group=GROUP, account_ids="999999")
    cand = [q for q, _ in pool.queries if "SELECT a.id" in q and "tg_accounts a" in q]
    assert cand, "запрос кандидатов не найден"
    assert "a.owner_id=$1" in cand[0], cand[0]
    assert 999999 not in scan.seen["ids"], (
        "чужой id проехал в проверку: подсказка порядка не вправе расширять выборку")


def test_the_candidate_query_lives_in_one_place():
    """Два обработчика берут кандидатов одним помощником, а не копией SQL."""
    from tests.miniapp_source import py_function

    src = py_function("services/mini_app_invite.py", "setup_routes")
    assert src.count("SELECT a.id, a.owner_id, a.session_str") == 1, (
        "выборка кандидатов снова размножена копипастой: правка порядка в одном "
        "месте разойдётся со вторым")


# ── Экран ───────────────────────────────────────────────────────────────────

def test_the_banner_does_not_judge_accounts_it_never_saw():
    """Баннер прав называет оба числа и не говорит «ни один из ваших»."""
    js = js_function("checkInviteRights")
    assert "d.scanned" in js and "d.fleet" in js, (
        "баннер не читает, сколько проверено и сколько всего")
    assert "Ни один из ваших аккаунтов не админ" not in js, (
        "баннер по-прежнему выносит приговор всему флоту")
    assert "Среди проверенных аккаунтов" in js, js[-400:]
    assert "account_ids=" in js, (
        "отмеченный аккаунт-админ не доезжает до живой проверки")


def test_the_grant_button_sends_the_ticked_accounts():
    js = js_function("massInviteGrantAdmin")
    assert "account_ids" in js, (
        "кнопка «выдать право приглашать» ищет промоутера без подсказки "
        "владельца — снова среди аккаунтов с наименьшими id")
    assert "massInviteAccsWrap" in js, js


# ── Тот же экран, тот же класс: «готовность флота» по выборке ───────────────
#
# Рядом с баннером прав живёт пре-флайт «готовность флота»: он тоже проверяет
# аккаунты живьём и тоже по выборке (шестнадцать), а строка говорила «готовы
# инвайтить X из Y», где Y — размер ВЫБОРКИ. На флоте в две сотни это читалось
# как итог по всему флоту.

class _FleetPool:
    def __init__(self, fleet: int = 212, limit_rows: int = 16) -> None:
        self.fleet = fleet
        self.limit_rows = limit_rows

    async def fetch(self, q, *a):
        return [{"id": i, "phone": f"+7000{i:04d}", "session_str": "s"}
                for i in range(1, self.limit_rows + 1)]

    async def fetchval(self, q, *a):
        return self.fleet


def test_the_preflight_says_the_fleet_is_bigger_than_the_sample():
    from services.invite_preflight import run_preflight

    async def checker(sess, acc, group):
        return {"state": "member", "can_invite": True}

    rep = asyncio.run(run_preflight(_FleetPool(), UID, GROUP,
                                    checker=checker, claim=False))
    assert rep["total"] == 16, rep["total"]
    assert rep["fleet"] == 212, (
        "пре-флайт не говорит, сколько аккаунтов всего: «готовы инвайтить X из "
        "16» выдаёт выборку за весь флот")
    assert rep["ready"] == 16


def test_named_accounts_are_the_whole_answer():
    """Когда аккаунты названы — проверены именно они, делить не на что."""
    from services.invite_preflight import run_preflight

    async def checker(sess, acc, group):
        return {"state": "member", "can_invite": True}

    rep = asyncio.run(run_preflight(_FleetPool(limit_rows=3), UID, GROUP,
                                    account_ids=[1, 2, 3],
                                    checker=checker, claim=False))
    assert rep["fleet"] == rep["total"] == 3, rep


def test_the_readiness_line_names_both_numbers():
    js = js_function("loadFleetReadiness")
    assert "d.fleet" in js, (
        "экран готовности не читает размер флота и снова выдаёт выборку за всех")
    assert "проверенных" in js, js[-500:]
