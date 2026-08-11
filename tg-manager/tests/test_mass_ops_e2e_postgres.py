"""Сквозной прогон массовых операций по НАСТОЯЩЕМУ Postgres. Заглушён только
движок Telethon/Reporter — весь жизненный цикл операции (захват аккаунтов,
per-target лог, инкремент done_items, аудит, финализация) исполняется реальным
воркером против живой БД.

ЗАЧЕМ ОТДЕЛЬНО ОТ ЮНИТ-ТЕСТОВ. Заглушка пула не проверяет типы параметров:
фейковый `execute(q, *a)` глотает что угодно, поэтому ошибки СВЯЗЫВАНИЯ (строка
вместо datetime, dict вместо jsonb, список вместо скаляра) на юнит-тестах
невидимы в принципе. Именно так пережил релиз сломанный
`operation_bus.submit(scheduled_for=...)`, убивавший ВСЕ отложенные операции.
Общий жизненный цикл (`operation_queue`/`operation_log`/`operation_audit`) — один
на все 60+ op_type: биндинг-регресс в нём тихо ломает КАЖДУЮ массовую операцию
сразу. `test_invite_e2e_postgres` стережёт этот слой для инвайта; здесь — для
остальных ядровых массовых действий (join/leave/report), чтобы рефактор общего
цикла не прошёл незаметно.

КАК ЗАПУСТИТЬ — см. docstring `test_invite_e2e_postgres.py` (тот же стенд/DSN).
Без `INFRAGRAM_TEST_DSN` файл пропускается: CI и обычный прогон не ломаются.
"""
from __future__ import annotations

import asyncio
import glob
import json
import os
import re

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(
    not DSN, reason="нужен живой Postgres: задайте INFRAGRAM_TEST_DSN (см. docstring)")

OWNER = 990777


# ОДИН цикл на весь модуль: соединения asyncpg привязаны к циклу, в котором создан
# пул (те же грабли, что в test_invite_e2e_postgres).
_LOOP: "asyncio.AbstractEventLoop | None" = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


