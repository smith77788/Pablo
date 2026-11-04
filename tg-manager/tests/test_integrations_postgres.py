"""Вебхуки на живой базе: триггеры → раскладка → доставка → повтор → выключение.

На заглушке пула нельзя проверить главное: что триггер действительно стреляет
на закрытии операции и смене статуса аккаунта, что раскладка уважает подписку
и не дублирует доставки, что уникальный ключ держит «одно событие — одна
доставка получателю», и что серия неудач выключает получателя. Нужен Postgres
(INFRAGRAM_TEST_DSN, см. CLAUDE.md «Схемные проверки можно гонять локально»).
"""
from __future__ import annotations

import asyncio
import json
import os

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER = 993262
OTHER = 993263

_LOOP = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


async def _cleanup(p):
    for o in (OWNER, OTHER):
        await p.execute("DELETE FROM webhook_endpoints WHERE owner_id=$1", o)
        await p.execute("DELETE FROM integration_events WHERE owner_id=$1", o)
        await p.execute("DELETE FROM operation_queue WHERE owner_id=$1", o)
        await p.execute("DELETE FROM restriction_events WHERE owner_id=$1", o)
        await p.execute("DELETE FROM tg_accounts WHERE owner_id=$1", o)
        await p.execute("DELETE FROM api_keys WHERE user_id=$1", o)
        await p.execute("DELETE FROM automation_events WHERE owner_id=$1", o)


@pytest.fixture(scope="module")
def pool():
    if not DSN:
        pytest.skip("нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")
    import asyncpg
    async def _mk():
        return await asyncpg.create_pool(DSN, min_size=1, max_size=4)

    try:
        p = _run(_mk())
    except Exception as exc:
        pytest.skip(f"Postgres по INFRAGRAM_TEST_DSN недоступен: {str(exc)[:120]}")
    has = _run(p.fetchval("SELECT to_regclass('webhook_endpoints') IS NOT NULL"))
    assert has, "schema_v262_integrations.sql не применилась"
    _run(_cleanup(p))
    yield p
    _run(_cleanup(p))
    _run(p.close())


@pytest.fixture(autouse=True)
def _clean_each(pool):
    _run(_cleanup(pool))
    yield


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch):
    """В тестовом контейнере DNS наружу нет — адрес получателя считаем публичным."""
    from services import security

    async def _ok(url):
        return security.is_safe_public_url(url)
    monkeypatch.setattr(security, "resolve_url_is_public", _ok)


class _Resp:
    def __init__(self, status, headers=None):
        self.status = status
        self.headers = headers or {}

        class _C:
            async def read(self, n):
                return b"nope"
        self.content = _C()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Http:
    def __init__(self, status=200, resp_headers=None):
        self.status = status
        self.resp_headers = resp_headers
        self.calls = []

    def post(self, url, data=None, headers=None, **kw):
        self.calls.append({"url": url, "body": data, "headers": headers, **kw})
        return _Resp(self.status, self.resp_headers)


async def _endpoint(pool, owner=OWNER, events=None):
    from services import webhooks as wh
    return await wh.create_endpoint(pool, owner, kind="http", name="тест",
                                    url="https://hooks.example.com/in", events=events)


async def _op(pool, owner=OWNER):
    return await pool.fetchval(
        "INSERT INTO operation_queue (owner_id, op_type, status, total_items, done_items) "
        "VALUES ($1, 'broadcast', 'running', 10, 0) RETURNING id", owner)


def test_no_endpoint_no_event(pool):
    """У владельца без получателей таблица событий не растёт."""
    op = _run(_op(pool))
    _run(pool.execute("UPDATE operation_queue SET status='done' WHERE id=$1", op))
    assert _run(pool.fetchval(
        "SELECT COUNT(*) FROM integration_events WHERE owner_id=$1", OWNER)) == 0


