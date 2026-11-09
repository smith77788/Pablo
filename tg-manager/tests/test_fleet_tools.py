"""Массовое управление ресурсами: сортировка, теги, виды, весь срез.

Продукт — быстрое управление сотнями и тысячами аккаунтов, ботов, каналов и
прокси. Требования (документ «Infragram: разрыв в интерфейсе и UX»):
* каждый список сортируется на сервере по всему набору, и страницы по offset
  не теряют и не повторяют строк;
* из запроса в SQL попадает только ключ из белого списка;
* у аккаунта сколько угодно тегов, по тегу фильтруют и запускают массовые
  операции, «весь срез» честно говорит, сколько не вошло сверх потолка;
* виды сохраняются на сервере и принадлежат владельцу;
* экран держит те же ключи сортировки, что и сервер.
"""
from __future__ import annotations

import asyncio
import glob
import json
import os
import re
from pathlib import Path

import pytest

from services import list_sort

ROOT = Path(__file__).resolve().parent.parent
JS = (ROOT / "mini_app" / "screens" / "fleet_tools.js").read_text(encoding="utf-8")
DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER, OTHER = 996201, 996202


def test_screen_and_server_have_the_same_sort_keys():
    for kind, table in list_sort.SORTS.items():
        m = re.search(r"\b" + kind + r":\s*\[(.*?\])\]", JS, re.S)
        assert m, kind
        keys = re.findall(r"\['(\w+)'", m.group(1))
        assert keys == list(table), (kind, keys)


def test_only_whitelisted_keys_reach_sql():
    bad = list_sort.order_by("accounts", "phone; DROP TABLE tg_accounts", "asc")
    assert "DROP" not in bad and bad == list_sort.order_by("accounts", None)
    assert "DROP" not in list_sort.order_by("accounts", "phone", "asc; DROP")
    assert list_sort.order_by("proxies", "latency", "asc").startswith("latency_avg_ms ASC")
    # Стабильный ключ (id) дописывает сам запрос литералом — его проверяет
    # tests/test_paging_does_not_skip_or_repeat_people.py. Здесь — что в
    # выражении нет ничего, кроме белого списка и направления.
    for kind, table in list_sort.SORTS.items():
        for key in table:
            o = list_sort.order_by(kind, key, "desc")
            assert o == table["default"][0] or o == f"{table[key][0]} DESC NULLS LAST", (kind, key)


def test_mass_cap_matches_the_screen():
    from services import mini_app_accounts

    assert f"FLEET_MASS_CAP = {mini_app_accounts.MASS_CAP};" in JS


def test_every_list_has_the_tools_and_range_select():
    html = (ROOT / "mini_app" / "index.html").read_text(encoding="utf-8")
    bots = (ROOT / "mini_app" / "screens" / "bots_list.js").read_text(encoding="utf-8")
    accs = (ROOT / "mini_app" / "screens" / "accounts_list.js").read_text(encoding="utf-8")
    for kind in ("accounts", "bots", "channels", "proxies"):
        assert f'id="fx-{kind}"' in html, kind
    assert '<script src="screens/fleet_tools.js"></script>' in html
    # Shift-диапазон: в каждый переключатель выбора приходит событие клика.
    assert "toggleAccSel(${a.id},event)" in accs
    assert "toggleBotSel(${b.bot_id},event)" in bots
    assert "toggleChSel('${c.channel_id}',event)" in html
    assert "togglePpSel(${p.id},event)" in html
    for fn in ("botSelWholeFilter()", "chSelWholeFilter()", "ppSelWholeFilter()",
               "accSelWholeFilter()"):
        assert fn in html, fn
    assert "fleetSortQS('accounts')" in html and "fleetSortQS('bots')" in bots


# ── Живой Postgres ─────────────────────────────────────────────────────────

_LOOP = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