class _Stand:
    def __init__(self, pool):
        self.pool = pool
        self.acc_ids: list[int] = []
        # режим движка: "ok" → успех, "error" → каждый вызов возвращает ошибку.
        self.mode = "ok"
        self.last_op_id: int | None = None
        self.bot_sends: list[int] = []  # chat_id'ы, ушедшие через aiogram Bot API

    # ── заглушки движка (единственная граница с Telegram) ────────────────────
    async def _join(self, session, ref, _acc=None):
        if self.mode == "error":
            return {"error": "ChannelPrivateError"}
        return {"title": "T", "members": 5, "channel_id": 1}

    async def _leave(self, session, channel, _acc=None):
        if self.mode == "error":
            return {"ok": False, "error": "ChannelPrivateError"}
        return {"ok": True}

    async def _report_peer(self, session, acc, target, reason, text="", *a, **k):
        if self.mode == "error":
            return {"ok": False, "error": "ReportDeclined"}
        return {"ok": True}

    async def _report_message(self, session, acc, target, ids, reason, text="", *a, **k):
        if self.mode == "error":
            return {"ok": False, "error": "ReportDeclined"}
        return {"ok": True}

    async def _send_dm(self, session_str, user_id, text, _acc=None, username=None, **k):
        if self.mode == "error":
            return {"status": "blocked", "error": "UserPrivacyRestricted"}
        return {"status": "sent"}

    async def _post_to_channel(self, session, ref, text, **k):
        # Формат успеха, годный обоим потребителям: mass_publish смотрит на
        # отсутствие error/banned (+resolved_access_hash), bulk_post_to_channel —
        # на наличие "msg_id".
        if self.mode == "error":
            return {"error": "ChatWriteForbidden"}
        self._post_seq = getattr(self, "_post_seq", 0) + 1
        return {"msg_id": 1000 + self._post_seq, "resolved_access_hash": 0}

    async def _get_dialogs(self, session, limit=None, _acc=None):
        return []  # пре-скан access_hash в mass_publish — пусто, безвредно

    async def seed(self, *, accounts=2):
        p = self.pool
        await p.execute("DELETE FROM operation_queue WHERE owner_id=$1", OWNER)
        await p.execute(
            "DELETE FROM operation_audit WHERE account_id IN "
            "(SELECT id FROM tg_accounts WHERE owner_id=$1)", OWNER)
        await p.execute("DELETE FROM tg_accounts WHERE owner_id=$1", OWNER)
        ids = []
        for i in range(accounts):
            ids.append(await p.fetchval(
                "INSERT INTO tg_accounts(owner_id,phone,session_str,is_active,"
                "acc_status,first_name) VALUES($1,$2,$3,TRUE,'active',$4) RETURNING id",
                OWNER, f"+7995{i:07d}", f"sess{i}", f"Acc{i}"))
        self.acc_ids = ids
        return ids

    async def seed_crm_campaign(self, *, contacts=5):
        """Кампания по crm-источнику: аккаунты + crm_contacts + строка dm_campaigns."""
        p = self.pool
        await self.seed(accounts=2)
        await p.execute("DELETE FROM crm_contacts WHERE owner_id=$1", OWNER)
        await p.execute("DELETE FROM dm_campaigns WHERE owner_id=$1", OWNER)
        for i in range(contacts):
            await p.execute(
                "INSERT INTO crm_contacts(owner_id,tg_user_id,username) VALUES($1,$2,$3)",
                OWNER, 600000 + i, f"u{i}")
        return await p.fetchval(
            "INSERT INTO dm_campaigns(owner_id,name,text_template,target_type,status) "
            "VALUES($1,'e2e','Привет','crm','pending') RETURNING id", OWNER)

    async def seed_parsed_gender_campaign(self, *, gender_filter=None):
        """Кампания по parsed_audience с размеченным полом (смычка gender→рассылка).

        Возвращает (campaign_id, female_ids, male_ids). params.gender_filter кладём
        в dm_campaigns.params — его читает dm_engine._get_targets.
        """
        import json as _json
        p = self.pool
        await self.seed(accounts=2)
        await p.execute("DELETE FROM parsed_audiences WHERE owner_id=$1", OWNER)
        await p.execute("DELETE FROM parser_runs WHERE owner_id=$1", OWNER)
        await p.execute("DELETE FROM dm_campaigns WHERE owner_id=$1", OWNER)
        run_id = await p.fetchval(
            "INSERT INTO parser_runs(owner_id,source_type,source_ref,parse_type,status) "
            "VALUES($1,'channel','X','members','done') RETURNING id", OWNER)
        female_ids = [700001, 700002, 700003]
        male_ids = [700010, 700011]
        for i, uid in enumerate(female_ids):
            await p.execute(
                "INSERT INTO parsed_audiences(owner_id,source_type,source_id,parse_run_id,"
                "tg_user_id,username,gender) VALUES($1,'channel',$2,$2,$3,$4,'f')",
                OWNER, run_id, uid, f"f{i}")
        for i, uid in enumerate(male_ids):
            await p.execute(
                "INSERT INTO parsed_audiences(owner_id,source_type,source_id,parse_run_id,"
                "tg_user_id,username,gender) VALUES($1,'channel',$2,$2,$3,$4,'m')",
                OWNER, run_id, uid, f"m{i}")
        params = {"gender_filter": gender_filter} if gender_filter else {}
        cid = await p.fetchval(
            "INSERT INTO dm_campaigns(owner_id,name,text_template,target_type,target_id,"
            "status,params) VALUES($1,'e2e','Привет','parsed_audience',$2,'pending',$3::jsonb) "
            "RETURNING id", OWNER, run_id, _json.dumps(params))
        return cid, female_ids, male_ids

    async def seed_promo(self, *, subscribers=4):
        """managed_bot + подписчики + self_promo шаблон (owner_id — inline-колонка)."""
        p = self.pool
        bot_id = 555001
        token = "555001:AAExampleTokenForTest_1234567890abcd"
        # bot_id и token — глобально уникальны (не только в рамках owner): чистим
        # по ним напрямую, иначе остаток от другого owner/прошлого прогона коллизит.
        await p.execute("DELETE FROM bot_users WHERE bot_id=$1", bot_id)
        await p.execute("DELETE FROM managed_bots WHERE bot_id=$1 OR token=$2", bot_id, token)
        await p.execute("DELETE FROM self_promo_templates WHERE owner_id=$1", OWNER)
        await p.execute("DELETE FROM operation_queue WHERE owner_id=$1", OWNER)
        await p.execute(
            "INSERT INTO managed_bots(bot_id,token,added_by,is_active) VALUES($1,$2,$3,TRUE)",
            bot_id, token, OWNER)
        for i in range(subscribers):
            await p.execute(
                "INSERT INTO bot_users(bot_id,user_id,is_active) VALUES($1,$2,TRUE)",
                bot_id, 700000 + i)
        return await p.fetchval(
            "INSERT INTO self_promo_templates(owner_id,style,title,content,cta_text,cta_url,is_active) "
            "VALUES($1,'direct','T','Наш продукт','Открыть','https://t.me/x?start=y',TRUE) "
            "RETURNING id", OWNER)

    async def seed_channels(self, *, channels=3):
        """Аккаунт-владелец + managed_channels для mass_publish."""
        p = self.pool
        ids = await self.seed(accounts=1)
        acc_id = ids[0]
        await p.execute("DELETE FROM managed_channels WHERE owner_id=$1", OWNER)
        chan_ids = []
        for i in range(channels):
            cid = 100000 + i
            chan_ids.append(cid)
            await p.execute(
                "INSERT INTO managed_channels(owner_id,acc_id,channel_id,title,username,access_hash) "
                "VALUES($1,$2,$3,$4,$5,0)", OWNER, acc_id, cid, f"Ch{i}", f"chan{i}")
        return acc_id, chan_ids

    async def run(self, op_type, params, total=1):
        """Поставить операцию тем же SQL, что и хендлер, и провести воркером."""
        from services import op_worker as w
        p = self.pool
        op_id = await p.fetchval(
            "INSERT INTO operation_queue(owner_id,op_type,status,params,total_items,label) "
            "VALUES($1,$2,'pending',$3,$4,'e2e') RETURNING id",
            OWNER, op_type, json.dumps(params), total)
        rows = await p.fetch(
            "UPDATE operation_queue SET status='running', started_at=now() WHERE id=$1 "
            "RETURNING id, owner_id, op_type, params", op_id)
        self.last_op_id = op_id
        await w._run_op_task(p, None, dict(rows[0]))
        return await p.fetchrow(
            "SELECT status, done_items, total_items, result->>'summary' AS summary, "
            "error_msg FROM operation_queue WHERE id=$1", op_id)

    async def log_counts(self) -> dict:
        rows = await self.pool.fetch(
            "SELECT status, COUNT(*) AS n FROM operation_log WHERE op_id=$1 GROUP BY status",
            self.last_op_id)
        return {r["status"]: r["n"] for r in rows}

    async def audit_count(self) -> int:
        return await self.pool.fetchval(
            "SELECT COUNT(*) FROM operation_audit WHERE operation_id=$1", self.last_op_id)