def test_operation_finish_is_delivered_signed_once(pool):
    from services import webhooks as wh
    from services.token_vault import decrypt_token
    ep = _run(_endpoint(pool))
    secret = ep["secret"]
    op = _run(_op(pool))
    _run(pool.execute("UPDATE operation_queue SET done_items=9 WHERE id=$1", op))
    _run(pool.execute("UPDATE operation_queue SET status='partial' WHERE id=$1", op))
    # повторная запись того же статуса — не новое событие
    _run(pool.execute("UPDATE operation_queue SET status='partial' WHERE id=$1", op))
    evs = _run(pool.fetch("SELECT event, payload FROM integration_events WHERE owner_id=$1", OWNER))
    assert [e["event"] for e in evs] == ["operation.partial"]
    payload = json.loads(evs[0]["payload"])
    assert payload["operation_id"] == op and payload["done_items"] == 9

    assert _run(wh.fan_out(pool)) == 1
    assert _run(wh.fan_out(pool)) == 0, "раскладка не повторяется"
    http = _Http(200)
    assert _run(wh.deliver_due(pool, http)) == 1
    assert len(http.calls) == 1
    call = http.calls[0]
    assert call["allow_redirects"] is False
    h = call["headers"]
    assert h["X-Infragram-Event"] == "operation.partial"
    assert wh.verify(secret, h["X-Infragram-Signature"], call["body"])
    stored = _run(pool.fetchval("SELECT secret_enc FROM webhook_endpoints WHERE id=$1", ep["id"]))
    assert stored != secret and decrypt_token(stored) == secret, "секрет хранится зашифрованным"
    st = _run(pool.fetchrow("SELECT status, attempts FROM webhook_deliveries"
                            " WHERE endpoint_id=$1", ep["id"]))
    assert (st["status"], st["attempts"]) == ("delivered", 1)
    assert _run(wh.deliver_due(pool, http)) == 0, "доставленное не уходит второй раз"


def test_subscription_filter_and_owner_isolation(pool):
    from services import webhooks as wh
    only_lost = _run(_endpoint(pool, events=["account.lost"]))
    _run(_endpoint(pool, owner=OTHER))
    acc = _run(pool.fetchval(
        "INSERT INTO tg_accounts (owner_id, phone, session_str) VALUES ($1, '+70009932620', 'x')"
        " RETURNING id", OWNER))
    _run(pool.execute("UPDATE tg_accounts SET acc_status='banned' WHERE id=$1", acc))
    names = sorted(r["event"] for r in _run(pool.fetch(
        "SELECT event FROM integration_events WHERE owner_id=$1", OWNER)))
    assert names == ["account.added", "account.lost"]
    _run(wh.fan_out(pool))
    got = _run(pool.fetch("SELECT endpoint_id, event FROM webhook_deliveries WHERE owner_id=$1", OWNER))
    assert [(r["endpoint_id"], r["event"]) for r in got] == [(only_lost["id"], "account.lost")]
    assert _run(pool.fetchval(
        "SELECT COUNT(*) FROM webhook_deliveries WHERE owner_id=$1", OTHER)) == 0, \
        "событие владельца ушло получателю другого владельца"


def test_restriction_event_is_emitted(pool):
    _run(_endpoint(pool))
    _run(pool.execute(
        "INSERT INTO restriction_events (owner_id, event_type, severity) "
        "VALUES ($1, 'dm_restricted', 'critical')", OWNER))
    ev = _run(pool.fetchrow("SELECT event, payload FROM integration_events WHERE owner_id=$1", OWNER))
    assert ev["event"] == "account.restricted"
    assert json.loads(ev["payload"])["kind"] == "dm_restricted"


def test_failure_backs_off_then_endpoint_is_disabled(pool):
    from services import webhooks as wh
    ep = _run(_endpoint(pool))
    _run(wh.emit(pool, OWNER, "webhook.test", {"n": 1}))
    _run(wh.fan_out(pool))
    _run(wh.deliver_due(pool, _Http(500)))
    row = _run(pool.fetchrow(
        "SELECT status, attempts, last_status_code, next_attempt_at > now() + interval '50 seconds' AS later"
        " FROM webhook_deliveries WHERE endpoint_id=$1", ep["id"]))
    assert row["status"] == "pending" and row["attempts"] == 1
    assert row["last_status_code"] == 500 and row["later"], "повтор не раньше минуты"
    e = _run(wh.get_endpoint(pool, OWNER, ep["id"]))
    assert e["consecutive_failures"] == 1 and e["is_active"] and "500" in e["last_error"]

    # серия неудач выключает получателя с понятной причиной
    _run(pool.execute("UPDATE webhook_endpoints SET consecutive_failures=$2, "
                      "last_failure_at=now()-interval '2 minutes' WHERE id=$1",
                      ep["id"], wh.DISABLE_AFTER_FAILURES - 1))
    _run(pool.execute("UPDATE webhook_deliveries SET next_attempt_at=now() WHERE endpoint_id=$1", ep["id"]))
    _run(wh.deliver_due(pool, _Http(502)))
    e = _run(wh.get_endpoint(pool, OWNER, ep["id"]))
    assert not e["is_active"] and str(wh.DISABLE_AFTER_FAILURES) in e["disabled_reason"]

    # включение заново обнуляет серию и причину
    e = _run(wh.update_endpoint(pool, OWNER, ep["id"], is_active=True))
    assert e["is_active"] and e["consecutive_failures"] == 0 and not e["disabled_reason"]


