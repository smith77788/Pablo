"""Второе нажатие «Передать» не гоняет аккаунты за уже отданными подарками.

ПОЧЕМУ ДЕДУП ШИНЫ ЗДЕСЬ БЕССИЛЕН. План передачи создаётся на каждое нажатие
новый, значит и params операции (`{"plan_id": …}`) каждый раз новые — два
сабмита не сольются никогда. Это тот же класс, что «свежая строка в params»,
и в самой баноопасной его части: передача подарка необратима и тратит звёзды.

ЧТО БЫЛО В БОТЕ. Кнопка «Передать» выбирала ВСЕ передаваемые подарки
выбранных аккаунтов без всякой проверки занятости. После успешной передачи
строка инвентаря не менялась, поэтому второй проход брал те же подарки, шёл с
аккаунтов в Телеграм и получал отказ: полный круг самых заметных снаружи
обращений на работу, которой заведомо нет. У мини-аппа проверка была — своя,
вторым экземпляром того же правила.

ЧТО ЗАКРЕПЛЕНО. Правило одно для обоих путей
(`GiftTransferService.busy_inventory_ids`), запрос проверяется на живой базе, а
постановка операции идёт под тем же замком владельца, что и выбор: иначе
второй тап успевает посмотреть, пока операции ещё нет.
"""
from __future__ import annotations

import ast
import asyncio
import os
import pathlib
import re

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
ROOT = pathlib.Path(__file__).resolve().parent.parent
OWNER = 993621

_LOOP: "asyncio.AbstractEventLoop | None" = None


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

    async def _mk():
        return await asyncpg.create_pool(DSN, min_size=1, max_size=2)

    try:
        p = _run(_mk())
    except Exception as exc:
        pytest.skip(f"Postgres по INFRAGRAM_TEST_DSN недоступен: {str(exc)[:120]}")
    _run(p.execute(
        "INSERT INTO platform_users(user_id) VALUES($1) ON CONFLICT DO NOTHING",
        OWNER))
    yield p
    _run(_cleanup(p))
    _run(p.close())


async def _cleanup(p):
    await p.execute("DELETE FROM gift_transfer_plans WHERE owner_id=$1", OWNER)
    await p.execute("DELETE FROM gift_inventory WHERE owner_id=$1", OWNER)
    await p.execute("DELETE FROM operation_queue WHERE owner_id=$1", OWNER)
    await p.execute("DELETE FROM tg_accounts WHERE owner_id=$1", OWNER)


@pytest.fixture(autouse=True)
def _clean(pool):
    _run(_cleanup(pool))
    yield


def _account(pool) -> int:
    return int(_run(pool.fetchval(
        "INSERT INTO tg_accounts(owner_id, phone, session_str) "
        "VALUES($1,$2,$3) RETURNING id",
        OWNER, f"+7000{OWNER}", "stand-session")))


def _gift(pool, acc_id: int, gift_id: str = "9001") -> int:
    return int(_run(pool.fetchval(
        "INSERT INTO gift_inventory(owner_id, account_id, gift_id, gift_type, "
        "                           is_transferable, last_seen_at) "
        "VALUES($1,$2,$3,'Star',TRUE, now()) RETURNING id",
        OWNER, acc_id, gift_id)))


def _plan_with_item(pool, acc_id: int, inv_id: int, *, item_status: str,
                    transferred_at_sql: str = "NULL") -> int:
    plan_id = int(_run(pool.fetchval(
        "INSERT INTO gift_transfer_plans(owner_id, recipient_username, "
        "   recipient_user_id, recipient_name, payment_source, status) "
        "VALUES($1,'friend',0,'friend','stars','queued') RETURNING id", OWNER)))
    _run(pool.execute(
        "INSERT INTO gift_transfer_items(plan_id, inventory_id, account_id, "
        "   gift_id, gift_type, status, transferred_at) "
        f"VALUES($1,$2,$3,'9001','Star',$4,{transferred_at_sql})",
        plan_id, inv_id, acc_id, item_status))
    return plan_id


def _live_op(pool, plan_id: int) -> int:
    return int(_run(pool.fetchval(
        "INSERT INTO operation_queue(owner_id, op_type, params, status) "
        "VALUES($1,'gift_transfer',$2::jsonb,'running') RETURNING id",
        OWNER, f'{{"plan_id": {plan_id}}}')))


def _busy(pool, ids):
    from services.gift_transfer import GiftTransferService

    return _run(GiftTransferService.busy_inventory_ids(pool, OWNER, ids))


def test_a_gift_in_a_live_transfer_is_busy(pool):
    acc = _account(pool)
    inv = _gift(pool, acc)
    plan = _plan_with_item(pool, acc, inv, item_status="pending")
    _live_op(pool, plan)
    assert _busy(pool, [inv]) == {inv}, (
        "подарок, который уже едет получателю, попадёт во второй план — "
        "аккаунты пойдут в Телеграм за ним второй раз")