@pytest.fixture(scope="module")
def pool():
    if not DSN:
        pytest.skip("нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")
    import asyncpg

    async def _boot():
        conn = await asyncpg.connect(DSN)
        files = ["schema.sql"] + sorted(
            glob.glob(str(ROOT / "schema_v*.sql")),
            key=lambda p: int(re.search(r"schema_v(\d+)", p).group(1)))
        for f in files:
            try:
                await conn.execute(open(ROOT / f if not os.path.isabs(f) else f,
                                        encoding="utf-8").read())
            except Exception:
                pass
        from services.mini_app_api import INLINE_MIGRATIONS
        for stmt in INLINE_MIGRATIONS:
            try:
                await conn.execute(stmt)
            except Exception:
                pass
        await conn.close()
        return await asyncpg.create_pool(DSN, min_size=1, max_size=4)

    try:
        p = _run(_boot())
    except Exception as exc:
        pytest.skip(f"Postgres недоступен: {str(exc)[:120]}")
    yield p
    _run(p.close())


async def _wipe(pool):
    for o in (OWNER, OTHER):
        await pool.execute("DELETE FROM saved_views WHERE owner_id=$1", o)
        await pool.execute("DELETE FROM tg_accounts WHERE owner_id=$1", o)
        await pool.execute("DELETE FROM managed_bots WHERE added_by=$1", o)
        await pool.execute("DELETE FROM managed_channels WHERE owner_id=$1", o)
        await pool.execute("DELETE FROM user_proxies WHERE owner_id=$1", o)


@pytest.fixture
def app(pool, monkeypatch):
    from aiohttp import web

    from services import (mini_app_accounts, mini_app_api, mini_app_bots,
                          mini_app_channels, mini_app_proxies, mini_app_saved_views)

    _run(_wipe(pool))
    monkeypatch.setattr(mini_app_api, "_get_uid", lambda r: int(r.headers.get("X-Uid", OWNER)))
    a = web.Application()
    for mod in (mini_app_accounts, mini_app_bots, mini_app_channels, mini_app_proxies,
                mini_app_saved_views):
        mod.setup_routes(a, pool)
    yield a
    _run(_wipe(pool))


def _call(app, method, path, body=None, uid=OWNER):
    from aiohttp.test_utils import make_mocked_request
    from multidict import CIMultiDict

    async def _go():
        base = path.split("?")[0]
        for r in app.router.routes():
            if r.method != method:
                continue
            info = r.resource.get_info()
            pat = info.get("pattern")
            if pat is not None:
                m = pat.fullmatch(base)
                if not m:
                    continue
                mi = m.groupdict()
            elif info.get("path") == base:
                mi = {}
            else:
                continue
            req = make_mocked_request(method, path, match_info=mi,
                                      headers=CIMultiDict({"X-Uid": str(uid)}))

            async def _json():
                return body or {}
            req.json = _json
            resp = await r.handler(req)
            return resp.status, json.loads(resp.text or "null")
        raise AssertionError(f"нет маршрута {method} {path}")
    return _run(_go())


def _accounts(pool, n=6):
    async def _go():
        ids = []
        statuses = ["ok", "banned", "ok", "spamblock", "ok", "session_expired"]
        for i in range(n):
            ids.append(await pool.fetchval(
                "INSERT INTO tg_accounts(owner_id, phone, session_str, is_active, acc_status, "
                "trust_score, first_name, pool) VALUES ($1,$2,'s',TRUE,$3,$4,$5,$6) RETURNING id",
                OWNER, f"+7900000{i:04d}", statuses[i % len(statuses)], (i + 1) / 10,
                f"Имя{i}", "eu" if i % 2 else "ru"))
        return ids
    return _run(_go())


def test_every_account_sort_returns_the_whole_set_once(app, pool):
    ids = set(_accounts(pool))
    for key in list_sort.SORTS["accounts"]:
        for d in ("asc", "desc"):
            seen = []
            for off in range(0, 6, 2):
                st, js = _call(app, "GET", f"/api/miniapp/accounts?limit=2&offset={off}&sort={key}&dir={d}")
                assert st == 200, (key, js)
                seen += [a["id"] for a in js["accounts"]]
            assert sorted(seen) == sorted(ids), (key, d, seen)