def test_last_attempt_marks_failed_and_redeliver_requeues(pool):
    from services import webhooks as wh
    ep = _run(_endpoint(pool))
    _run(wh.emit(pool, OWNER, "webhook.test", {}))
    _run(wh.fan_out(pool))
    _run(pool.execute("UPDATE webhook_deliveries SET attempts=$2 WHERE endpoint_id=$1",
                      ep["id"], wh.MAX_ATTEMPTS - 1))
    _run(wh.deliver_due(pool, _Http(400)))
    d = _run(pool.fetchrow("SELECT id, status FROM webhook_deliveries WHERE endpoint_id=$1", ep["id"]))
    assert d["status"] == "failed"
    assert not _run(wh.redeliver(pool, OTHER, d["id"])), "чужую доставку не повторить"
    assert _run(wh.redeliver(pool, OWNER, d["id"]))
    _run(wh.deliver_due(pool, _Http(200)))
    assert _run(pool.fetchval("SELECT status FROM webhook_deliveries WHERE id=$1", d["id"])) == "delivered"
    log = _run(wh.list_deliveries(pool, OWNER, ep["id"]))
    assert log[0]["status"] == "delivered" and log[0]["event_label"] == "Тестовое событие"
    assert _run(wh.list_deliveries(pool, OTHER, ep["id"])) == []


def test_gone_disables_immediately(pool):
    from services import webhooks as wh
    ep = _run(_endpoint(pool))
    _run(wh.emit(pool, OWNER, "webhook.test", {}))
    _run(wh.fan_out(pool))
    _run(wh.deliver_due(pool, _Http(410)))
    e = _run(wh.get_endpoint(pool, OWNER, ep["id"]))
    assert not e["is_active"] and "410" in e["disabled_reason"]


def test_disabled_endpoint_gets_no_new_deliveries(pool):
    from services import webhooks as wh
    ep = _run(_endpoint(pool))
    _run(wh.update_endpoint(pool, OWNER, ep["id"], is_active=False))
    assert _run(wh.emit(pool, OWNER, "webhook.test", {})) is None, \
        "без активного получателя событие не пишется"


def test_trigger_failure_does_not_block_operation_close(pool):
    """Сбой интеграции не откатывает закрытие операции владельца."""
    _run(_endpoint(pool))
    op = _run(_op(pool))
    async def _go():
        await pool.execute("ALTER TABLE integration_events ADD CONSTRAINT _t_block CHECK (false) NOT VALID")
        try:
            await pool.execute("UPDATE operation_queue SET status='failed' WHERE id=$1", op)
        finally:
            await pool.execute("ALTER TABLE integration_events DROP CONSTRAINT _t_block")
    _run(_go())
    assert _run(pool.fetchval("SELECT status FROM operation_queue WHERE id=$1", op)) == "failed"


def test_api_key_lifecycle_on_real_schema(pool):
    from services import api_keys
    k = _run(api_keys.create_key(pool, OWNER, "n8n", ["read"]))
    info = _run(api_keys.authenticate(pool, k["key"]))
    assert info == {"key_id": k["id"], "owner_id": OWNER, "scopes": ["read"]}
    row = _run(pool.fetchrow("SELECT key_hash, requests_total, last_used_at FROM api_keys WHERE id=$1", k["id"]))
    assert row["key_hash"] != k["key"] and row["requests_total"] == 1 and row["last_used_at"]
    assert _run(api_keys.revoke_key(pool, OWNER, k["id"]))
    assert _run(api_keys.authenticate(pool, k["key"])) is None
    listed = _run(api_keys.list_keys(pool, OWNER))
    assert listed[0]["is_active"] is False


