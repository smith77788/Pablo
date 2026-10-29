"""Онбординг и пробный период на живом Postgres.

P0 D8: чеклист гас сразу после первого аккаунта — подключение само ставит
`contacts_push`, а эндпоинт считал «активацией» любую строку operation_queue.
Здесь проверяется весь путь новичка: аккаунт → (триал) → первое действие →
провал и восстановление → первый результат → липкая активация с записью.

D15 (решение продукта — пробный период): триал выдаётся один раз, после
первого живого аккаунта, и не выдаётся повторно ни тому же владельцу, ни
другому владельцу с теми же Telegram-аккаунтами.
"""
from __future__ import annotations

import asyncio
import json
import os

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER = 88_100_001
OTHER = 88_100_002
OWNERS = (OWNER, OTHER)

_LOOP: "asyncio.AbstractEventLoop | None" = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


async def _clean(p):
    for t, col in (("operation_queue", "owner_id"), ("tg_accounts", "owner_id"),
                   ("account_warmup_plans", "owner_id"),
                   ("onboarding_progress", "owner_id"),
                   ("subscription_trials", "owner_id"),
                   ("subscriptions", "user_id"), ("platform_users", "user_id"),
                   ("user_proxies", "owner_id")):
        await p.execute(f"DELETE FROM {t} WHERE {col} = ANY($1::bigint[])", list(OWNERS))
    from bot.utils.subscription import invalidate_plan_cache
    for o in OWNERS:
        invalidate_plan_cache(o)


@pytest.fixture()
def pool(monkeypatch):
    if not DSN:
        pytest.skip("нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")
    monkeypatch.delenv("TRIAL_DAYS", raising=False)
    monkeypatch.setenv("ADMIN_IDS", "")
    import asyncpg

    async def _mk():
        return await asyncpg.create_pool(DSN, min_size=1, max_size=4)

    try:
        p = _run(_mk())
    except Exception as exc:
        pytest.skip(f"Postgres по INFRAGRAM_TEST_DSN недоступен: {str(exc)[:120]}")
    _run(_clean(p))
    yield p
    _run(_clean(p))
    _run(p.close())


def _acc(pool, owner=OWNER, status="active", tg_user_id=None):
    return _run(pool.fetchval(
        "INSERT INTO tg_accounts(owner_id,phone,session_str,is_active,acc_status,tg_user_id) "
        "VALUES($1,$2,'sess',true,$3,$4) RETURNING id",
        owner, f"+7999{os.urandom(3).hex()}", status, tg_user_id))


def _op(pool, op_type, status, *, owner=OWNER, result=None, label=None,
        done=0, total=0, error=None):
    return _run(pool.fetchval(
        "INSERT INTO operation_queue(owner_id,op_type,status,params,result,label,"
        "done_items,total_items,error_msg,finished_at) "
        "VALUES($1,$2,$3,'{}'::jsonb,$4::jsonb,$5,$6,$7,$8,"
        " CASE WHEN $3 IN ('done','partial','failed','cancelled') THEN now() END) "
        "RETURNING id",
        owner, op_type, status, json.dumps(result) if result is not None else None,
        label, done, total, error))


def _checklist(pool, owner=OWNER):
    from services import onboarding
    return _run(onboarding.load_checklist(pool, owner))


def _step(r, sid):
    return next(s for s in r["steps"] if s["id"] == sid)


def test_account_with_system_sync_does_not_activate(pool):
    """Ровно сценарий P0: аккаунт подключён, платформа поставила contacts_push
    и он даже выполнился — владелец ничего не запускал."""
    _acc(pool)
    _op(pool, "contacts_push", "done", result={"ok": 1, "status": "done"})
    _op(pool, "check_accounts_health", "done", result={"ok": 1})
    r = _checklist(pool)
    assert r["activated"] is False
    assert _step(r, "accounts")["done"] is True
    assert _step(r, "first_op")["done"] is False
    assert r["next_step"] == "first_op"
    assert _run(pool.fetchval(
        "SELECT COUNT(*) FROM onboarding_progress WHERE owner_id=$1", OWNER)) == 0