def test_an_abandoned_plan_does_not_freeze_the_gift_forever(pool):
    """Запрет обязан сниматься сам: иначе брошенный план запретит подарок навсегда.

    План мог остаться с целями 'pending', если операция не встала или её
    отменили. Без привязки к ЖИВОЙ операции владелец не смог бы передать этот
    подарок уже никогда и не узнал бы, почему.
    """
    acc = _account(pool)
    inv = _gift(pool, acc)
    _plan_with_item(pool, acc, inv, item_status="pending")   # операции нет
    assert _busy(pool, [inv]) == set(), (
        "брошенный план держит подарок занятым навсегда")


def test_a_gift_transferred_after_the_scan_is_busy(pool):
    """Строка инвентаря после передачи не меняется — поэтому смотрим на журнал."""
    acc = _account(pool)
    inv = _gift(pool, acc)
    _plan_with_item(pool, acc, inv, item_status="transferred",
                    transferred_at_sql="now()")
    assert _busy(pool, [inv]) == {inv}, (
        "уже отданный подарок снова попадёт в план: круг обращений с "
        "аккаунтов ради отказа Телеграма")


def test_a_gift_seen_again_after_the_transfer_is_free(pool):
    """Получатель вернул подарок, скан увидел его на месте — позиция свободна."""
    acc = _account(pool)
    inv = _gift(pool, acc)
    _plan_with_item(pool, acc, inv, item_status="transferred",
                    transferred_at_sql="now() - INTERVAL '1 day'")
    assert _busy(pool, [inv]) == set(), (
        "подарок, вернувшийся к владельцу, передать больше нельзя")


def test_another_owners_gift_is_not_leaked(pool):
    """Запрос ходит по чужим планам только через owner_id — иначе это утечка."""
    acc = _account(pool)
    inv = _gift(pool, acc)
    plan = _plan_with_item(pool, acc, inv, item_status="pending")
    _live_op(pool, plan)
    from services.gift_transfer import GiftTransferService

    other = _run(GiftTransferService.busy_inventory_ids(pool, OWNER + 1, [inv]))
    assert other == set(), "чужая занятость видна другому владельцу"


def test_no_ids_asks_the_database_nothing(pool):
    assert _busy(pool, []) == set()


def test_both_paths_use_the_one_door():
    """Второй экземпляр правила про необратимое разойдётся с первым."""
    impl = []
    for sub in ("services", "bot"):
        for path in sorted((ROOT / sub).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            src = path.read_text(encoding="utf-8")
            if "FROM gift_transfer_items" in src and "operation_queue" in src:
                rel = path.relative_to(ROOT).as_posix()
                if re.search(r"it\.status\s*=\s*'transferred'", src):
                    impl.append(rel)
    assert impl == ["services/gift_transfer.py"], (
        f"правило занятости подарка живёт не в одном месте: {impl}")


def test_the_bot_submits_under_the_same_lock():
    """Отпустить замок до постановки — оставить ровно ту щель, против которой он.

    Дверь видит занятый подарок по ЖИВОЙ операции на его план. Пока операции
    нет, второй тап видит подарки свободными, поэтому выбор, создание плана и
    постановка обязаны идти под одним замком владельца.
    """
    src = (ROOT / "bot" / "handlers" / "gift_transfer.py").read_text(
        encoding="utf-8")
    tree = ast.parse(src)
    # Мерка по смыслу: берём обработчик, который СОЗДАЁТ план и ставит по нему
    # операцию, а не функцию с определённым именем — переименование её не
    # обманет. Повтор неудачных (`cb_retry_failed`) сюда не попадает намеренно:
    # он работает по существующему плану, params у него стабильные, и двойной
    # тап по нему сливает дедуп шины.
    fns = [n for n in ast.walk(tree)
           if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
           and "create_plan(" in (ast.get_source_segment(src, n) or "")
           and "_op_bus.submit(" in (ast.get_source_segment(src, n) or "")]
    assert len(fns) == 1, [f.name for f in fns]
    fn = fns[0]
    blocks = [n for n in ast.walk(fn) if isinstance(n, ast.AsyncWith)
              and "pg_advisory_xact_lock" in (ast.get_source_segment(src, n) or "")]
    assert len(blocks) == 1, "замка владельца в кнопке «Передать» нет"
    body = ast.get_source_segment(src, blocks[0]) or ""
    assert "busy_inventory_ids" in body, "проверка занятости вне замка"
    assert "create_plan" in body, "план создаётся вне замка"
    assert re.search(r"_op_bus\.submit\(", body), (
        "постановка операции вне замка: второй тап успеет посмотреть, пока "
        "операции ещё нет")
