"""Безопасность сессий: одна auth-key сессия НИКОГДА не коннектится из двух мест.

Раньше фоновые циклы (призрак/прогрев/пре-флайт) выбирали аккаунты по снимку
`in_operation=FALSE` в БД, но не захватывали их в in-memory арбитре op_worker —
операция могла параллельно открыть ту же сессию → Telegram видит вход с двух
мест и УНИЧТОЖАЕТ auth-key (AUTH_KEY_DUPLICATED, безвозвратно).

Фикс: атомарный захват try_claim_account(s) под единым `_accounts_lock` —
общий арбитр `_accounts_in_use` для операций И фоновых сессий.
"""
from __future__ import annotations

import asyncio
import os

import pytest

import services.op_worker as ow

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def _func_src(rel: str, name: str) -> str:
    """Тело функции по границам AST, а не срез фиксированной длины.

    Окно `src[i:i+3000]` молча перестаёт проверять, как только код сдвинулся: у
    отрицательного утверждения («такого вызова тут больше нет») пустое окно
    делает тест зелёным навсегда. Границы AST этого не допускают.
    """
    import ast
    src = open(os.path.join(ROOT, rel), encoding="utf-8").read()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return "\n".join(src.split("\n")[node.lineno - 1:node.end_lineno])
    raise AssertionError(f"{name} не найдена в {rel}")



@pytest.fixture(autouse=True)
def _isolate():
    saved_pool = ow._db_pool
    saved_in_use = set(ow._accounts_in_use)
    saved_locks = dict(ow._operation_account_locks)
    ow._db_pool = None  # без БД — чистая in-memory проверка арбитра
    ow._accounts_in_use.clear()
    ow._operation_account_locks.clear()
    try:
        yield
    finally:
        ow._db_pool = saved_pool
        ow._accounts_in_use.clear()
        ow._accounts_in_use.update(saved_in_use)
        ow._operation_account_locks.clear()
        ow._operation_account_locks.update(saved_locks)


def test_try_claim_account_true_when_free_false_when_held():
    assert asyncio.run(ow.try_claim_account(42)) is True   # свободен → захвачен
    assert 42 in ow._accounts_in_use
    assert asyncio.run(ow.try_claim_account(42)) is False  # уже держим → отказ


def test_try_claim_accounts_returns_only_free_subset():
    ow._accounts_in_use.update({2, 4})  # заняты
    claimed = asyncio.run(ow.try_claim_accounts([1, 2, 3, 4, 5]))
    assert set(claimed) == {1, 3, 5}                 # только свободные
    assert ow._accounts_in_use == {1, 2, 3, 4, 5}    # все теперь заняты


def test_ghost_claim_blocks_operation_claim_same_account():
    # призрак захватил аккаунт 7 …
    assert asyncio.run(ow.try_claim_account(7)) is True
    # … операция НЕ должна получить его в свою долю
    claimed = asyncio.run(ow._claim_available_accounts(900, [{"id": 7}, {"id": 8}]))
    ids = [a["id"] for a in claimed]
    assert 7 not in ids and 8 in ids


def test_operation_claim_blocks_ghost_claim_same_account():
    # операция захватила аккаунт 9 …
    asyncio.run(ow._claim_available_accounts(901, [{"id": 9}]))
    # … призрак/прогрев не может открыть на нём вторую сессию
    assert asyncio.run(ow.try_claim_account(9)) is False


def test_release_returns_account_to_arbiter():
    asyncio.run(ow.try_claim_account(11))
    asyncio.run(ow.release_accounts([11]))
    assert 11 not in ow._accounts_in_use
    assert asyncio.run(ow.try_claim_account(11)) is True  # снова захватываем


# ─── Wiring: живые сессии проходят через арбитр ────────────────────────────────