def test_problem_accounts_first_and_worst_trust_first(app, pool):
    _accounts(pool)
    _, js = _call(app, "GET", "/api/miniapp/accounts?sort=status")
    order = [a["acc_status"] for a in js["accounts"]]
    assert set(order[:2]) == {"banned", "session_expired"}, order
    _, js = _call(app, "GET", "/api/miniapp/accounts?sort=trust&dir=asc")
    trust = [a["trust_score"] for a in js["accounts"]]
    assert trust == sorted(trust)


def test_tags_filter_and_mass_ops_with_the_whole_slice(app, pool, monkeypatch):
    from services import mini_app_accounts

    ids = _accounts(pool)
    st, js = _call(app, "POST", "/api/miniapp/accounts/mass",
                   {"op": "add_tag", "value": " #Прогрев ", "account_ids": ids[:3]})
    assert st == 200 and js["count"] == 3 and js["tag"] == "прогрев"
    _, js = _call(app, "GET", "/api/miniapp/accounts?tag=прогрев")
    assert sorted(a["id"] for a in js["accounts"]) == sorted(ids[:3])
    assert all(a["tags"] == ["прогрев"] for a in js["accounts"])
    _, js = _call(app, "GET", "/api/miniapp/accounts/tags")
    assert js["tags"] == [{"tag": "прогрев", "count": 3}]
    # Чужой не видит и не трогает.
    _, js = _call(app, "GET", "/api/miniapp/accounts/tags", uid=OTHER)
    assert js["tags"] == []
    st, _ = _call(app, "POST", "/api/miniapp/accounts/mass",
                  {"op": "add_tag", "value": "x", "account_ids": ids}, uid=OTHER)
    assert st == 404
    # Весь срез по тегу с потолком 2: снимается у двух, остаток назван.
    monkeypatch.setattr(mini_app_accounts, "MASS_CAP", 2)
    st, js = _call(app, "POST", "/api/miniapp/accounts/mass",
                   {"op": "remove_tag", "value": "прогрев", "select_all_filtered": True,
                    "tag": "прогрев"})
    assert st == 200 and js["count"] == 2 and js["capped"] == 1, js
    _, js = _call(app, "GET", "/api/miniapp/accounts/tags")
    assert js["tags"] == [{"tag": "прогрев", "count": 1}]


def test_saved_views_belong_to_their_owner(app, pool):
    st, js = _call(app, "POST", "/api/miniapp/views",
                   {"kind": "accounts", "name": "Мёртвые RU",
                    "params": {"filter": "dead", "pool": "ru", "sort": "trust", "dir": "asc"}})
    assert st == 200, js
    vid = js["id"]
    # Тот же вид по имени — обновление, а не дубль.
    _call(app, "POST", "/api/miniapp/views",
          {"kind": "accounts", "name": "Мёртвые RU", "params": {"filter": "dead"}})
    _, js = _call(app, "GET", "/api/miniapp/views?kind=accounts")
    assert [(v["name"], v["params"]) for v in js["views"]] == [("Мёртвые RU", {"filter": "dead"})]
    _, js = _call(app, "GET", "/api/miniapp/views?kind=accounts", uid=OTHER)
    assert js["views"] == []
    st, _ = _call(app, "DELETE", f"/api/miniapp/views/{vid}", uid=OTHER)
    assert st == 404
    st, _ = _call(app, "DELETE", f"/api/miniapp/views/{vid}")
    assert st == 200
    st, _ = _call(app, "POST", "/api/miniapp/views",
                  {"kind": "accounts", "name": "x", "params": {"q": "y" * 5000}})
    assert st == 200   # строки режутся до 100 символов
    st, _ = _call(app, "POST", "/api/miniapp/views", {"kind": "nope", "name": "x", "params": {}})
    assert st == 400