@pytest.fixture(scope="module")
def stand():
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
                pass  # схемы идемпотентны; частичный сбой не рушит прогон
        # Прод-достоверность: schema_v*.sql — НЕ вся схема. Часть колонок/таблиц
        # заводится инлайн-миграциями mini_app при старте (on_startup). Без них
        # тестовая БД отличается от продовой, и операции, пишущие в такие колонки
        # (напр. managed_channels.last_post_at в mass_publish), молчано-опят —
        # e2e перестаёт отражать реальность. Накатываем тем же списком-источником.
        from services.mini_app_api import INLINE_MIGRATIONS
        for stmt in INLINE_MIGRATIONS:
            try:
                await conn.execute(stmt)
            except Exception:
                pass
        await conn.close()
        return await asyncpg.create_pool(DSN, min_size=1, max_size=4)

    try:
        pool = _run(_boot())
    except Exception as exc:
        pytest.skip(f"Postgres по INFRAGRAM_TEST_DSN недоступен: {str(exc)[:120]}")

    s = _Stand(pool)

    # Подменяем ТОЛЬКО границу с Telegram; жизненный цикл исполняет реальный воркер.
    # Оригиналы восстанавливаем в teardown — иначе заглушки протекут в другие модули
    # (module-scoped фикстуру нельзя чинить function-scoped monkeypatch'ем).
    from services import account_manager, reporter_engine, dm_engine, op_worker as w
    _orig = {
        "join_channel": account_manager.join_channel,
        "leave_channel": account_manager.leave_channel,
        "report_peer": reporter_engine.report_peer,
        "report_message": reporter_engine.report_message,
        "send_dm": dm_engine.send_dm,
        "post_to_channel": account_manager.post_to_channel,
        "get_dialogs": account_manager.get_dialogs,
        "sleep": w.asyncio.sleep,
        "dm_sleep": dm_engine.asyncio.sleep,
    }
    account_manager.join_channel = s._join
    account_manager.leave_channel = s._leave
    reporter_engine.report_peer = s._report_peer
    reporter_engine.report_message = s._report_message
    dm_engine.send_dm = s._send_dm
    account_manager.post_to_channel = s._post_to_channel
    account_manager.get_dialogs = s._get_dialogs

    import aiogram
    _orig_bot_send = aiogram.Bot.send_message

    async def _patched_bot_send(self, chat_id, text, **k):
        s.bot_sends.append(int(chat_id))
        if s.mode == "error":
            raise Exception("Forbidden: bot was blocked by the user")
        return True
    aiogram.Bot.send_message = _patched_bot_send

    _sleep = asyncio.sleep

    async def _fast(x):
        return await _sleep(0)
    w.asyncio.sleep = _fast
    dm_engine.asyncio.sleep = _fast  # DM-раннер спит между отправками — гасим

    yield s

    aiogram.Bot.send_message = _orig_bot_send

    account_manager.join_channel = _orig["join_channel"]
    account_manager.leave_channel = _orig["leave_channel"]
    reporter_engine.report_peer = _orig["report_peer"]
    reporter_engine.report_message = _orig["report_message"]
    dm_engine.send_dm = _orig["send_dm"]
    account_manager.post_to_channel = _orig["post_to_channel"]
    account_manager.get_dialogs = _orig["get_dialogs"]
    w.asyncio.sleep = _orig["sleep"]
    dm_engine.asyncio.sleep = _orig["dm_sleep"]
    _run(pool.close())
    global _LOOP
    if _LOOP is not None and not _LOOP.is_closed():
        _LOOP.close()


