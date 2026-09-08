"""Храповик: исполнитель, поднимающий живую сессию, обязан захватить аккаунт.

Продукт это уже переживал (оп #112): инвайт был единственным массовым
исполнителем без захвата, прогрев подключил те же сессии параллельно, и весь
флот упал на connect с «authorization key used under two different IP addresses
simultaneously». Одна auth-key сессия, подключённая из двух мест, умирает.

Захват (`try_claim_account(s)` / `_claim_available_accounts` /
`_claim_single_account`) — единый арбитр для ВСЕХ живых сессий: операции,
прогрев, призрак, пре-флайт. Освобождение централизовано в `finally`
`_run_op_task` по `op_id`, поэтому захватывать надо тем помощником, который
регистрирует аккаунты за операцией.

Тот случай закрыли точечным тестом на инвайт (`test_invite_claims_accounts`),
а класс остался открытым: аудит нашёл ЕЩЁ 13 исполнителей, поднимавших сессии
без захвата — буст (просмотры/реакции/сторис), массовая жалоба и репорт, клон
контента, ИИ-комментарии, комплаенс-скан, массовая смена профиля, проверка
номеров (ImportContacts!), проверка здоровья, скан рекламы и скан подарков.
Два последних поднимают сессию не сами, а внутри вызываемого сервиса —
косвенность ничего не меняет.

Исключение ровно одно: `_exec_bulk_join_inner` — внутренняя часть
`_exec_bulk_join`, которая захватывает за неё.
"""
from __future__ import annotations

import ast
import pathlib
import re

_SRC = (pathlib.Path(__file__).resolve().parents[1]
        / "services" / "op_worker.py").read_text(encoding="utf-8")
_TREE = ast.parse(_SRC)

_CLAIM = re.compile(
    r"try_claim_accounts?|_claim_available_accounts|_claim_single_account")

# Захват делает вызывающий, а не сам исполнитель. Пополнять этот список можно
# только вместе с доказательством, что обёртка действительно захватывает.
_CLAIMED_BY_CALLER = {"_exec_bulk_join_inner": "_exec_bulk_join"}


def _executors(src: str):
    for fn in ast.walk(ast.parse(src)):
        if (isinstance(fn, (ast.AsyncFunctionDef, ast.FunctionDef))
                and fn.name.startswith("_exec_")):
            yield fn.name, ast.get_source_segment(src, fn) or ""


def _session_executors(src: str) -> dict[str, str]:
    """Исполнители, у которых в теле фигурирует session_str — то есть они сами
    строят клиента. Косвенные (сессию поднимает вызванный сервис) сюда не
    попадают, поэтому ниже они проверяются поимённо."""
    return {n: s for n, s in _executors(src) if "session_str" in s}


def _unclaimed(src: str) -> list[str]:
    return [n for n, s in _session_executors(src).items()
            if not _CLAIM.search(s) and n not in _CLAIMED_BY_CALLER]


# ── Сам храповик ───────────────────────────────────────────────────────────

def test_every_session_executor_claims_its_accounts():
    bad = _unclaimed(_SRC)
    assert not bad, (
        "исполнители поднимают живые сессии без захвата аккаунта — вторая "
        "сессия на том же auth-key убивает аккаунт (AUTH_KEY_DUPLICATED): "
        f"{sorted(bad)}"
    )


def test_the_ratchet_covers_the_whole_surface():
    """Сломается фильтр — тест выше позеленеет навсегда."""
    n = len(_session_executors(_SRC))
    assert n >= 40, f"проверка охватывает лишь {n} исполнителей — фильтр сломался"


def test_the_caller_of_the_only_exception_really_claims():
    """Единственное исключение держится на обёртке — проверяем, что она есть и
    действительно захватывает, а не просто числится в списке."""
    for inner, outer in _CLAIMED_BY_CALLER.items():
        body = next(s for n, s in _executors(_SRC) if n == outer)
        assert re.search(rf"\b{inner}\s*\(", body), f"{outer} больше не зовёт {inner}"
        assert _CLAIM.search(body), f"{outer} перестал захватывать за {inner}"


# ── Косвенные: сессию поднимает вызванный сервис ───────────────────────────

def test_indirect_session_executors_claim_too():
    """`session_str` в теле нет, но сервис по account_id поднимает ту же живую
    сессию — без захвата это тот же AUTH_KEY_DUPLICATED."""
    for name in ("_exec_ad_intel_scan", "_exec_gift_scan"):
        body = next(s for n, s in _executors(_SRC) if n == name)
        assert _CLAIM.search(body), f"{name} не захватывает аккаунт"


# ── Захват должен освобождаться, а не течь ─────────────────────────────────

def test_claims_are_bound_to_the_operation_for_cleanup():
    """`try_claim_account` НЕ регистрирует аккаунт за операцией, поэтому
    централизованный release по op_id его не отпустит. Внутри исполнителей
    должен использоваться помощник, привязывающий захват к op_id."""
    for name, body in _session_executors(_SRC).items():
        if name in _CLAIMED_BY_CALLER:
            continue
        if "_claim_available_accounts" in body or "_claim_single_account" in body:
            continue
        # остался голый try_claim_account — тогда обязано быть и освобождение
        if "try_claim_account" in body:
            assert "release_accounts" in body, (
                f"{name} захватывает голым try_claim_account без release — "
                f"аккаунт останется «занят» навсегда"
            )


def test_central_release_still_runs_for_every_operation():
    assert "await release_operation_accounts(op_id)" in _SRC, (
        "исчезло централизованное освобождение в finally _run_op_task — "
        "захваченные аккаунты потекут"
    )


# ── Проверка самого измерителя ─────────────────────────────────────────────

def test_probe_catches_an_unclaimed_executor():
    leaky = '''
async def _exec_thing(pool, bot, op_id, owner_id, params):
    accounts = await resource_selector.select_all_active(pool, owner_id)
    for a in accounts:
        await do(a["session_str"], a)
'''
    assert _unclaimed(leaky) == ["_exec_thing"]


def test_probe_accepts_each_claim_helper():
    for helper in ("_claim_available_accounts(op_id, accounts, owner_id)",
                   "try_claim_account(int(acc['id']))",
                   "_claim_single_account(pool, owner_id, params)"):
        ok = f'''
async def _exec_thing(pool, bot, op_id, owner_id, params):
    accounts = await sel(pool, owner_id)
    await {helper}
    await do(accounts[0]["session_str"])
'''
        assert _unclaimed(ok) == [], helper


def test_probe_ignores_executors_that_never_open_a_session():
    no_session = '''
async def _exec_thing(pool, bot, op_id, owner_id, params):
    await pool.execute("UPDATE x SET y=1")
'''
    assert _session_executors(no_session) == {}