def test_old_unprefixed_keys_keep_working(pool):
    """Ключи, выданные мини-аппом до общего модуля, — без приставки bm_."""
    import hashlib
    from services import api_keys
    raw = "legacyKEYwithoutPrefix0123456789"
    _run(pool.execute(
        "INSERT INTO api_keys (user_id, key_hash, key_prefix, name) VALUES ($1,$2,$3,'старый')",
        OWNER, hashlib.sha256(raw.encode()).hexdigest(), raw[:8]))
    info = _run(api_keys.authenticate(pool, raw))
    assert info and info["owner_id"] == OWNER and info["scopes"] == ["read", "write"]


def test_prune_drops_old_finished_events(pool):
    from services import webhooks as wh
    _run(_endpoint(pool))
    eid = _run(wh.emit(pool, OWNER, "webhook.test", {}))
    _run(wh.fan_out(pool))
    _run(wh.deliver_due(pool, _Http(200)))
    _run(pool.execute("UPDATE integration_events SET created_at=now()-interval '30 days' WHERE id=$1", eid))
    _run(wh.prune(pool))
    assert _run(pool.fetchval("SELECT COUNT(*) FROM integration_events WHERE id=$1", eid)) == 0
    assert _run(pool.fetchval("SELECT COUNT(*) FROM webhook_deliveries WHERE event_id=$1", eid)) == 0


def test_automation_events_reach_subscribed_endpoints_only(pool):
    """Событие сценария копируется, только если на него есть подписка;
    входящий вебхук наружу не пересылается (петля)."""
    from services import webhooks as wh
    ep = _run(_endpoint(pool, events=["payment.received"]))
    for ev in ("message.received", "payment.received", "webhook.received"):
        _run(pool.execute(
            "INSERT INTO automation_events (owner_id, event_type, bot_id, data) "
            "VALUES ($1, $2, 77, '{\"amount\": 5}'::jsonb)", OWNER, ev))
    got = _run(pool.fetch("SELECT event, payload FROM integration_events WHERE owner_id=$1", OWNER))
    assert [g["event"] for g in got] == ["payment.received"]
    payload = json.loads(got[0]["payload"])
    assert payload["amount"] == 5 and payload["bot_id"] == 77
    _run(wh.fan_out(pool))
    assert _run(pool.fetchval(
        "SELECT COUNT(*) FROM webhook_deliveries WHERE endpoint_id=$1", ep["id"])) == 1


def test_one_minute_outage_with_a_backlog_does_not_disable_the_endpoint(pool):
    """В очереди больше событий, чем DISABLE_AFTER_FAILURES, и получатель
    минуту недоступен: раньше каждая упавшая строка удлиняла серию, получатель
    выключался навсегда, а все его доставки уходили в 'skipped'."""
    from services import webhooks as wh
    ep = _run(_endpoint(pool))
    n = wh.DISABLE_AFTER_FAILURES + 5
    for i in range(n):
        _run(wh.emit(pool, OWNER, "webhook.test", {"n": i}))
    _run(wh.fan_out(pool))
    while _run(wh.deliver_due(pool, _Http(503))):
        pass
    e = _run(wh.get_endpoint(pool, OWNER, ep["id"]))
    assert e["is_active"], f"минута сбоя выключила получателя: {e['disabled_reason']}"
    assert e["consecutive_failures"] == 1
    # Сбой прошёл — очередь доходит целиком.
    _run(pool.execute("UPDATE webhook_deliveries SET next_attempt_at=now() "
                      "WHERE endpoint_id=$1", ep["id"]))
    while _run(wh.deliver_due(pool, _Http(200))):
        pass
    left = _run(pool.fetchval(
        "SELECT COUNT(*) FROM webhook_deliveries WHERE endpoint_id=$1 "
        "AND status <> 'delivered'", ep["id"]))
    assert left == 0, f"не доставлено после сбоя: {left}"