def test_trial_granted_once_after_first_live_account(pool):
    # До аккаунта триал не выдаётся, но обещан.
    r = _checklist(pool)
    assert _step(r, "plan")["done"] is False
    assert "Пробный период" in _step(r, "plan")["title"]
    assert _run(pool.fetchval(
        "SELECT COUNT(*) FROM subscription_trials WHERE owner_id=$1", OWNER)) == 0

    _acc(pool, tg_user_id=555001)
    r = _checklist(pool)
    plan = _step(r, "plan")
    assert plan["done"] is True and "осталось 3 дн." in plan["title"]
    row = _run(pool.fetchrow(
        "SELECT s.plan, s.is_active, s.expires_at > now() + interval '2 days' AS long, "
        "t.account_tg_user_ids FROM subscriptions s JOIN subscription_trials t "
        "ON t.owner_id=s.user_id WHERE s.user_id=$1", OWNER))
    assert row["plan"] == "paid" and row["is_active"] and row["long"]
    assert list(row["account_tg_user_ids"]) == [555001]

    from bot.utils.subscription import require_plan
    assert _run(require_plan(pool, OWNER, "paid")) is True

    # Истёк — второй раз не выдаётся.
    _run(pool.execute("UPDATE subscriptions SET expires_at=now()-interval '1 hour' "
                      "WHERE user_id=$1", OWNER))
    _run(pool.execute("UPDATE subscription_trials SET expires_at=now()-interval '1 hour' "
                      "WHERE owner_id=$1", OWNER))
    from bot.utils.subscription import invalidate_plan_cache
    invalidate_plan_cache(OWNER)
    from services import trial
    assert _run(trial.maybe_grant(pool, OWNER)) is None
    r = _checklist(pool)
    assert _step(r, "plan")["done"] is False
    assert _step(r, "plan")["action"] == {"kind": "billing"}


def test_trial_not_reused_with_same_telegram_account(pool):
    from services import trial
    _acc(pool, owner=OWNER, tg_user_id=555002)
    assert _run(trial.maybe_grant(pool, OWNER)) is not None
    # Второй бот-пользователь подключает тот же Telegram-аккаунт.
    _acc(pool, owner=OTHER, tg_user_id=555002)
    assert _run(trial.is_eligible(pool, OTHER)) is False
    assert _run(trial.maybe_grant(pool, OTHER)) is None
    assert _run(pool.fetchval(
        "SELECT COUNT(*) FROM subscriptions WHERE user_id=$1", OTHER)) == 0


def test_no_trial_for_previous_subscriber_or_dead_account(pool):
    from services import trial
    _acc(pool, status="banned")
    assert _run(trial.maybe_grant(pool, OWNER)) is None   # нет живого аккаунта
    _acc(pool)
    _run(pool.execute(
        "INSERT INTO subscriptions(user_id,plan,expires_at,is_active) "
        "VALUES($1,'paid',now()-interval '10 days',false)", OWNER))
    assert _run(trial.maybe_grant(pool, OWNER)) is None   # уже был подписчиком


def test_concurrent_grants_give_one_trial(pool):
    from services import trial
    _acc(pool)

    async def _both():
        return await asyncio.gather(*(trial.maybe_grant(pool, OWNER) for _ in range(5)))
    got = [x for x in _run(_both()) if x is not None]
    assert len(got) == 1
    assert _run(pool.fetchval(
        "SELECT COUNT(*) FROM subscription_trials WHERE owner_id=$1", OWNER)) == 1


def test_operation_bus_grants_trial_instead_of_paywall(pool):
    from services import operation_bus
    _acc(pool)
    _run(operation_bus.enforce_gates(pool, OWNER, "parse_audience"))   # не бросает
    assert _run(pool.fetchval(
        "SELECT COUNT(*) FROM subscription_trials WHERE owner_id=$1", OWNER)) == 1