def test_ghost_engine_claims_and_releases():
    src = open(os.path.join(ROOT, "services", "ghost_engine.py"), encoding="utf-8").read()
    assert "try_claim_account" in src
    assert "release_accounts([account_id])" in src
    # захват ДО открытия клиента
    i_claim = src.index("try_claim_account")
    i_client = src.index("_make_client(session")
    assert i_claim < i_client, "захват должен предшествовать открытию сессии"
    # освобождение в finally
    assert "finally:" in src[i_client:]


def test_warmer_uses_atomic_claim_not_unconditional_mark():
    src = open(os.path.join(ROOT, "services", "account_warmer.py"), encoding="utf-8").read()
    # оба пути прогрева (одиночный план + мультисессия) — через атомарный захват
    assert src.count("try_claim_account") >= 2
    # больше не безусловный mark_accounts_in_use (он допускал двойную сессию)
    assert "await _opw.mark_accounts_in_use([account_id])" not in src


def test_invite_preflight_uses_atomic_batch_claim():
    src = open(os.path.join(ROOT, "services", "invite_preflight.py"), encoding="utf-8").read()
    assert "try_claim_accounts" in src
    # старый небезопасный check-then-mark убран
    assert "if _opw.is_account_in_use(int(a[\"id\"]))" not in src


def test_background_connectors_claim_atomically_and_release():
    # Все фоновые циклы, ОТКРЫВАЮЩИЕ живые сессии, теперь атомарно захватывают
    # аккаунт и освобождают его (раньше только читали снимок is_account_in_use).
    for rel in ("services/activity_engine.py", "services/content_mesh.py",
                "services/keyword_watcher.py"):
        src = open(os.path.join(ROOT, rel), encoding="utf-8").read()
        assert "try_claim_account" in src, f"{rel}: нет атомарного захвата"
        assert "release_accounts(" in src, f"{rel}: нет освобождения аккаунта"
        # снимочная проверка-без-захвата убрана (она допускала гонку)
        assert "is_account_in_use" not in src, \
            f"{rel}: остался небезопасный снимок is_account_in_use вместо захвата"


# ─── Межпроцессный арбитр (аренда в БД) ───────────────────────────────────────
# Дизъюнктность двух реплик проверяется на живом Postgres в
# tests/test_account_lease_postgres.py. Эти проверки работают ВСЕГДА (без БД) —
# иначе регресс арбитра проехал бы CI незамеченным, ведь тот файл пропускается
# без INFRAGRAM_TEST_DSN.

def test_both_claim_paths_go_through_db_arbiter():
    src = open(os.path.join(ROOT, "services", "op_worker.py"), encoding="utf-8").read()
    for fn in ("try_claim_accounts", "_claim_available_accounts"):
        i = src.index(f"async def {fn}(")
        body = src[i:i + 3000]
        assert "_db_claim(" in body, (
            f"{fn} не проходит через межпроцессный арбитр — вторая реплика "
            f"возьмёт те же сессии (AUTH_KEY_DUPLICATED)"
        )


def test_db_claim_fails_closed_and_rolls_back_local_set():
    """Сбой БД: захвата нет, локальный фильтр откатывается (иначе аккаунт
    навсегда «занят» в памяти, не будучи занятым на деле)."""
    class _Broken:
        async def fetch(self, *_a, **_k):
            raise RuntimeError("БД недоступна")
        async def execute(self, *_a, **_k):
            raise RuntimeError("БД недоступна")

    saved = ow._db_pool
    ow._db_pool = _Broken()
    try:
        assert asyncio.run(ow.try_claim_accounts([21, 22])) == []
        assert ow._accounts_in_use == set(), "локальный набор не откатился"
    finally:
        ow._db_pool = saved


