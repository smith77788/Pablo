"""Виртуальный администратор канала по НАСТОЯЩЕМУ Postgres: весь путь от установки
на ПУСТОЙ канал до публикации, статистики, черновиков и суточного отчёта.

Заглушены только внешние миры: Telegram (снимок канала) и ИИ (ответы модели).
Всё SQL — настоящее: заглушка пула не ловит ошибок связывания (str вместо
datetime, dict вместо jsonb), а у администратора их поле — десятки запросов.

Запуск — как у tests/test_invite_e2e_postgres.py (см. его docstring): поднять
Postgres 16 и задать INFRAGRAM_TEST_DSN. Без переменной файл пропускается.
"""
from __future__ import annotations

import asyncio
import glob
import json
import os
import re
from datetime import datetime, timedelta, timezone

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(
    not DSN, reason="нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")

OWNER = 990777
CID = 1777001
_LOOP = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


class _Bot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text, kw))


class _AI:
    """Ответы модели по типу запроса; счётчик делает посты разными."""

    def __init__(self):
        self.n = 0
        self.prompts = []

    async def __call__(self, system, user):
        from services import channel_admin as ca
        self.prompts.append((system, user))
        if system == ca._SYSTEM_PROFILE:
            return json.dumps({
                "topic": "Доставка мебели по Москве за один день",
                "audience": "семьи и офисы, которые переезжают или обновляют мебель",
                "tone": "спокойный, уверенный, по делу",
                "niche": "доставка и сборка мебели", "offer": "доставка за день и сборка",
                "usp": "свои грузчики, фиксированная цена", "geo": "Москва",
                "pains": ["поцарапают мебель", "опоздают"], "objections": ["дорого"],
                "triggers": ["переезд", "покупка шкафа"],
                "pillars": [{"name": "Полезные советы", "weight": 3},
                            {"name": "Как мы работаем", "weight": 2},
                            {"name": "Вопрос подписчикам", "weight": 1},
                            {"name": "Предложение недели", "weight": 1}],
            }, ensure_ascii=False)
        if system == ca._SYSTEM_PLAN:
            n = user.count("\n") + 1
            return json.dumps([f"Тема номер {i}" for i in range(n)], ensure_ascii=False)
        self.n += 1
        words = ["шкаф", "диван", "кухня", "стол", "кровать", "комод", "полка", "кресло"]
        w = words[self.n % len(words)]
        return (f"Вариант {self.n}: как перевезти {w} без единой царапины. "
                f"Упаковываем {w} в три слоя, снимаем фасады, крепим в кузове ремнями. "
                f"Номер {self.n * 7919} — уникальная деталь, чтобы тексты не совпадали. "
                + "Подробности " * (self.n % 3))


@pytest.fixture(scope="module")
def pool():
    import asyncpg

    async def _boot():
        conn = await asyncpg.connect(DSN)
        files = ["schema.sql"] + sorted(
            glob.glob("schema_v*.sql"),
            key=lambda p: int(re.search(r"schema_v(\d+)", p).group(1)))
        for f in files:
            try:
                await conn.execute(open(f, encoding="utf-8").read())
            except Exception:
                pass
        await conn.close()
        return await asyncpg.create_pool(DSN, min_size=1, max_size=4)

    try:
        p = _run(_boot())
    except Exception as exc:
        pytest.skip(f"Postgres недоступен: {str(exc)[:120]}")

    async def _seed():
        for t in ("va_channel_admin", "va_admin_drafts", "va_admin_plan", "va_admin_events",
                  "va_channel_stats", "va_channel_brain", "va_channel_posts",
                  "operation_queue", "managed_channels"):
            await p.execute(f"DELETE FROM {t} WHERE owner_id=$1", OWNER)
        await p.execute("DELETE FROM tg_accounts WHERE owner_id=$1", OWNER)
        acc = await p.fetchval(
            "INSERT INTO tg_accounts(owner_id,phone,session_str,is_active,acc_status,first_name) "
            "VALUES($1,'+79907770001','sess',TRUE,'active','Админ') RETURNING id", OWNER)
        await p.execute(
            "INSERT INTO managed_channels(owner_id, acc_id, channel_id, title, username) "
            "VALUES($1,$2,$3,'Мебель Экспресс','mebel_express')", OWNER, acc, CID)
    _run(_seed())
    yield p
    _run(p.close())


@pytest.fixture()
def stubs(monkeypatch):
    from services import account_manager, operation_bus

    snap = {"title": "Мебель Экспресс", "username": "mebel_express", "about": "",
            "members_count": 0, "recent": [], "by_id": {}}

    async def _snapshot(*a, **k):
        out = dict(snap)
        out["by_id"] = {i: {"views": 100 + i, "forwards": 1, "reactions": 2}
                        for i in (k.get("msg_ids") or [])}
        return out

    async def _no_plan(*a, **k):
        return None

    monkeypatch.setattr(account_manager, "read_channel_snapshot", _snapshot)
    monkeypatch.setattr(operation_bus, "_enforce_min_plan", _no_plan)
    return snap


