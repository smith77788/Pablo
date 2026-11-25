"""Прогрев, ниши и посты нарратив-кампании листаются — на живой базе.

РАЗРЫВ. Три списка стояли на жёстком потолке, и каждый по-своему врал о нём:

* прогрев — `LIMIT 50`, причём общее число планов тот же ответ отдавал честно
  (`total`): владелец флота на двести аккаунтов читал «Всего планов 200» над
  списком из пятидесяти строк, и плана за пятидесятым не мог ни открыть, ни
  остановить;
* ниши — `LIMIT 50` по дате, молча: ниша за пятидесятой для экрана не
  существовала;
* посты нарратив-кампании — `LIMIT 50`, и подпись раздела печатала «Посты
  (50)» по длине куска, то есть неверное число как факт.

Живая база, а не заглушка: у всех трёх появились `LIMIT $2 OFFSET $3` и
запрос-счётчик, и у прогрева счёт идёт по условию с джойном к `tg_accounts` —
разъехавшееся условие на заглушке было бы невидимо.
"""
from __future__ import annotations

import asyncio
import json
import os

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="нужен живой Postgres")

OWNER = 995921
STRANGER = 995922


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _clean(pool):
    await pool.execute(
        "DELETE FROM account_warmup_plans WHERE owner_id = ANY($1::bigint[])",
        [OWNER, STRANGER])
    await pool.execute(
        "DELETE FROM tg_accounts WHERE owner_id = ANY($1::bigint[])", [OWNER, STRANGER])
    await pool.execute(
        "DELETE FROM niche_campaigns WHERE owner_id = ANY($1::bigint[])", [OWNER, STRANGER])
    await pool.execute(
        "DELETE FROM narrative_posts WHERE owner_id = ANY($1::bigint[])", [OWNER, STRANGER])
    await pool.execute(
        "DELETE FROM narrative_campaigns WHERE owner_id = ANY($1::bigint[])",
        [OWNER, STRANGER])


async def _seed(pool) -> int:
    await _clean(pool)
    # Прогрев: у плана уникальный индекс по account_id, поэтому каждому плану
    # нужен свой аккаунт — как во флоте.
    for owner in (OWNER, STRANGER):
        n = 7 if owner == OWNER else 1
        for i in range(n):
            acc = await pool.fetchval(
                "INSERT INTO tg_accounts(owner_id, phone, session_str, is_active) "
                "VALUES($1,$2,'s',TRUE) RETURNING id", owner, f"+7999{owner}{i:02d}")
            await pool.execute(
                "INSERT INTO account_warmup_plans(owner_id, account_id, plan_type, "
                "status, target_days) VALUES($1,$2,'standard','active',14)", owner, acc)
        for i in range(n):
            await pool.execute(
                "INSERT INTO niche_campaigns(owner_id, title, niche) VALUES($1,$2,$3)",
                owner, f"Ниша {owner}-{i}", "nisha")
    cid = await pool.fetchval(
        "INSERT INTO narrative_campaigns(owner_id, topic, core_message, status) "
        "VALUES($1,'Тема','Послание','active') RETURNING id", OWNER)
    other = await pool.fetchval(
        "INSERT INTO narrative_campaigns(owner_id, topic, core_message, status) "
        "VALUES($1,'Чужая','Чужое','active') RETURNING id", STRANGER)
    for i in range(7):
        await pool.execute(
            "INSERT INTO narrative_posts(campaign_id, owner_id, channel_username, "
            "angle, content, scheduled_at) "
            "VALUES($1,$2,$3,'news','текст', NOW() + ($4 || ' hours')::interval)",
            cid, OWNER, f"@chan{i}", str(i))
    await pool.execute(
        "INSERT INTO narrative_posts(campaign_id, owner_id, channel_username, "
        "angle, content, scheduled_at) VALUES($1,$2,'@alien','news','чужое', NOW())",
        other, STRANGER)
    return cid


def _ask(monkeypatch, module: str, route: str, query: str, match=None,
         uid: int = OWNER) -> dict:
    import importlib

    import asyncpg
    from aiohttp import web
    from aiohttp.test_utils import make_mocked_request

    from services import mini_app_api

    mod = importlib.import_module(f"services.{module}")
    monkeypatch.setattr(mini_app_api, "_get_uid", lambda r: uid)

    async def _go():
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        try:
            cid = await _seed(pool)
            app = web.Application()
            mod.setup_routes(app, pool)
            handler = next(
                r.handler for r in app.router.routes()
                if getattr(r.resource, "canonical", "") == route)
            info = {}
            if match:
                info = {match: str(cid)}
            path = route
            for k, v in info.items():
                path = path.replace("{" + k + "}", v)
            resp = await handler(
                make_mocked_request("GET", path + query, match_info=info))
            return json.loads(resp.body)
        finally:
            await _clean(pool)
            await pool.close()

    return _run(_go())


_CASES = (
    ("mini_app_warmup", "/api/miniapp/warmup", "plans", None),
    ("mini_app_niche", "/api/miniapp/niche/overview", "campaigns", None),
    ("mini_app_content", "/api/miniapp/narrative/{campaign_id}", "posts", "campaign_id"),
)


@pytest.mark.parametrize("module,route,key,match", _CASES)
def test_the_page_names_the_whole_count(monkeypatch, module, route, key, match):
    d = _ask(monkeypatch, module, route, "?limit=3&offset=0", match)
    assert len(d[key]) == 3, d
    assert d["page"]["total"] == 7, "итог обязан считаться по всей выборке, а не по странице"
    assert d["page"]["has_more"] is True


@pytest.mark.parametrize("module,route,key,match", _CASES)
def test_the_pages_together_give_everything_once(monkeypatch, module, route, key, match):
    seen = []
    for off in (0, 3, 6):
        d = _ask(monkeypatch, module, route, f"?limit=3&offset={off}", match)
        seen += [json.dumps(r, sort_keys=True, default=str) for r in d[key]]
    assert len(seen) == 7, seen
    assert len(set(seen)) == 7, "страницы перекрылись — часть строк видна дважды"
    last = _ask(monkeypatch, module, route, "?limit=3&offset=6", match)
    assert last["page"]["has_more"] is False


@pytest.mark.parametrize("module,route,key,match", _CASES)
def test_another_owner_is_neither_shown_nor_counted(monkeypatch, module, route, key, match):
    d = _ask(monkeypatch, module, route, "?limit=50", match)
    assert d["page"]["total"] == 7
    assert len(d[key]) == 7


def test_the_warmup_screen_no_longer_contradicts_its_own_counter(monkeypatch):
    """`total` над списком и длина списка должны объясняться конвертом страницы."""
    d = _ask(monkeypatch, "mini_app_warmup", "/api/miniapp/warmup", "?limit=3&offset=0")
    assert int(d["total"]) == 7, "общий счётчик экрана считает все планы"
    assert d["page"]["shown"] == 3 and d["page"]["total"] == 7, (
        "экран обязан знать, что показана часть: иначе «Всего планов 7» стоит "
        "над тремя строками без объяснения")