# ── bulk_join ────────────────────────────────────────────────────────────────

def test_bulk_join_happy_path(stand):
    stand.mode = "ok"
    _run(stand.seed(accounts=2))
    links = ["@chan1", "@chan2", "https://t.me/+abcdef"]
    params = {"links": links, "account_ids": stand.acc_ids, "delay_mode": "fast"}
    row = _run(stand.run("bulk_join", params, total=len(links) * len(stand.acc_ids)))
    assert row["status"] == "done", row["error_msg"]
    # каждая ссылка × каждый аккаунт → отдельный шаг, все успешны
    assert row["done_items"] == len(links) * len(stand.acc_ids)
    counts = _run(stand.log_counts())
    assert counts.get("ok") == len(links) * len(stand.acc_ids)
    assert counts.get("error", 0) == 0
    assert _run(stand.audit_count()) > 0


def test_bulk_join_errors_are_logged_not_lost(stand):
    stand.mode = "error"
    _run(stand.seed(accounts=1))
    links = ["@x", "@y"]
    params = {"links": links, "account_ids": stand.acc_ids, "delay_mode": "fast"}
    row = _run(stand.run("bulk_join", params, total=len(links)))
    # Контракт _run_op_task: исполнитель тронул цели, но ни одна не удалась
    # (ok=0, failed>0) → статус нормализуется в 'failed'. Это НЕ «конфигурационный
    # отказ» (ok=0 && failed=0) — предохранитель такое учитывает. Главное: каждая
    # проваленная цель честно залогирована (error-путь биндинга живой), а не потеряна.
    assert row["status"] == "failed", row["error_msg"]
    counts = _run(stand.log_counts())
    assert counts.get("error", 0) == len(links)
    assert counts.get("ok", 0) == 0
    # done_items двигается и на провалах — прогресс операции не «зависает» на нуле
    assert row["done_items"] == len(links)


# ── bulk_leave ───────────────────────────────────────────────────────────────

def test_bulk_leave_happy_path(stand):
    stand.mode = "ok"
    _run(stand.seed(accounts=2))
    channels = ["@a", "@b"]
    params = {"channels": channels, "account_ids": stand.acc_ids, "delay_mode": "fast"}
    row = _run(stand.run("bulk_leave", params, total=len(channels) * len(stand.acc_ids)))
    assert row["status"] == "done", row["error_msg"]
    assert row["done_items"] == len(channels) * len(stand.acc_ids)
    counts = _run(stand.log_counts())
    assert counts.get("ok") == len(channels) * len(stand.acc_ids)


# ── mass_report / report_peer ────────────────────────────────────────────────