def test_reenabling_an_endpoint_disabled_by_failures_catches_up(pool):
    """Получателя выключили сбои — ожидавшее доставки ушло в 'skipped'. Раньше
    эти события терялись насовсем: владелец чинил сервер, включал получателя,
    а события за простой так и не приходили. Ручная пауза не догоняет."""
    from services import webhooks as wh
    ep = _run(_endpoint(pool))
    for i in range(3):
        _run(wh.emit(pool, OWNER, "webhook.test", {"n": i}))
    _run(wh.fan_out(pool))
    _run(pool.execute(
        "UPDATE webhook_endpoints SET is_active=FALSE, disabled_reason='Выключен после 50 "
        "неудачных доставок подряд' WHERE id=$1", ep["id"]))
    _run(wh.deliver_due(pool, _Http(200)))
    assert _run(pool.fetchval(
        "SELECT COUNT(*) FROM webhook_deliveries WHERE endpoint_id=$1 AND status='skipped'",
        ep["id"])) == 3

    e = _run(wh.update_endpoint(pool, OWNER, ep["id"], is_active=True))
    assert e["is_active"] and e["requeued"] == 3
    http = _Http(200)
    _run(wh.deliver_due(pool, http))
    assert len(http.calls) == 3, "недоставленное за простой не догнало получателя"

    # Ручная пауза владельца — его решение: включение ничего не догоняет.
    _run(wh.emit(pool, OWNER, "webhook.test", {"n": 9}))
    _run(wh.fan_out(pool))
    _run(wh.update_endpoint(pool, OWNER, ep["id"], is_active=False))
    _run(wh.deliver_due(pool, _Http(200)))
    e = _run(wh.update_endpoint(pool, OWNER, ep["id"], is_active=True))
    assert e["requeued"] == 0


def test_a_receiver_asking_to_wait_is_not_hammered_or_disabled(pool):
    """429 с Retry-After: следующая попытка — не раньше, чем просит получатель,
    и серию неудач, которая его выключает, «подождите» не удлиняет. Раньше
    повтор шёл через минуту, снова попадал в лимит, и вежливый отказ за час
    выключал получателя навсегда."""
    from services import webhooks as wh
    ep = _run(_endpoint(pool))
    _run(wh.emit(pool, OWNER, "webhook.test", {}))
    _run(wh.fan_out(pool))
    _run(wh.deliver_due(pool, _Http(429, {"Retry-After": "600"})))
    row = _run(pool.fetchrow(
        "SELECT status, next_attempt_at > now() + interval '590 seconds' AS waits "
        "FROM webhook_deliveries WHERE endpoint_id=$1", ep["id"]))
    assert row["status"] == "pending" and row["waits"], "Retry-After не соблюдён"
    e = _run(wh.get_endpoint(pool, OWNER, ep["id"]))
    assert e["consecutive_failures"] == 0 and e["is_active"]


def test_retry_after_parsing():
    from services import webhooks as wh
    assert wh.retry_after_seconds("120") == 120
    assert wh.retry_after_seconds("-5") == 0
    assert wh.retry_after_seconds(None) is None
    assert wh.retry_after_seconds("скоро") is None
    assert wh.retry_after_seconds("Wed, 21 Oct 2015 07:28:00 GMT") == 0


def test_google_sheets_quota_waits_instead_of_counting_failures(pool, monkeypatch):
    """Квота Google Таблиц (429) — «подождите», а не неудача получателя."""
    from services import google_sheets as gs
    from services import webhooks as wh

    ep_id = _run(pool.fetchval(
        "INSERT INTO webhook_endpoints (owner_id, kind, name, spreadsheet_id, sheet_name, events) "
        "VALUES ($1, 'sheets', 'лист', 'sheet-id-1', 'События', '{}'::text[]) RETURNING id",
        OWNER))

    async def _quota(pool_, owner_id, sid, sheet, rows):
        return False, 429, "Google ограничил частоту запросов — повторим позже"

    monkeypatch.setattr(gs, "append_event_rows", _quota)
    _run(wh.emit(pool, OWNER, "webhook.test", {}))
    _run(wh.fan_out(pool))
    _run(wh.deliver_due(pool, _Http(200)))
    row = _run(pool.fetchrow(
        "SELECT status, next_attempt_at > now() + interval '50 seconds' AS waits "
        "FROM webhook_deliveries WHERE endpoint_id=$1", ep_id))
    assert row["status"] == "pending" and row["waits"]
    assert _run(pool.fetchval(
        "SELECT consecutive_failures FROM webhook_endpoints WHERE id=$1", ep_id)) == 0