def test_operation_bus_still_refuses_after_trial(pool):
    from services import operation_bus
    _acc(pool)
    _run(pool.execute(
        "INSERT INTO subscription_trials(owner_id,expires_at,days) "
        "VALUES($1, now()-interval '1 day', 3)", OWNER))
    with pytest.raises(operation_bus.PlanRequiredError):
        _run(operation_bus.enforce_gates(pool, OWNER, "parse_audience"))


def test_failed_then_productive_op_activates_and_sticks(pool):
    _acc(pool)
    _run(pool.execute(
        "INSERT INTO subscriptions(user_id,plan,expires_at,is_active) "
        "VALUES($1,'paid',now()+interval '30 days',true)", OWNER))

    running = _op(pool, "parse_audience", "running", label="Сбор @rival", done=2, total=10)
    r = _checklist(pool)
    assert r["activated"] is False
    st = _step(r, "first_result")
    assert st["action"] == {"kind": "operation", "param": running}
    assert "2 из 10" in st["hint"]
    _run(pool.execute("DELETE FROM operation_queue WHERE id=$1", running))

    # «Выполнено», но ни одной цели — не результат.
    _op(pool, "parse_audience", "done", result={"ok": 0, "failed": 0, "status": "done"})
    failed = _op(pool, "mass_invite", "failed", result={"ok": 0, "failed": 5},
                 error="нет прав на добавление в группу")
    r = _checklist(pool)
    assert r["activated"] is False
    st = _step(r, "first_result")
    assert st["action"] == {"kind": "operation", "param": failed}
    assert "нет прав" in st["hint"]

    good = _op(pool, "mass_invite", "partial", result={"ok": 3, "failed": 2},
               label="Инвайт в @grp")
    r = _checklist(pool)
    assert r["activated"] is True
    assert r["first_result"] == {"op_id": good, "title": "Инвайт в @grp", "ok": 3}
    rec = _run(pool.fetchrow("SELECT * FROM onboarding_progress WHERE owner_id=$1", OWNER))
    assert rec["first_result_op_id"] == good and rec["first_result_ok"] == 3
    assert rec["first_result_op_type"] == "mass_invite"

    # Липкость: аккаунт умер, операции почищены — ветеран чеклист не видит.
    _run(pool.execute("UPDATE tg_accounts SET acc_status='banned' WHERE owner_id=$1", OWNER))
    _run(pool.execute("DELETE FROM operation_queue WHERE owner_id=$1", OWNER))
    r = _checklist(pool)
    assert r["activated"] is True and r["first_result"]["op_id"] == good


def test_warmup_day_counts_as_result(pool):
    acc = _acc(pool)
    _run(pool.execute(
        "INSERT INTO subscriptions(user_id,plan,expires_at,is_active) "
        "VALUES($1,'paid',now()+interval '30 days',true)", OWNER))
    _run(pool.execute(
        "INSERT INTO account_warmup_plans(owner_id,account_id,current_day,target_days,"
        "daily_actions,status) VALUES($1,$2,1,14,5,'active')", OWNER, acc))
    r = _checklist(pool)
    assert r["activated"] is True
    assert _run(pool.fetchval(
        "SELECT first_result_op_type FROM onboarding_progress WHERE owner_id=$1",
        OWNER)) == "account_warmup"


def test_bot_account_limit_lets_trial_newcomer_add_first_account(pool):
    from bot.handlers import accounts as bot_acc
    plan, limit = _run(bot_acc._get_account_limit(pool, OWNER))
    assert limit == 1          # бесплатный, но триал ещё положен
    _acc(pool)
    plan, limit = _run(bot_acc._get_account_limit(pool, OWNER))
    assert limit > 1           # аккаунт есть → триал выдан, лимит платный
    _run(pool.execute("UPDATE subscriptions SET expires_at=now()-interval '1 hour' "
                      "WHERE user_id=$1", OWNER))
    from bot.utils.subscription import invalidate_plan_cache
    invalidate_plan_cache(OWNER)
    plan, limit = _run(bot_acc._get_account_limit(pool, OWNER))
    assert limit == 0          # триал использован — обычный замок