def test_mass_report_happy_path(stand):
    stand.mode = "ok"
    ids = _run(stand.seed(accounts=2))
    params = {"mode": "peer", "target": "@bad", "reason": "spam", "account_ids": ids}
    row = _run(stand.run("mass_report", params, total=len(ids)))
    assert row["status"] == "done", row["error_msg"]
    assert row["done_items"] == len(ids)
    counts = _run(stand.log_counts())
    assert counts.get("ok") == len(ids)


def test_report_peer_happy_path(stand):
    stand.mode = "ok"
    ids = _run(stand.seed(accounts=2))
    # report_peer берёт число аккаунтов из total_items операции
    params = {"target": "@bad2", "reason": "spam"}
    row = _run(stand.run("report_peer", params, total=len(ids)))
    assert row["status"] == "done", row["error_msg"]
    assert row["done_items"] == len(ids)


# ── dm_campaign (самый богатый по биндингу: per-target dm_campaign_log,
#    счётчики sent/fail в dm_campaigns, done_items в operation_queue) ──────────

def test_dm_campaign_happy_path(stand):
    stand.mode = "ok"
    contacts = 5
    cid = _run(stand.seed_crm_campaign(contacts=contacts))
    row = _run(stand.run("dm_campaign", {"campaign_id": cid}, total=0))
    assert row["status"] == "done", row["error_msg"]
    # total_items выставляет сам раннер (из числа целей), done_items доходит до него
    assert row["done_items"] == contacts
    camp = _run(stand.pool.fetchrow(
        "SELECT status, sent_count, fail_count, total_targets FROM dm_campaigns WHERE id=$1",
        cid))
    assert camp["status"] == "done"
    assert camp["sent_count"] == contacts
    assert camp["total_targets"] == contacts
    sent_logs = _run(stand.pool.fetchval(
        "SELECT COUNT(*) FROM dm_campaign_log WHERE campaign_id=$1 AND status='sent'", cid))
    assert sent_logs == contacts


def test_dm_campaign_blocked_targets_counted(stand):
    stand.mode = "error"
    contacts = 3
    cid = _run(stand.seed_crm_campaign(contacts=contacts))
    row = _run(stand.run("dm_campaign", {"campaign_id": cid}, total=0))
    # Ни одна не доставлена (ok=0, failed>0) → _run_op_task нормализует op в 'failed'.
    # Но кампания реально отработала все цели и учла их — не «зависла».
    assert row["status"] == "failed", row["error_msg"]
    camp = _run(stand.pool.fetchrow(
        "SELECT sent_count, fail_count FROM dm_campaigns WHERE id=$1", cid))
    assert camp["sent_count"] == 0
    assert camp["fail_count"] == contacts
    # 'blocked' пишется в dm_campaign_log — цели не потеряны, повторно не долбим
    blocked = _run(stand.pool.fetchval(
        "SELECT COUNT(*) FROM dm_campaign_log WHERE campaign_id=$1 AND status='blocked'", cid))
    assert blocked == contacts


# ── смычка: gender-фильтр аудитории → таргетинг DM-рассылки (end-to-end) ──────

def test_dm_gender_filter_targets_only_selected_gender(stand):
    stand.mode = "ok"
    cid, female_ids, male_ids = _run(stand.seed_parsed_gender_campaign(gender_filter="f"))
    row = _run(stand.run("dm_campaign", {"campaign_id": cid}, total=0))
    assert row["status"] == "done", row["error_msg"]
    # доставлено РОВНО женщинам — мужчины отфильтрованы движком по params.gender_filter
    sent_ids = _run(stand.pool.fetch(
        "SELECT tg_user_id FROM dm_campaign_log WHERE campaign_id=$1 AND status='sent'", cid))
    got = {r["tg_user_id"] for r in sent_ids}
    assert got == set(female_ids)
    assert not (got & set(male_ids))


def test_dm_no_gender_filter_targets_everyone(stand):
    stand.mode = "ok"
    cid, female_ids, male_ids = _run(stand.seed_parsed_gender_campaign(gender_filter=None))
    row = _run(stand.run("dm_campaign", {"campaign_id": cid}, total=0))
    assert row["status"] == "done", row["error_msg"]
    got = {r["tg_user_id"] for r in _run(stand.pool.fetch(
        "SELECT tg_user_id FROM dm_campaign_log WHERE campaign_id=$1 AND status='sent'", cid))}
    assert got == set(female_ids) | set(male_ids)


