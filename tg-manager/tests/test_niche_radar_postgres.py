"""Радар ниши: найти максимум чужих ресурсов и вести в них присутствие.

Замысел владельца (10.11.2026): агент находит по нише КАК МОЖНО БОЛЬШЕ чатов и
каналов, флот вступает и работает там — комментарии, реакции. Прежний Growth
Agent брал пять групп за запуск и ничего о них не помнил.

Что держит файл:
* ключевые слова размножаются (ниша, её слова, гео, хвосты «чат/общение»), но
  не бесконечно и без повторов;
* в каталог идут только пригодные чаты: без @username, забаненные, scam и
  мелкие — мимо, а broadcast отличается от группы;
* оценка полезности выше у групп и у каналов с комментариями, чем у гигантов;
* очередь прохода: ресурс не трогаем чаще кулдауна, у аккаунта потолок
  действий, аккаунты идут по кругу, действует тот, кто вступал;
* на живой базе: каталог копится без дублей (один чат — одна строка у
  владельца), повторный поиск уже известное не возвращает, чужой каталог не
  виден, журнал действий считает сделанное.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from services import niche_radar as nr

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER, STRANGER = 990801, 990802
NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)


# ── Чистая часть ───────────────────────────────────────────────────────────

def test_keywords_multiply_but_stay_sane():
    out = nr.expand_keywords("доставка еды", "Москва", ["фудтех"])
    assert out[0] == "доставка еды" and "фудтех" in out
    assert "доставка еды Москва" in out and "доставка Москва" in out
    assert "доставка еды чат" in out and "еды общение" in out
    assert len(out) == len(set(out)) <= nr.KEYWORDS_MAX
    # Предлоги и короткие слова в запросы не идут.
    assert "на" not in out and "и" not in out
    assert nr.expand_keywords("", "") == []


class _Chat:
    def __init__(self, **kw):
        self.id = kw.get("id", 1)
        self.access_hash = kw.get("access_hash", 7)
        self.title = kw.get("title", "Чат")
        self.username = kw.get("username", "chat_one")
        self.participants_count = kw.get("members", 500)
        self.broadcast = kw.get("broadcast", False)
        self.restricted = kw.get("restricted", False)
        self.scam = kw.get("scam", False)
        self.fake = kw.get("fake", False)
        self.left = kw.get("left", False)


def test_only_usable_resources_get_into_the_catalogue(monkeypatch):
    import sys, types
    mod = types.ModuleType("telethon.tl.types")
    mod.Channel = _Chat
    mod.Chat = _Chat
    monkeypatch.setitem(sys.modules, "telethon.tl.types", mod)
    ok = nr._chat_to_asset(_Chat(id=5, title="Еда", members=900), source="search", keyword="еда")
    assert ok["kind"] == "group" and ok["tg_id"] == 5 and ok["keyword"] == "еда"
    assert nr._chat_to_asset(_Chat(broadcast=True), source="search")["kind"] == "channel"
    for bad in (_Chat(username=""), _Chat(scam=True), _Chat(restricted=True),
                _Chat(fake=True), _Chat(left=True)):
        assert nr._chat_to_asset(bad, source="search") is None
    assert nr._chat_to_asset(object(), source="search") is None


def test_score_prefers_live_resources_over_giants():
    group = {"kind": "group", "members": 5000, "username": "g", "source": "search"}
    giant = {"kind": "group", "members": 500000, "username": "g", "source": "search"}
    chan = {"kind": "channel", "members": 5000, "username": "c", "source": "similar",
            "linked_chat_id": 9}
    mute = {"kind": "channel", "members": 5000, "username": "c", "source": "search"}
    assert nr.score_asset(group) > nr.score_asset(giant)
    assert nr.score_asset(chan) > nr.score_asset(mute), "канал с комментариями полезнее немого"
    assert 0 <= nr.score_asset({}) <= 100
    ranked = nr.rank([giant, group, chan])
    assert ranked[0] is group


def test_dedup_drops_what_we_already_know():
    a = [{"tg_id": 1}, {"tg_id": 1}, {"tg_id": 2}, {"tg_id": 0}]
    assert [x["tg_id"] for x in nr.dedup(a, known={2})] == [1]


def _asset(i, **kw):
    base = {"id": i, "tg_id": 100 + i, "kind": "group", "members": 1000 - i,
            "username": f"c{i}", "source": "search", "score": 50 - i,
            "last_action_at": None, "joined_acc_id": None}
    base.update(kw)
    return base


def test_round_respects_cooldown_account_limit_and_rotation():
    assets = [_asset(1), _asset(2), _asset(3), _asset(4),
              _asset(5, last_action_at=NOW - timedelta(hours=5))]
    # Потолок аккаунта поднят, чтобы отсеивал ТОЛЬКО кулдаун: при per_account=2
    # четвёрку даёт сам потолок, и контроль «выключили кулдаун» остаётся зелёным.
    loose = nr.plan_round(assets, accounts=[10, 11], per_account=5, cooldown_hours=48, now=NOW)
    assert [a["id"] for _, a in loose] == [1, 2, 3, 4], (
        "ресурс, который трогали 5 часов назад, в проход не идёт")
    plan = nr.plan_round(assets, accounts=[10, 11], per_account=2, cooldown_hours=48, now=NOW)
    assert len(plan) == 4
    assert sorted(a for a, _ in plan) == [10, 10, 11, 11], "аккаунты идут по кругу"
    # Действует тот аккаунт, который вступал.
    mine = nr.plan_round([_asset(6, joined_acc_id=11)], accounts=[10, 11], per_account=2,
                         cooldown_hours=48, now=NOW)
    assert mine[0][0] == 11
    assert nr.plan_round(assets, accounts=[], per_account=5, cooldown_hours=0, now=NOW) == []
    # Без кулдауна берём и недавние.
    assert len(nr.plan_round(assets, accounts=[10], per_account=9, cooldown_hours=0, now=NOW)) == 5


@pytest.mark.parametrize("body,word", [
    ({"niche": ""}, "Ниша"),
    ({"niche": "еда", "tone": "zzz"}, "тон"),
    ({"niche": "еда", "promo_link": "http://x.ru"}, "https://"),
    ({"niche": "еда", "min_members": "много"}, "число"),
    ({"niche": "еда", "min_members": -5}, "от 0"),
])
def test_a_bad_campaign_is_refused_in_russian(body, word):
    with pytest.raises(nr.NicheError) as e:
        nr.clean_campaign(body)
    assert word in str(e.value).lower() or word in str(e.value)


def test_a_good_campaign_is_normalised():
    c = nr.clean_campaign({"niche": " Крипта ", "keywords": "биток\nальты, биток",
                           "geo": "Питер"})
    assert c["niche"] == "Крипта" and c["title"] == "Крипта"
    assert c["keywords"] == ["биток", "альты"], "повторы убраны"
    assert c["min_members"] == nr.MIN_MEMBERS_DEFAULT and c["tone"] == "neutral"


# ── Живая база ─────────────────────────────────────────────────────────────

live = pytest.mark.skipif(not DSN, reason="нужен живой Postgres: INFRAGRAM_TEST_DSN")

_SCHEMA = ["CREATE TABLE tg_accounts(id serial PRIMARY KEY, owner_id bigint,"
           " is_active boolean DEFAULT true, session_str text)"]


def _db(name: str) -> str:
    from urllib.parse import urlsplit, urlunsplit
    p = urlsplit(DSN)
    return urlunsplit((p.scheme, p.netloc, "/" + name, p.query, p.fragment))


def _run(body):
    async def go():
        import pathlib

        import asyncpg

        name = "nr_" + uuid.uuid4().hex[:10]
        root = await asyncpg.connect(DSN)
        await root.execute(f'CREATE DATABASE "{name}"')
        pool = None
        try:
            pool = await asyncpg.create_pool(_db(name), min_size=1, max_size=3)
            for ddl in _SCHEMA:
                await pool.execute(ddl)
            mig = pathlib.Path(__file__).resolve().parent.parent / "schema_v319_niche_radar.sql"
            await pool.execute(mig.read_text(encoding="utf-8"))
            return await body(pool)
        finally:
            if pool is not None:
                await pool.close()
            await root.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
            await root.close()

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(go())
    finally:
        loop.close()


def _found(i, **kw):
    a = {"tg_id": 1000 + i, "access_hash": i, "kind": "group", "username": f"chat{i}",
         "title": f"Чат {i}", "members": 500 + i, "source": "search", "keyword": "еда",
         "linked_chat_id": None}
    a.update(kw)
    a["score"] = nr.score_asset(a)
    return a


@live
def test_the_catalogue_grows_without_duplicates_and_is_scoped():
    async def body(pool):
        cid = await nr.save_campaign(pool, OWNER, nr.clean_campaign({"niche": "еда"}))
        other = await nr.save_campaign(pool, STRANGER, nr.clean_campaign({"niche": "еда"}))
        assert await nr.save_assets(pool, OWNER, cid, [_found(1), _found(2)]) == 2
        # Тот же чат вторым заходом (нашли другим словом) — не дубль.
        assert await nr.save_assets(pool, OWNER, cid, [_found(1), _found(3)]) == 1
        assert await nr.known_ids(pool, OWNER) == {1001, 1002, 1003}
        assert await nr.known_ids(pool, STRANGER) == set()
        # Чужой владелец находит тот же чат — это его собственная строка.
        assert await nr.save_assets(pool, STRANGER, other, [_found(1)]) == 1

        lst = await nr.list_assets(pool, OWNER, cid)
        assert lst["total"] == 3 and lst["items"][0]["status_ru"] == "Найден"
        assert lst["items"][0]["source_ru"] == "поиск по слову"
        assert (await nr.list_assets(pool, STRANGER, cid))["total"] == 0

        assert await nr.get_campaign(pool, STRANGER, cid) is None
        assert await nr.get_campaign(pool, OWNER, "мусор") is None
        with pytest.raises(nr.NicheError):
            await nr.save_campaign(pool, STRANGER, nr.clean_campaign({"niche": "x"}), cid)
    _run(body)


@live
def test_stats_and_the_action_log_count_what_was_done():
    async def body(pool):
        cid = await nr.save_campaign(pool, OWNER, nr.clean_campaign({"niche": "еда"}))
        await nr.save_assets(pool, OWNER, cid, [_found(1), _found(2), _found(3)])
        rows = await pool.fetch("SELECT id FROM niche_assets WHERE owner_id=$1 ORDER BY id", OWNER)
        ids = [int(r["id"]) for r in rows]
        await pool.execute("UPDATE niche_assets SET status='joined' WHERE id=$1", ids[0])
        await nr.log_action(pool, OWNER, ids[0], op_id=1, acc_id=5, kind="comment",
                            detail="По теме")
        await nr.log_action(pool, OWNER, ids[0], op_id=1, acc_id=5, kind="reaction")
        await nr.log_action(pool, OWNER, ids[1], op_id=1, acc_id=5, kind="join",
                            status="error", detail="banned")
        s = await nr.campaign_stats(pool, OWNER, cid)
        assert s["found"] == 3 and s["joined"] == 1 and s["actions"] == 2
        assert s["by_kind"]["comment"] == {"ok": 1, "total": 1}
        assert s["by_kind"]["join"] == {"ok": 0, "total": 1}, "ошибка не считается успехом"
        row = await pool.fetchrow("SELECT actions_done, last_action_at FROM niche_assets "
                                  "WHERE id=$1", ids[0])
        assert row["actions_done"] == 2 and row["last_action_at"] is not None
        # Отфильтрованный срез каталога.
        assert (await nr.list_assets(pool, OWNER, cid, status="joined"))["total"] == 1
    _run(body)


@live
def test_a_scan_round_feeds_the_queue_from_the_catalogue():
    async def body(pool):
        cid = await nr.save_campaign(pool, OWNER, nr.clean_campaign({"niche": "еда"}))
        await nr.save_assets(pool, OWNER, cid, [
            _found(i, kind="channel" if i % 2 else "group") for i in range(1, 7)])
        rows = await pool.fetch(
            "SELECT * FROM niche_assets WHERE owner_id=$1 AND campaign_id=$2", OWNER, cid)
        plan = nr.plan_round([dict(r) for r in rows], accounts=[1, 2], per_account=2,
                             cooldown_hours=48, now=NOW)
        assert len(plan) == 4 and len({a["id"] for _, a in plan}) == 4
    _run(body)
