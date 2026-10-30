"""Прогрев обязан уважать общие паузы флота — паузу в базе и карантин пульса.

ЧТО ЛОМАЛОСЬ. Из всех потребителей живых аккаунтов прогрев был единственным, кто
не спрашивал ни про одну из двух ОБЩИХ пауз. Массовая операция ловила PEER_FLOOD:
`flood_engine.apply_cooldown` ставил `tg_accounts.cooldown_until`, а в
`restriction_events` ложилась запись, и риск-пульс уводил аккаунт в карантин. С
этой секунды аккаунт не брала ни одна операция — а цикл прогрева в ближайший час
поднимал ту же самую сессию и шёл вступать в каналы, ставить реакции и писать
комментарии: ровно то поведение, за которое Telegram только что ограничил. Своё
правило «не добивать флагнутый аккаунт» прогрев соблюдал только для ограничений,
пойманных им самим, внутри своей же сессии.

Проверяем три слоя: смысл гейта (`risk_hold`), то, что ОБА пути прогрева —
одиночный план и мультиаккаунтная сессия — спрашивают его ДО коннекта, и то, что
пропуск не немой (у причины есть русская подпись и подсказка на экране).
"""
from __future__ import annotations

import ast
import datetime
import inspect
import os

import pytest

from services import account_warmer as aw
from services import warmup_status

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")

UTC = datetime.timezone.utc


class _Pool:
    """Заглушка пула: отдаёт заданный cooldown_until и заданный карантин."""

    def __init__(self, cooldown=None, quarantined=(), boom=False):
        self._cooldown = cooldown
        self._quarantined = set(quarantined)
        self._boom = boom
        self.queries: list[str] = []

    async def fetchrow(self, q, *a):
        self.queries.append(q)
        if self._boom:
            raise RuntimeError("база моргнула")
        return {"cooldown_until": self._cooldown}

    async def fetch(self, q, *a):
        self.queries.append(q)
        if self._boom:
            raise RuntimeError("база моргнула")
        ids = a[0] if a else []
        return [{"account_id": i} for i in ids if i in self._quarantined]

    async def execute(self, q, *a):
        self.queries.append(q)
        return "UPDATE 1"


def _future(**kw):
    return datetime.datetime.now(UTC) + datetime.timedelta(**kw)


def _past(**kw):
    return datetime.datetime.now(UTC) - datetime.timedelta(**kw)


# ── Смысл гейта ──────────────────────────────────────────────────────────────

async def test_live_cooldown_holds_the_warmup():
    """Пауза в базе ещё не истекла — греть нельзя."""
    assert await aw.risk_hold(_Pool(cooldown=_future(hours=3)), 7) == "cooldown"


async def test_expired_cooldown_does_not_hold():
    """Истёкшая пауза больше ничего не значит — прогрев идёт."""
    assert await aw.risk_hold(_Pool(cooldown=_past(hours=3)), 7) is None


async def test_no_cooldown_at_all_does_not_hold():
    assert await aw.risk_hold(_Pool(cooldown=None), 7) is None


async def test_naive_timestamp_is_read_as_utc():
    """Колонка без таймзоны не должна читаться как «паузы нет»."""
    naive = (datetime.datetime.now(UTC) + datetime.timedelta(hours=3)).replace(tzinfo=None)
    assert await aw.risk_hold(_Pool(cooldown=naive), 7) == "cooldown"


async def test_quarantine_holds_the_warmup():
    """Карантин риск-пульса держит прогрев так же, как держит любую операцию."""
    assert await aw.risk_hold(_Pool(quarantined=[7]), 7) == "quarantine"


async def test_healthy_account_is_not_held():
    assert await aw.risk_hold(_Pool(quarantined=[8]), 7) is None


async def test_the_gate_fails_open():
    """Как и сам пульс: сбой чтения не должен блокировать прогрев."""
    assert await aw.risk_hold(_Pool(boom=True), 7) is None


async def test_no_pool_and_no_account_are_not_a_hold():
    assert await aw.risk_hold(None, 7) is None
    assert await aw.risk_hold(_Pool(cooldown=_future(hours=3)), 0) is None


async def test_junk_account_id_does_not_explode():
    assert await aw.risk_hold(_Pool(), "не число") is None


async def test_the_gate_reads_the_pause_from_the_database():
    """Паузу, поставленную соседней репликой, память процесса не знает."""
    pool = _Pool(cooldown=_future(hours=1))
    await aw.risk_hold(pool, 7)
    assert any("cooldown_until" in q and "tg_accounts" in q for q in pool.queries), (
        "гейт не читает tg_accounts.cooldown_until — значит, видит только "
        "паузы своего процесса"
    )


# ── Оба пути прогрева спрашивают гейт ДО коннекта ────────────────────────────

def _func(name):
    """Границы функции берём по ast, а не окном фиксированной длины."""
    src = inspect.getsource(aw)
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"функция {name} не найдена в account_warmer")


def _first_call_line(node, names):
    out = None
    for n in ast.walk(node):
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        label = f.id if isinstance(f, ast.Name) else (f.attr if isinstance(f, ast.Attribute) else "")
        if label in names:
            out = n.lineno if out is None else min(out, n.lineno)
    return out


@pytest.mark.parametrize("fn", ["_run_daily_warmup_impl", "_run_warmup_session_impl"])
def test_both_warmup_paths_ask_the_gate(fn):
    node = _func(fn)
    assert _first_call_line(node, {"risk_hold"}) is not None, (
        f"{fn} не спрашивает risk_hold: аккаунт под паузой или в карантине "
        f"всё равно будет разогрет — той же сессией, которую Telegram только "
        f"что ограничил"
    )