# ── mass_publish (публикация во все каналы владельца + сигнал активности) ─────

def test_mass_publish_happy_path_sets_activity_signal(stand):
    stand.mode = "ok"
    channels = 3
    _acc_id, chan_ids = _run(stand.seed_channels(channels=channels))
    params = {"target": "channels", "text": "Привет, друзья!"}
    row = _run(stand.run("mass_publish", params, total=0))
    assert row["status"] == "done", row["error_msg"]
    assert row["done_items"] == channels
    counts = _run(stand.log_counts())
    assert counts.get("ok") == channels
    # Ключевое: реальная публикация проставляет managed_channels.last_post_at —
    # сигнал активности, который кормит ecosystem auto_remove_dead_channels. Колонка
    # существует ТОЛЬКО благодаря инлайн-миграциям (в schema_v*.sql её нет) — тест
    # заодно стережёт прод-достоверность стенда: без наката INLINE_MIGRATIONS этот
    # SELECT упал бы «column does not exist».
    posted = _run(stand.pool.fetchval(
        "SELECT COUNT(*) FROM managed_channels "
        "WHERE owner_id=$1 AND last_post_at IS NOT NULL", OWNER))
    assert posted == channels


# ── bulk_post_to_channel (публикация в канал от нескольких аккаунтов) ────────

def test_bulk_post_to_channel_happy_path(stand):
    stand.mode = "ok"
    ids = _run(stand.seed(accounts=3))
    params = {"account_ids": ids, "channel_ref": "@chan", "text_to_post": "Привет"}
    row = _run(stand.run("bulk_post_to_channel", params, total=len(ids)))
    assert row["status"] == "done", row["error_msg"]
    # done_items доходит до числа аккаунтов, все опубликовали (успех по msg_id)
    assert row["done_items"] == len(ids)
    assert "✅" in (row["summary"] or "") and "❌" not in (row["summary"] or "")


def test_bulk_post_to_channel_all_fail_marks_failed(stand):
    stand.mode = "error"
    ids = _run(stand.seed(accounts=2))
    params = {"account_ids": ids, "channel_ref": "@chan", "text_to_post": "Привет"}
    row = _run(stand.run("bulk_post_to_channel", params, total=len(ids)))
    # ни одна публикация не удалась → статус нормализуется в failed, прогресс дошёл
    assert row["status"] == "failed", row["error_msg"]
    assert row["done_items"] == len(ids)


# ── self_promo_blast (рассылка подписчикам ботов через Bot API) ──────────────
# Фильтр по self_promo_templates.owner_id — колонка существует ТОЛЬКО из
# инлайн-миграций (в schema_v101 её нет). На schema-only стенде запрос упал бы,
# т.е. без прод-достоверности эта массовая операция вообще непроверяема.

def test_self_promo_blast_happy_path(stand):
    stand.mode = "ok"
    stand.bot_sends.clear()
    subscribers = 4
    tid = _run(stand.seed_promo(subscribers=subscribers))
    row = _run(stand.run("self_promo_blast", {"template_id": tid}, total=0))
    assert row["status"] == "done", row["error_msg"]
    assert row["done_items"] == subscribers
    assert len(stand.bot_sends) == subscribers
    # use_count шаблона инкрементится после рассылки (учёт использования)
    use_count = _run(stand.pool.fetchval(
        "SELECT use_count FROM self_promo_templates WHERE id=$1", tid))
    assert use_count == 1


def test_self_promo_blast_deactivates_blockers(stand):
    stand.mode = "error"  # каждый send → "bot was blocked by the user"
    stand.bot_sends.clear()
    subscribers = 3
    tid = _run(stand.seed_promo(subscribers=subscribers))
    row = _run(stand.run("self_promo_blast", {"template_id": tid}, total=0))
    # ни одна не доставлена → op нормализуется в failed, но операция отработала
    assert row["status"] == "failed", row["error_msg"]
    # заблокировавшие боты помечаются is_active=FALSE — рассылка самоочищается
    active_left = _run(stand.pool.fetchval(
        "SELECT COUNT(*) FROM bot_users bu JOIN managed_bots mb ON mb.bot_id=bu.bot_id "
        "WHERE mb.added_by=$1 AND bu.is_active=TRUE", OWNER))
    assert active_left == 0