def test_partial_grant_rolls_back_only_ungranted():
    """БД выдала часть — в памяти остаётся ровно выданное."""
    class _Partial:
        async def fetch(self, _q, ids, *_a):
            return [{"id": int(i)} for i in ids if int(i) != 31]  # 31 занят соседом
        async def execute(self, *_a, **_k):
            return "UPDATE"

    saved = ow._db_pool
    ow._db_pool = _Partial()
    try:
        got = asyncio.run(ow.try_claim_accounts([30, 31, 32]))
        assert set(got) == {30, 32}
        assert ow._accounts_in_use == {30, 32}, "невыданный аккаунт остался занятым"
    finally:
        ow._db_pool = saved


def test_unconditional_mark_is_gone_for_good():
    """Храповик: безусловной пометки «эти сессии мои» в продукте больше нет.

    mark_accounts_in_use() не спрашивал арбитра и пропускал в работу аккаунт,
    уже занятый другой операцией/репликой. Все пути переведены на отказной
    try_claim_account(s). Если функция вернётся — вернётся и класс аварии.
    """
    assert not hasattr(ow, "mark_accounts_in_use"), (
        "mark_accounts_in_use воскрешён — это безусловный захват без арбитража"
    )
    for rel in ("services/op_worker.py", "bot/handlers/strike.py"):
        src = open(os.path.join(ROOT, rel), encoding="utf-8").read()
        calls = [ln for ln in src.split("\n")
                 if "mark_accounts_in_use(" in ln and not ln.strip().startswith("#")]
        assert not calls, f"{rel}: остался безусловный вызов: {calls[:2]}"


def _func_source(path: str, name: str) -> str:
    """Точное тело функции по границам AST.

    Окно фиксированной длины тут не годится: `finally: release_accounts(...)`
    лежит в сотнях строк от начала исполнителя и в окно не попадал — проверка
    «освобождает то, что захватил» молча меряла не то.
    """
    import ast
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return "\n".join(src.split("\n")[node.lineno - 1:node.end_lineno])
    raise AssertionError(f"{name} не найдена в {path}")


def _calls(body: str, needle: str) -> list[str]:
    """Строки с вызовом, без комментариев (упоминание в комментарии — не вызов)."""
    return [ln for ln in body.split("\n")
            if needle in ln and not ln.strip().startswith("#")]


def test_every_executor_claim_is_refusable():
    """Исполнители, берущие флот, обязаны УМЕТЬ ОТКАЗАТЬ, а не помечать вслепую."""
    path = os.path.join(ROOT, "services", "op_worker.py")
    for fn in ("_exec_bulk_create_channels_multi", "_exec_bot_factory_multi",
               "_exec_bulk_chan_exec"):
        body = _func_source(path, fn)
        assert _calls(body, "try_claim_accounts("), f"{fn}: захват без арбитража"
        assert "claimed_ids" in body, f"{fn}: не сузился до захваченных аккаунтов"
        assert _calls(body, "release_accounts(claimed_ids)"), (
            f"{fn}: освобождает НЕ то, что захватил"
        )


def test_strike_handler_claims_atomically_not_check_then_mark():
    """Проверка-снимок + пометка — это TOCTOU; должен быть один захват."""
    src = open(os.path.join(ROOT, "bot", "handlers", "strike.py"), encoding="utf-8").read()
    assert _calls(src, "try_claim_account(")
    assert not _calls(src, "is_account_in_use("), (
        "снимок is_account_in_use виден только в СВОЁМ процессе и допускает гонку"
    )