@pytest.mark.parametrize("fn", ["_run_daily_warmup_impl", "_run_warmup_session_impl"])
def test_the_gate_comes_before_the_connect(fn):
    # Границей берём подъём клиента, а не чтение строки из базы: читать строку
    # аккаунта безопасно, а _make_client/connect — это уже трафик в Telegram.
    node = _func(fn)
    gate = _first_call_line(node, {"risk_hold"})
    connect = _first_call_line(node, {"_make_client", "connect"})
    assert gate is not None
    assert connect is None or gate < connect, (
        f"{fn} спрашивает гейт ПОСЛЕ подъёма клиента — сессия уже поднята, "
        f"а значит трафик по флагнутому аккаунту уже пошёл"
    )


# ── Пропуск не немой ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("reason", ["cooldown", "quarantine"])
def test_the_skip_reason_has_a_russian_label(reason):
    label = warmup_status.skip_label(reason)
    assert label and label != reason, (
        f"причина «{reason}» показалась бы владельцу латиницей — "
        f"он не читает по-английски"
    )
    assert not any("a" <= c.lower() <= "z" for c in label.replace("Telegram", "")), (
        f"в подписи «{label}» остался английский"
    )
    hint = warmup_status._SKIP_HINT.get(reason, "")
    assert hint, f"у причины «{reason}» нет подсказки — экран скажет «пропущен» без причины"


@pytest.mark.parametrize("reason", ["cooldown", "quarantine"])
def test_the_pause_is_not_a_dead_end(reason):
    """Обе паузы проходят сами — план не нужно объявлять застрявшим."""
    assert reason not in warmup_status._TERMINAL_SKIPS, (
        f"«{reason}» помечен как терминальный пропуск — экран позовёт владельца "
        f"чинить то, что рассосётся само"
    )


# ── Живая база: карантинный аккаунт реально не греется ───────────────────────

@pytest.mark.skipif(not DSN, reason="нужен живой Postgres: INFRAGRAM_TEST_DSN")
def test_quarantined_account_is_not_warmed_postgres(monkeypatch):
    import asyncio
    import asyncpg

    from services import account_manager
    from services import op_worker as w

    OWNER = 825011

    async def go():
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=3)
        _orig_pool = w._db_pool
        w.init_op_worker_pool(pool)

        connected: list[int] = []

        def _boom(*a, **k):
            connected.append(1)
            raise AssertionError("прогрев поднял клиента на аккаунте под карантином")

        monkeypatch.setattr(account_manager, "_make_client", _boom)

        async def _cl():
            await pool.execute(
                "DELETE FROM restriction_events WHERE owner_id=$1", OWNER)
            await pool.execute(
                "DELETE FROM account_warmup_plans WHERE account_id IN "
                "(SELECT id FROM tg_accounts WHERE owner_id=$1)", OWNER)
            await pool.execute("DELETE FROM tg_accounts WHERE owner_id=$1", OWNER)

        try:
            await _cl()
            acc = int(await pool.fetchval(
                "INSERT INTO tg_accounts(owner_id,phone,session_str,is_active,acc_status,"
                "added_at) VALUES($1,$2,'sess',TRUE,'active', now() - interval '10 days') "
                "RETURNING id", OWNER, "+79000009011"))
            # Прогрев — по подписке (гейт тарифа перед каждым днём, D3):
            # у владельца стенда она настоящая, в базе.
            await pool.execute(
                "INSERT INTO subscriptions(user_id, plan, expires_at, is_active) "
                "VALUES($1,'paid', now() + INTERVAL '30 days', true) "
                "ON CONFLICT (user_id) DO UPDATE SET is_active=true, "
                "expires_at=now() + INTERVAL '30 days'", OWNER)
            from bot.utils.subscription import invalidate_plan_cache
            invalidate_plan_cache(OWNER)
            plan_id = int(await pool.fetchval(
                "INSERT INTO account_warmup_plans(account_id,owner_id,current_day,target_days,"
                "daily_actions,status,last_action_at) VALUES($1,$2,3,14,5,'active', NULL) "
                "RETURNING id", acc, OWNER))
            # Операция словила подтверждённое ограничение — пульс уводит в карантин.
            await pool.execute(
                "INSERT INTO restriction_events(owner_id,account_id,event_type,severity) "
                "VALUES($1,$2,'account_restricted','critical')", OWNER, acc)

            async with w._accounts_lock:
                w._accounts_in_use.clear()

            plan = {"id": plan_id, "account_id": acc, "owner_id": OWNER,
                    "current_day": 3, "daily_actions": 5, "target_days": 14}
            await aw.run_daily_warmup(pool, plan)

            assert not connected, "клиент всё-таки поднялся на карантинном аккаунте"
            reason = await pool.fetchval(
                "SELECT last_skip_reason FROM account_warmup_plans WHERE id=$1", plan_id)
            assert reason == "quarantine", (
                f"причина пропуска не записана ({reason!r}) — на экране план "
                f"остался бы «активным», и владелец не узнал бы, почему он стоит")

            # Владелец снял риск кнопкой — прогрев обязан вернуться.
            await pool.execute(
                "UPDATE tg_accounts SET risk_cleared_at = now() WHERE id=$1", acc)
            assert await aw.risk_hold(pool, acc) is None, (
                "после сброса риска владельцем прогрев всё ещё держится — "
                "кнопка «Сбросить кулдаун» не работает для прогрева")

            await _cl()
        finally:
            async with w._accounts_lock:
                w._accounts_in_use.clear()
            w._db_pool = _orig_pool
            await pool.close()

    asyncio.new_event_loop().run_until_complete(go())