def test_empty_channel_is_administered_end_to_end(pool, stubs):
    from services import channel_admin as ca
    from services import channel_admin_runner as runner
    from services import channel_brain_store as store

    ai, bot = _AI(), _Bot()

    async def go():
        # 1. Установка на пустой канал: владелец не написал ничего, кроме контакта.
        await ca.install(pool, OWNER, CID, {"lead_contact": "@mebel_manager"})
        await ca.setup_channel(pool, OWNER, CID, complete=ai)
        a = await ca.get_admin(pool, OWNER, CID)
        assert a["enabled"] and a["setup_done"]
        assert a["topic"].startswith("Доставка мебели")
        assert a["intro_pending"] is True, "пустой канал должен начаться со знакомства"
        brief = a["brief"] if isinstance(a["brief"], dict) else json.loads(a["brief"])
        assert brief["niche"] and brief["pains"]
        brain = await store.get_profile(pool, OWNER, str(CID))
        assert "Полезные советы" in brain.pillars
        plan = await ca.get_plan(pool, OWNER, CID)
        assert len(plan) >= 7 and plan[0]["pillar"] == ca.INTRO_PILLAR
        assert a["next_post_at"] is not None

        # 2. Такт по расписанию: пишет знакомство и ставит публикацию на один канал.
        now = datetime.now(timezone.utc) + timedelta(minutes=15)
        res = await ca.tick_post(pool, bot, await ca.get_admin(pool, OWNER, CID),
                                 complete=ai, now=now)
        assert res == "published"
        op = await pool.fetchrow(
            "SELECT op_type, params FROM operation_queue WHERE owner_id=$1 ORDER BY id DESC LIMIT 1",
            OWNER)
        params = json.loads(op["params"]) if isinstance(op["params"], str) else op["params"]
        assert op["op_type"] == "mass_publish" and params["channel_ids"] == [CID]
        assert params["pillar"] == ca.INTRO_PILLAR
        assert "ПЕРВЫЙ пост" in ai.prompts[-1][1]
        assert "@mebel_manager" in ai.prompts[-1][1]
        a = await ca.get_admin(pool, OWNER, CID)
        assert a["intro_pending"] is False and a["last_op_id"]

        # 3. Публикация провалилась в исполнителе → администратор сам это видит.
        await pool.execute("UPDATE operation_queue SET status='failed', error_msg=$2 WHERE id=$1",
                           a["last_op_id"], "Нет прав для публикации")
        await ca.check_last_publish(pool, bot, a)
        a = await ca.get_admin(pool, OWNER, CID)
        assert a["fail_streak"] == 1 and "Нет прав" in a["last_error"]

        # 4. Исполнитель записал пост с msg_id → статистика и подстройка рубрик.
        for i, pillar in enumerate(["Полезные советы"] * 3 + ["Вопрос подписчикам"] * 3):
            await pool.execute(
                "INSERT INTO va_channel_posts(owner_id, channel_key, pillar, body, msg_id) "
                "VALUES($1,$2,$3,$4,$5)", OWNER, str(CID), pillar, f"пост {i}", 10 + i * 50)
        got = await ca.collect_stats(pool, OWNER, CID)
        assert got["posts"] == 6
        tuned = await ca.autotune(pool, OWNER, CID)
        assert tuned and tuned["Вопрос подписчикам"] > 1.0, "рубрика с лучшим откликом должна вырасти"
        rep = await ca.channel_report(pool, OWNER, CID)
        assert rep["best_pillar"] == "Вопрос подписчикам" and rep["posts_7d"] >= 6

        # 5. Режим «на одобрение»: черновик уходит владельцу с кнопками.
        await ca.save_settings(pool, OWNER, CID, {"publish_mode": "review"})
        res = await ca.post_now(pool, bot, OWNER, CID, complete=ai)
        assert res == "draft"
        assert bot.sent and bot.sent[-1][2].get("reply_markup") is not None
        drafts = await ca.list_drafts(pool, OWNER, CID)
        assert len(drafts) == 1
        new = await ca.regenerate_draft(pool, OWNER, drafts[0]["id"], complete=ai)
        assert new["id"] != drafts[0]["id"]
        out = await ca.publish_draft(pool, OWNER, new["id"])
        assert out["op_id"]
        with pytest.raises(ca.ChannelAdminError):
            await ca.publish_draft(pool, OWNER, new["id"])  # второй тап не публикует дважды

        # 6. Экранные выборки и фоновый цикл целиком — без падений.
        chans = await ca.list_channels(pool, OWNER)
        assert chans and chans[0]["enabled"] and chans[0]["channel_id"] == str(CID)
        assert await ca.events(pool, OWNER, CID)
        await pool.execute("UPDATE va_channel_admin SET last_post_at=now(), last_report_at=NULL "
                           "WHERE owner_id=$1", OWNER)
        await runner.run_once(pool, bot)
        assert any("Отчёт виртуального администратора" in t for _, t, _ in bot.sent)

    _run(go())