def test_bots_channels_proxies_sort_on_the_server(app, pool):
    async def _seed():
        acc = await pool.fetchval(
            "INSERT INTO tg_accounts(owner_id, phone, session_str, is_active) "
            "VALUES ($1,'+79001112233','s',TRUE) RETURNING id", OWNER)
        for i, name in enumerate(("bbb", "aaa", "ccc")):
            await pool.execute(
                "INSERT INTO managed_bots(bot_id, token, added_by, username, is_active) "
                "VALUES ($1,$2,$3,$4,TRUE)", 9962010 + i, f"t{i}", OWNER, name)
            await pool.execute(
                "INSERT INTO managed_channels(owner_id, acc_id, channel_id, title, members_count) "
                "VALUES ($1,$2,$3,$4,$5)", OWNER, acc, -1009962010 - i, name, 10 * (i + 1))
            await pool.execute(
                "INSERT INTO user_proxies(owner_id, proxy_url, label, is_alive) VALUES ($1,$2,$3,$4)",
                OWNER, f"socks5://10.0.0.{i}:1080", name, i != 1)
    _run(_seed())
    for key in list_sort.SORTS["bots"]:
        _, js = _call(app, "GET", f"/api/miniapp/bots?sort={key}")
        assert len(js["bots"]) == 3, key
    _, js = _call(app, "GET", "/api/miniapp/bots?sort=name&dir=asc")
    assert [b["username"] for b in js["bots"]] == ["aaa", "bbb", "ccc"]
    for key in list_sort.SORTS["channels"]:
        _, js = _call(app, "GET", f"/api/miniapp/channels?sort={key}")
        assert len(js["channels"]) == 3, key
    _, js = _call(app, "GET", "/api/miniapp/channels?sort=title&dir=desc")
    assert [c["title"] for c in js["channels"]] == ["ccc", "bbb", "aaa"]
    for key in list_sort.SORTS["proxies"]:
        _, js = _call(app, "GET", f"/api/miniapp/proxies?sort={key}")
        assert len(js["proxies"]) == 3 and "acc_count" in js["proxies"][0], key
    _, js = _call(app, "GET", "/api/miniapp/proxies?sort=alive")
    assert js["proxies"][0]["label"] == "aaa"   # мёртвый — первым


def test_proxies_bulk_keeps_assigned_ones(app, pool):
    async def _seed():
        p1 = await pool.fetchval("INSERT INTO user_proxies(owner_id, proxy_url) VALUES ($1,'socks5://1.1.1.1:1') RETURNING id", OWNER)
        p2 = await pool.fetchval("INSERT INTO user_proxies(owner_id, proxy_url) VALUES ($1,'socks5://1.1.1.2:1') RETURNING id", OWNER)
        await pool.execute(
            "INSERT INTO tg_accounts(owner_id, phone, session_str, proxy_id) VALUES ($1,'+79005550000','s',$2)",
            OWNER, p2)
        return p1, p2
    p1, p2 = _run(_seed())
    st, js = _call(app, "POST", "/api/miniapp/proxies/bulk", {"op": "backup_on", "proxy_ids": [p1, p2]})
    assert st == 200 and js["count"] == 2
    st, js = _call(app, "POST", "/api/miniapp/proxies/bulk", {"op": "delete", "proxy_ids": [p1, p2]})
    assert js == {"ok": True, "count": 1, "skipped_assigned": 1}, js
    left = _run(pool.fetch("SELECT id FROM user_proxies WHERE owner_id=$1", OWNER))
    assert [r["id"] for r in left] == [p2]
    st, _ = _call(app, "POST", "/api/miniapp/proxies/bulk", {"op": "delete", "proxy_ids": [p2]}, uid=OTHER)
    assert _run(pool.fetchval("SELECT COUNT(*) FROM user_proxies WHERE id=$1", p2)) == 1
