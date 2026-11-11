"""План роста: цель, честный прогресс и рычаги на своих данных.

Прежний «План роста» делил введённое число по вшитым долям 50/20/20/10 и
ничего не сохранял (владелец: «абсолютно бесполезный модуль», 09.11.2026).
Что держит этот файл:

* прогресс: набрано, осталось, нужный и фактический темп, отставание от
  прямой «от старта к цели», прогноз даты, состояние (по плану / отстаём /
  опережаем / стоит / срок вышел / достигнута) и вердикт по-русски;
* рычаги считаются из того, что ЕСТЬ: без аккаунтов инвайт «не готов» и
  говорит, чего не хватает; в бота инвайт не предлагается вовсе; оценка
  самопиара — вилка от числа людей, которых ещё нет в продвигаемом боте;
* накрутка в цель не считается;
* «сегодня» — не больше трёх дел, готовые рычаги с оценкой вперёд;
* на живой базе: цель создаётся с базой «сколько сейчас», вторая активная
  цель тому же каналу не ставится, чужой канал/цель — «не найдено»,
  достигнутая цель закрывается сама, ряд бота собирается по дням прихода.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest

from services import growth_plan as gp

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER, STRANGER = 990601, 990602
BOT_A, BOT_B, ALIEN_BOT = 8101, 8102, 8109
CH, ALIEN_CH = -1008101, -1008109
D0 = date(2026, 11, 1)


# ── Чистая часть ───────────────────────────────────────────────────────────

def _p(current, today, series=None, baseline=1000, target=2000, days=20):
    return gp.progress(baseline=baseline, target=target, current=current, started_on=D0,
                       deadline=D0 + timedelta(days=days), today=today, series=series)


def test_progress_knows_where_we_are_against_the_straight_line():
    day10 = D0 + timedelta(days=10)
    p = _p(1500, day10)
    assert (p["gained"], p["remaining"], p["pct"], p["expected"], p["lag"]) == (500, 500, 50, 500, 0)
    assert p["state"] == "on_track" and p["need_per_day"] == 50 and p["days_left"] == 10
    assert p["pace"] == 50.0 and p["pace_is_recent"] is False
    assert p["forecast"] == (day10 + timedelta(days=10)).isoformat()

    behind = _p(1200, day10)
    assert behind["state"] == "behind" and behind["lag"] == 300 and behind["need_per_day"] == 80
    assert "Отстаём на 300" in behind["verdict"] and "по 80 в день" in behind["verdict"]
    assert "позже срока" in behind["verdict"]

    ahead = _p(1800, day10)
    assert ahead["state"] == "ahead" and "Опережаем план на 300" in ahead["verdict"]

    done = _p(2100, day10)
    assert done["state"] == "done" and done["remaining"] == 0 and done["pct"] == 100
    assert done["need_per_day"] == 0 and done["forecast"] is None

    late = _p(1700, D0 + timedelta(days=25))
    assert late["state"] == "overdue" and "Срок вышел" in late["verdict"]


def test_recent_pace_wins_over_the_average_and_a_flat_week_is_a_stall():
    day10 = D0 + timedelta(days=10)
    # Начали бодро (+400 за три дня), потом неделю стоим.
    flat = [(D0 + timedelta(days=i), 1400) for i in range(3, 11)]
    p = _p(1400, day10, series=flat)
    assert p["pace"] == 0.0 and p["pace_is_recent"] is True
    assert p["state"] == "stalled" and "прироста нет" in p["verdict"]
    # Одной точки мало — темп не выдумываем.
    assert gp.recent_pace([(day10, 1400)], day10) is None
    # Убыль — тоже не рост.
    falling = [(day10 - timedelta(days=2), 1500), (day10, 1400)]
    assert _p(1400, day10, series=falling)["state"] == "stalled"


def test_after_effect_is_measured_only_when_there_are_points():
    ser = [(D0, 100), (D0 + timedelta(days=1), 110), (D0 + timedelta(days=3), 150)]
    assert gp.after_effect(ser, D0) == 50
    assert gp.after_effect(ser, D0 + timedelta(days=3)) is None, "после — ещё нет точек"
    assert gp.after_effect([], D0) is None


def _ctx(**kw):
    base = {"kind": "channel", "remaining": 800, "days_left": 10, "promo_reach": 2000,
            "promo_campaigns": 0, "promo_bots": 2, "accounts": 5, "daily_capacity": 100,
            "parsed_audience": 3000, "va_enabled": False, "has_username": True,
            "keywords": 0, "last_ops": {}}
    base.update(kw)
    return base


def _by_key(levers):
    return {lv["key"]: lv for lv in levers}


def test_levers_are_built_from_what_the_owner_really_has():
    lv = _by_key(gp.build_levers(_ctx()))
    assert list(lv) == ["self_promo", "invite", "content", "growth_agent", "seo", "boost"]
    assert lv["self_promo"]["ready"] and lv["self_promo"]["estimate"] == (60, 200)
    # Инвайт: 100 в день × 10 дней = 1000 приглашений (аудитории хватает), 30–60%.
    assert lv["invite"]["ready"] and lv["invite"]["estimate"] == (300, 600)
    assert lv["invite"]["action"] == {"kind": "invite", "goal": 800, "days": 10}
    # Аудитории меньше, чем тянет флот, — потолок она.
    small = _by_key(gp.build_levers(_ctx(parsed_audience=200)))
    assert small["invite"]["estimate"] == (60, 120)
    assert not lv["content"]["ready"] and "виртуального администратора" in lv["content"]["missing"]
    # Накрутка и поиск оценки не имеют — и не притворяются.
    assert lv["boost"]["estimate"] is None and "не считаем" in lv["boost"]["estimate_note"]
    assert lv["seo"]["estimate"] is None


def test_a_missing_resource_is_named_not_hidden():
    lv = _by_key(gp.build_levers(_ctx(accounts=0, daily_capacity=0)))
    assert not lv["invite"]["ready"] and "Нет рабочих аккаунтов" in lv["invite"]["missing"]
    assert lv["invite"]["action"] == {"kind": "accounts"} and lv["invite"]["estimate"] is None
    assert not lv["growth_agent"]["ready"]
    lv = _by_key(gp.build_levers(_ctx(parsed_audience=0)))
    assert "парсером" in lv["invite"]["missing"] and lv["invite"]["action"] == {"kind": "parser"}
    lv = _by_key(gp.build_levers(_ctx(daily_capacity=0)))
    assert "прогрев" in lv["invite"]["missing"]
    lv = _by_key(gp.build_levers(_ctx(promo_reach=0, promo_bots=0)))
    assert not lv["self_promo"]["ready"] and "нужен ещё один бот" in lv["self_promo"]["missing"]
    lv = _by_key(gp.build_levers(_ctx(has_username=False)))
    assert any("нет @username" in f for f in lv["seo"]["facts"])


def test_a_bot_goal_has_no_invite_and_no_channel_content_lever():
    lv = _by_key(gp.build_levers(_ctx(kind="bot", promo_reach=500)))
    assert list(lv) == ["self_promo", "growth_agent", "seo", "boost"]
    assert "ещё не в этом боте" in lv["self_promo"]["facts"][0]


def test_coverage_says_honestly_whether_the_levers_are_enough():
    levers = gp.build_levers(_ctx())  # оценки: 60–200 и 300–600
    assert gp.coverage(levers, 300)["text"].startswith("Оценённых рычагов хватает")
    assert "только если всё сработает" in gp.coverage(levers, 700)["text"]
    assert "Нужна ещё аудитория" in gp.coverage(levers, 5000)["text"]
    none = gp.build_levers(_ctx(promo_reach=0, accounts=0, daily_capacity=0))
    assert "с оценкой сейчас нет" in gp.coverage(none, 100)["text"]
    assert gp.coverage(levers, 0)["text"] == ""


def test_today_is_at_most_three_things_biggest_first():
    levers = gp.build_levers(_ctx())
    tasks = gp.today_tasks(_p(1200, D0 + timedelta(days=10)), levers)
    assert len(tasks) == 3
    assert tasks[0]["action"]["kind"] == "invite" and "от +300 до +600" in tasks[0]["text"]
    assert tasks[1]["action"]["kind"] == "self_promo"
    assert tasks[2]["action"]["kind"] == "va"
    done = gp.today_tasks(_p(2100, D0 + timedelta(days=10)), levers)
    assert done == [{"text": "Цель достигнута — поставьте следующую.", "action": {"kind": "new_goal"}}]
    late = gp.today_tasks(_p(1700, D0 + timedelta(days=25)), levers)
    assert late[0]["action"]["kind"] == "edit_goal"


@pytest.mark.parametrize("body,word", [
    ({"target": 900, "days": 30}, "больше, чем сейчас"),
    ({"target": "много", "days": 30}, "числами"),
    ({"target": 2000, "days": 0}, "от 1 до"),
    ({"target": 10 ** 9, "days": 30}, "не больше"),
    ({"target": True, "days": 30}, "числами"),
])
def test_a_bad_goal_is_refused_in_russian(body, word):
    with pytest.raises(gp.GoalError) as e:
        gp.clean_goal(body, current=1000)
    assert word in str(e.value)


# ── Живая база ─────────────────────────────────────────────────────────────

live = pytest.mark.skipif(not DSN, reason="нужен живой Postgres: INFRAGRAM_TEST_DSN")

_SCHEMA = [
    "CREATE TABLE managed_bots(bot_id bigint PRIMARY KEY, token text, username text, first_name text,"
    " added_by bigint, is_active boolean DEFAULT true)",
    "CREATE TABLE bot_users(id serial PRIMARY KEY, bot_id bigint NOT NULL, user_id bigint NOT NULL,"
    " first_seen timestamptz NOT NULL DEFAULT now(), last_seen timestamptz NOT NULL DEFAULT now(),"
    " is_active boolean NOT NULL DEFAULT true, is_blocked boolean NOT NULL DEFAULT false,"
    " suspect boolean NOT NULL DEFAULT false, UNIQUE(bot_id, user_id))",
    "CREATE TABLE managed_channels(id serial PRIMARY KEY, owner_id bigint NOT NULL,"
    " channel_id bigint NOT NULL, title text, username text, members_count int DEFAULT 0)",
    "CREATE TABLE channel_member_history(id bigserial PRIMARY KEY, owner_id bigint, channel_id bigint,"
    " members_count int, captured_on date DEFAULT CURRENT_DATE)",
    "CREATE TABLE self_promo_campaigns(id bigserial PRIMARY KEY, owner_id bigint, target_kind text,"
    " target_ref bigint, status text DEFAULT 'active')",
    "CREATE TABLE tg_accounts(id serial PRIMARY KEY, owner_id bigint, is_active boolean DEFAULT true,"
    " session_str text)",
    "CREATE TABLE parsed_audiences(id bigserial PRIMARY KEY, owner_id bigint, tg_user_id bigint,"
    " is_bot boolean DEFAULT false)",
    "CREATE TABLE va_channel_admin(id bigserial PRIMARY KEY, owner_id bigint, channel_id bigint,"
    " enabled boolean DEFAULT false)",
    "CREATE TABLE tracked_keywords(id serial PRIMARY KEY, owner_id bigint, is_active boolean DEFAULT true)",
    "CREATE TABLE operation_queue(id bigserial PRIMARY KEY, owner_id bigint, op_type text, label text,"
    " status text, done_items int DEFAULT 0, total_items int DEFAULT 0, params jsonb DEFAULT '{}',"
    " created_at timestamptz DEFAULT now())",
]


def _db(name: str) -> str:
    from urllib.parse import urlsplit, urlunsplit
    p = urlsplit(DSN)
    return urlunsplit((p.scheme, p.netloc, "/" + name, p.query, p.fragment))


def _run(body):
    async def go():
        import pathlib

        import asyncpg

        from services import flood_engine

        name = "gp_" + uuid.uuid4().hex[:10]
        root = await asyncpg.connect(DSN)
        await root.execute(f'CREATE DATABASE "{name}"')
        pool = None
        orig = flood_engine.recommended_daily_limit

        async def fake_limit(pool_, account_id):
            return {"limit": 40}

        flood_engine.recommended_daily_limit = fake_limit
        try:
            pool = await asyncpg.create_pool(_db(name), min_size=1, max_size=3)
            for ddl in _SCHEMA:
                await pool.execute(ddl)
            mig = pathlib.Path(__file__).resolve().parent.parent / "schema_v318_growth_asset_goals.sql"
            await pool.execute(mig.read_text(encoding="utf-8"))
            await pool.executemany(
                "INSERT INTO managed_bots VALUES($1,$2,$3,$4,$5,$6)",
                [(BOT_A, "1:a", "shop_bot", "Магазин", OWNER, True),
                 (BOT_B, "2:b", "news_bot", "Новости", OWNER, True),
                 (ALIEN_BOT, "9:z", "alien_bot", "Чужой", STRANGER, True)])
            await pool.executemany(
                "INSERT INTO managed_channels(owner_id, channel_id, title, username, members_count)"
                " VALUES($1,$2,$3,$4,$5)",
                [(OWNER, CH, "Наш канал", "our_news", 1200), (STRANGER, ALIEN_CH, "Чужой", "alien", 50)])
            return await body(pool)
        finally:
            flood_engine.recommended_daily_limit = orig
            if pool is not None:
                await pool.close()
            await root.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
            await root.close()

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(go())
    finally:
        loop.close()


@live
def test_a_goal_is_stored_scoped_and_one_per_asset():
    async def body(pool):
        gid = await gp.create_goal(pool, OWNER, {"asset_kind": "channel", "asset_id": CH,
                                                 "target": 2000, "days": 20}, today=D0)
        g = await gp.get_goal(pool, OWNER, gid)
        assert (g["baseline"], g["target"], g["started_on"], g["deadline"]) == (
            1200, 2000, D0, D0 + timedelta(days=20))
        with pytest.raises(gp.GoalError) as e:
            await gp.create_goal(pool, OWNER, {"asset_kind": "channel", "asset_id": CH,
                                               "target": 3000, "days": 5}, today=D0)
        assert "уже есть цель" in str(e.value)
        for bad in ({"asset_kind": "channel", "asset_id": ALIEN_CH}, {"asset_kind": "bot", "asset_id": ALIEN_BOT},
                    {"asset_kind": "group", "asset_id": CH}, {"asset_kind": "channel", "asset_id": "x"}):
            with pytest.raises(gp.GoalError):
                await gp.create_goal(pool, OWNER, {**bad, "target": 5000, "days": 10}, today=D0)
        assert await gp.get_goal(pool, STRANGER, gid) is None
        assert await gp.get_goal(pool, OWNER, "мусор") is None
        assert await gp.delete_goal(pool, STRANGER, gid) is False
        assert await gp.list_goals(pool, STRANGER, today=D0) == []

        # Правка срока и цели не двигает старт и базу.
        await gp.update_goal(pool, OWNER, g, {"target": 2500, "days": 40}, today=D0 + timedelta(days=5))
        g = await gp.get_goal(pool, OWNER, gid)
        assert (g["baseline"], g["target"], g["started_on"], g["deadline"]) == (
            1200, 2500, D0, D0 + timedelta(days=45))
        with pytest.raises(gp.GoalError):
            await gp.update_goal(pool, OWNER, g, {"target": 1000}, today=D0)
    _run(body)


@live
def test_the_plan_reads_real_numbers_and_closes_a_reached_goal():
    async def body(pool):
        day = D0 + timedelta(days=10)
        gid = await gp.create_goal(pool, OWNER, {"asset_kind": "channel", "asset_id": CH,
                                                 "target": 2000, "days": 20}, today=D0)
        await pool.executemany(
            "INSERT INTO channel_member_history(owner_id, channel_id, members_count, captured_on)"
            " VALUES($1,$2,$3,$4)",
            [(OWNER, CH, 1200 + 30 * i, D0 + timedelta(days=i)) for i in range(0, 10)]
            + [(STRANGER, CH, 999999, D0 + timedelta(days=9))])
        await pool.execute("UPDATE managed_channels SET members_count=1500 WHERE channel_id=$1", CH)
        # Аудитория самопиара: 3 уникальных человека в двух ботах (один — в обоих).
        await pool.executemany("INSERT INTO bot_users(bot_id, user_id) VALUES($1,$2)",
                               [(BOT_A, 1), (BOT_A, 2), (BOT_B, 2), (BOT_B, 3), (ALIEN_BOT, 9)])
        await pool.executemany("INSERT INTO tg_accounts(owner_id, session_str) VALUES($1,'s')",
                               [(OWNER,), (OWNER,), (STRANGER,)])
        await pool.executemany("INSERT INTO parsed_audiences(owner_id, tg_user_id) VALUES($1,$2)",
                               [(OWNER, i) for i in range(500)] + [(STRANGER, 7)])
        when = datetime(2026, 11, 3, 12, tzinfo=timezone.utc)
        await pool.executemany(
            "INSERT INTO operation_queue(owner_id, op_type, label, status, done_items, total_items,"
            " params, created_at) VALUES($1,$2,$3,'done',40,50,$4::jsonb,$5)",
            [(OWNER, "mass_invite", "Инвайт в канал", "{}", when),
             (OWNER, "run_broadcast", "Самопиар: канал", '{"self_promo_campaign": 1}', when),
             (OWNER, "run_broadcast", "Обычная рассылка", "{}", when),
             (STRANGER, "mass_invite", "Чужой инвайт", "{}", when)])

        goal = await gp.get_goal(pool, OWNER, gid)
        plan = await gp.plan(pool, OWNER, goal, today=day)
        p = plan["progress"]
        assert (p["current"], p["gained"], p["remaining"], p["days_left"]) == (1500, 300, 500, 10)
        assert p["state"] == "behind" and p["pace_is_recent"] is True
        assert plan["series"][-1] == [day.isoformat(), 1500]
        assert all(v < 10000 for _, v in plan["series"]), "в ряд попал чужой снимок"
        lv = {x["key"]: x for x in plan["levers"]}
        assert "3 человека" in lv["self_promo"]["facts"][0]
        # 2 аккаунта × 40 в день × 10 дней = 800, но собрано 500 → потолок 500.
        assert lv["invite"]["estimate"] == (150, 300)
        assert "вместе тянут 80 приглашений" in lv["invite"]["facts"][0]
        log = plan["log"]
        assert [o["what"] for o in log] == ["Инвайтинг", "Самопиар в личку"] or \
               [o["what"] for o in log] == ["Самопиар в личку", "Инвайтинг"]
        assert all(o["after"] == 90 for o in log), "прирост за 3 дня после 3 ноября: 1260 → 1350"
        assert plan["today"] and len(plan["today"]) <= 3

        # Бот: инвайта нет, самопиар считает тех, кого в боте ещё нет.
        bid = await gp.create_goal(pool, OWNER, {"asset_kind": "bot", "asset_id": BOT_A,
                                                 "target": 100, "days": 30}, today=D0)
        bplan = await gp.plan(pool, OWNER, await gp.get_goal(pool, OWNER, bid), today=day)
        keys = [x["key"] for x in bplan["levers"]]
        assert "invite" not in keys and "content" not in keys
        assert "1 человек ещё не в этом боте" in bplan["levers"][0]["facts"][0]
        assert bplan["progress"]["baseline"] == 2

        # Достигнутая цель закрывается сама и остаётся в списке как достигнутая.
        await pool.execute("UPDATE managed_channels SET members_count=2100 WHERE channel_id=$1", CH)
        goals = await gp.list_goals(pool, OWNER, today=day)
        mine = next(g for g in goals if g["id"] == gid)
        assert mine["status"] == "done" and mine["state_ru"] == "Цель достигнута"
        assert (await gp.get_goal(pool, OWNER, gid))["reached_on"] == day
        # Теперь этому каналу можно поставить следующую цель.
        await gp.create_goal(pool, OWNER, {"asset_kind": "channel", "asset_id": CH,
                                           "target": 3000, "days": 30}, today=day)
    _run(body)