def test_every_direct_session_executor_claims():
    """Храповик на весь класс: любой исполнитель операции, который САМ открывает
    Telethon-клиента, обязан захватить аккаунт.

    Так были найдены 4 дыры (leave_all_chats, read_all_dialogs,
    delete_private_dialogs, delete_contacts): они открывали живую сессию на
    аккаунте, который в этот момент мог вести массовую операцию или прогрев.
    Новый исполнитель не должен уметь повторить это молча.
    """
    import ast
    path = os.path.join(ROOT, "services", "op_worker.py")
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    lines = src.split("\n")
    claim_markers = ("try_claim_account", "_claim_available_accounts",
                     "_claim_single_account")

    checked, offenders = 0, []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.name.startswith("_exec_"):
            continue
        body = "\n".join(lines[node.lineno - 1:node.end_lineno])
        if "_make_client(" not in body and "connect_client(" not in body:
            continue                      # сессию открывает движок ниже — не наш случай
        checked += 1
        if not any(m in body for m in claim_markers):
            offenders.append(node.name)

    # Самопроверка измерителя: если исполнителей вдруг не нашлось, тест зелёный
    # «просто так» — это сломанный детектор, а не чистый код.
    assert checked >= 4, (
        f"детектор нашёл всего {checked} исполнителей с прямым открытием сессии — "
        f"похоже, сломан он, а не код"
    )
    assert not offenders, (
        "исполнитель открывает живую сессию без захвата аккаунта "
        f"(вторая сессия на одном auth-key → AUTH_KEY_DUPLICATED): {offenders}"
    )


def test_single_account_prologue_is_shared_not_copied():
    """Пролог одиночных исполнителей — один на всех.

    Он был скопирован в каждый, и во ВСЕХ копиях не хватало захвата: правка в
    одном месте не чинила остальные. Теперь общий _claim_single_account.
    """
    src = open(os.path.join(ROOT, "services", "op_worker.py"), encoding="utf-8").read()
    assert "async def _claim_single_account(" in src
    # старый скопированный пролог «достать аккаунт и сразу открыть клиента» —
    # больше не повторяется по файлу
    copies = src.count('return {"status": "failed", "summary": "⚠️ account_id не указан"}')
    assert copies <= 1, (
        f"пролог одиночного исполнителя снова размножен ({copies} копий) — "
        f"захват опять разъедется по копиям"
    )


def test_release_is_scoped_to_own_lease():
    """Освобождение обязано скоупиться владельцем аренды: иначе реплика снимает
    защиту с живой сессии соседа."""
    body = _func_src("services/op_worker.py", "_db_release")
    assert "op_lease_owner = $2" in body


def test_strike_claims_atomically_and_works_only_on_claimed():
    body = _func_src("services/strike_engine.py", "staggered_strike")
    assert "try_claim_accounts" in body
    # больше не безусловный mark всех аккаунтов плана
    assert "await _opw.mark_accounts_in_use(_claimed_ids)" not in body
    # страйк работает только с реально захваченными сессиями
    assert "plan.accounts = [a for a in plan.accounts" in body


def test_all_session_executors_claim_no_exceptions():
    """Финальный храповик: НИ ОДИН исполнитель операции не работает сессией
    аккаунта без захвата. Покрытие 100% — новые исключения заводить нельзя.

    Ловит и тех, кто клиента не создаёт сам, а делегирует в account_manager
    (create_channel, promote_to_admin, post_to_channel, join_channel …): именно
    так 14 дыр пережили первую волну правок.
    """
    import ast
    path = os.path.join(ROOT, "services", "op_worker.py")
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    lines = src.split("\n")
    claim_markers = ("try_claim_account", "try_claim_accounts",
                     "_claim_available_accounts", "_claim_single_account")
    checked, offenders = 0, []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.name.startswith("_exec_"):
            continue
        body = "\n".join(lines[node.lineno - 1:node.end_lineno])
        if "session_str" not in body:
            continue
        if not any(k in body for k in ("account_manager.", "_make_client(", "connect_client(")):
            continue
        # аккаунты приходят параметром → захватил вызывающий (напр. _exec_bulk_join_inner)
        if "accounts" in [a.arg for a in node.args.args]:
            continue
        checked += 1
        if not any(m in body for m in claim_markers):
            offenders.append(node.name)
    assert checked >= 30, (
        f"детектор нашёл всего {checked} исполнителей — сломан он, а не код"
    )
    assert not offenders, (
        "исполнитель работает сессией аккаунта без захвата "
        f"(две сессии на одном auth-key → AUTH_KEY_DUPLICATED): {offenders}"
    )