def test_reconfigure_and_disable(pool, stubs):
    from services import channel_admin as ca

    async def go():
        if not await ca.get_admin(pool, OWNER, CID):
            await ca.install(pool, OWNER, CID, {})
        await ca.reconfigure(pool, OWNER, CID)
        a = await ca.get_admin(pool, OWNER, CID)
        assert a["setup_done"] is False and a["topic"] == ""
        assert await ca.get_plan(pool, OWNER, CID) == []
        await ca.save_settings(pool, OWNER, CID, {"enabled": False})
        assert (await ca.get_admin(pool, OWNER, CID))["enabled"] is False
        with pytest.raises(ca.ChannelAdminError):
            await ca.save_settings(pool, OWNER, 424242, {"enabled": True})  # чужой канал

    _run(go())


def test_miniapp_routes_over_real_db(pool, stubs, monkeypatch):
    """Экран Mini App: список, карточка, сохранение, правила канала — по живой БД."""
    from aiohttp import ClientSession, web
    from services import mini_app_api as M
    from services import security as _sec

    monkeypatch.setattr(M, "_get_uid", lambda request: OWNER)

    async def call(method, path, payload=None):
        _sec._rate_limiter._requests.clear()
        app = web.Application()
        M.setup_routes(app, pool)
        r = web.AppRunner(app)
        await r.setup()
        site = web.TCPSite(r, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            async with ClientSession() as s:
                kw = {"data": json.dumps(payload), "headers": {"Content-Type": "application/json"}} \
                    if payload is not None else {}
                async with s.request(method, f"http://127.0.0.1:{port}{path}", **kw) as resp:
                    return resp.status, await resp.json()
        finally:
            await r.cleanup()

    async def go():
        st, d = await call("GET", "/api/miniapp/va/channels")
        assert st == 200 and any(c["channel_id"] == str(CID) for c in d["channels"])
        st, d = await call("POST", f"/api/miniapp/va/channel/{CID}/install",
                           {"project_info": "Доставка мебели", "posts_per_day": 3})
        assert st == 200 and d["settings"]["enabled"] and d["settings"]["posts_per_day"] == 3
        st, d = await call("PUT", f"/api/miniapp/va/channel/{CID}",
                           {"tone": "дружелюбный", "pillars": "Советы: 3\nАкции: 1",
                            "window_start": 10, "window_end": 20})
        assert st == 200 and d["settings"]["tone"] == "дружелюбный"
        assert [p["name"] for p in d["pillars"]] == ["Советы", "Акции"]
        biz = {"goal": "sales", "products": "Диван — 30 000 ₽", "sales_share": 20, "address": "vy"}
        st, d = await call("PUT", f"/api/miniapp/va/channel/{CID}", {"business": biz})
        assert st == 200 and d["settings"]["business"] == biz
        st, d = await call("PUT", f"/api/miniapp/va/channel/{CID}", {"tone": "строгий"})
        assert st == 200 and d["settings"]["business"] == biz  # другие поля бизнес не затирают
        st, _ = await call("PUT", f"/api/miniapp/va/channel/{CID}", {"business": {"goal": "деньги"}})
        assert st == 400
        st, d = await call("PUT", f"/api/miniapp/va/channel/{CID}",
                           {"window_start": 20, "window_end": 10})
        assert st == 400
        st, d = await call("PUT", f"/api/miniapp/editorial/policy?channel={CID}",
                           {"forbidden_words": ["дёшево"], "max_emoji": 2})
        assert st == 200 and d["policy"]["forbidden_words"] == ["дёшево"]
        st, d = await call("GET", f"/api/miniapp/editorial/policy?channel={CID}")
        assert d["policy"]["max_emoji"] == 2
        st, d = await call("GET", "/api/miniapp/editorial/policy")
        assert d["policy"]["forbidden_words"] == [], "правила канала не должны стать общими"
        st, _ = await call("GET", "/api/miniapp/va/channel/5555")
        assert st == 404
        st, _ = await call("GET", "/api/miniapp/editorial/policy?channel=5555")
        assert st == 404

    _run(go())
