"""Ни один исполнитель не берёт аккаунт в работу без захвата.

ЦЕНА ПРОМАХА. Операции владельца идут ПАРАЛЛЕЛЬНО (до
`OP_MAX_PARALLEL_PER_OWNER` задач на владельца), а одна auth-key сессия,
подключённая из двух мест одновременно, получает от Телеграма
AUTH_KEY_DUPLICATED — аккаунт теряется не на время, а насовсем. Поэтому
единственный арбитр занятости (`_accounts_in_use` + аренда в БД) обязателен
для КАЖДОГО исполнителя, который открывает сессию: захват через
`try_claim_accounts` / `_claim_available_accounts`, освобождение — в `finally`.

ЧТО ПОКАЗАЛА ПЕРЕПИСЬ. 54 исполнителя работают с `session_str`; захват есть у
всех. Единственное исключение — внутренняя половина `_exec_bulk_join`:
захватывает и освобождает обёртка, а `_exec_bulk_join_inner` получает уже
захваченные аккаунты аргументом. Освобождение в `finally` есть у каждого, кто
берёт низкоуровневый захват. То есть находок ноль, и эта проверка — не
исправление, а замок: следующий исполнитель, написанный без захвата, не
доедет до продакшена молча.

ПРО ИЗМЕРИТЕЛЬ. Первая версия давала то 0 находок (искала ключ
`session_string`, которого в коде нет), то 32 из 54 (знала только про один из
двух захватов). Ни одна цифра не была правдой о продукте. Поэтому ниже —
самопроверка на заведомо больном и заведомо здоровом примере и проверка объёма:
перепись, которая вдруг никого не нашла, должна падать, а не зеленеть.
"""

import ast
import pathlib

OW = pathlib.Path(__file__).resolve().parent.parent / "services" / "op_worker.py"

# Обе двери к единому арбитру занятости.
CLAIMS = ("_claim_available_accounts", "_claim_single_account",
          "try_claim_accounts", "try_claim_account")

# Исключение с причиной, а не молчаливый пропуск.
WRAPPED = {"_exec_bulk_join_inner": "_exec_bulk_join"}


def _tree() -> tuple[str, ast.Module]:
    src = OW.read_text(encoding="utf-8")
    return src, ast.parse(src)


def _handlers(src: str, tree: ast.Module) -> dict[str, str]:
    out = {}
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and fn.name.startswith("_exec_"):
            out[fn.name] = ast.get_source_segment(src, fn) or ""
    return out


def _unclaimed(handlers: dict[str, str]) -> list[str]:
    bad = []
    for name, body in handlers.items():
        if "session_str" not in body:
            continue
        if any(c in body for c in CLAIMS):
            continue
        if name in WRAPPED:
            continue
        bad.append(name)
    return bad


def test_every_handler_that_opens_a_session_claims_the_account():
    src, tree = _tree()
    handlers = _handlers(src, tree)
    bad = _unclaimed(handlers)
    assert not bad, (
        "исполнитель открывает сессию аккаунта без захвата у единого арбитра — "
        "при параллельных операциях та же сессия подключится из двух мест, и "
        "Телеграм убьёт аккаунт (AUTH_KEY_DUPLICATED): " + ", ".join(sorted(bad))
    )


def test_the_census_actually_sees_the_handlers():
    """Перепись, никого не нашедшая, обязана падать, а не зеленеть."""
    src, tree = _tree()
    handlers = _handlers(src, tree)
    with_sessions = [n for n, b in handlers.items() if "session_str" in b]
    assert len(handlers) >= 60, f"исполнителей найдено {len(handlers)} — разбор сломан"
    assert len(with_sessions) >= 40, (
        f"исполнителей с сессиями найдено {len(with_sessions)} — ключ в коде "
        "переименовали, и перепись смотрит в пустоту"
    )


def test_the_measurer_tells_a_sick_handler_from_a_healthy_one():
    sick = {"_exec_probe": (
        "async def _exec_probe(pool, bot, op_id, owner_id, params):\n"
        "    for a in accounts:\n"
        "        await account_manager.post_to_channel(a['session_str'], 1, 'x')\n")}
    healthy = {"_exec_probe": (
        "async def _exec_probe(pool, bot, op_id, owner_id, params):\n"
        "    ids = await try_claim_accounts([a['id'] for a in accounts])\n"
        "    try:\n"
        "        await account_manager.post_to_channel(a['session_str'], 1, 'x')\n"
        "    finally:\n"
        "        await release_accounts(ids)\n")}
    assert _unclaimed(sick) == ["_exec_probe"], "измеритель не видит больной пример"
    assert _unclaimed(healthy) == [], "измеритель ругается на здоровый пример"


def test_the_low_level_claim_is_always_released_in_finally():
    src, tree = _tree()
    bad = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not fn.name.startswith("_exec_"):
            continue
        body = ast.get_source_segment(src, fn) or ""
        if "try_claim_account" not in body:
            continue
        released = False
        for node in ast.walk(fn):
            if isinstance(node, ast.Try) and node.finalbody:
                tail = "\n".join(ast.get_source_segment(src, s) or ""
                                 for s in node.finalbody)
                if "release_accounts" in tail:
                    released = True
        if not released:
            bad.append(fn.name)
    assert not bad, (
        "низкоуровневый захват без освобождения в finally: аккаунт останется "
        "«занят» до перезапуска процесса, и владелец будет видеть «флот занят "
        "другими операциями» на пустом флоте: " + ", ".join(sorted(bad))
    )


def test_the_allowed_exception_is_still_claimed_by_its_wrapper():
    """Разрешение держится на обёртке — проверяем, что обёртка на месте."""
    src, tree = _tree()
    handlers = _handlers(src, tree)
    for inner, outer in WRAPPED.items():
        assert inner in handlers, (
            f"{inner} больше нет — список исключений надо перечитать")
        body = handlers.get(outer, "")
        assert body, f"обёртка {outer} исчезла, а исключение для {inner} осталось"
        assert any(c in body for c in CLAIMS), (
            f"{outer} больше не захватывает аккаунты, а {inner} по-прежнему "
            "освобождён от проверки — захвата нет ни у кого")
        assert f"{inner}(" in body, (
            f"{outer} больше не вызывает {inner} — связка, на которой держится "
            "исключение, разорвана")
        releases_in_finally = False
        for fn in ast.walk(tree):
            if not (isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and fn.name == outer):
                continue
            for node in ast.walk(fn):
                if isinstance(node, ast.Try) and node.finalbody:
                    tail = "\n".join(ast.get_source_segment(src, s) or ""
                                     for s in node.finalbody)
                    if "release_accounts" in tail:
                        releases_in_finally = True
        assert releases_in_finally, (
            f"{outer} не освобождает аккаунты в finally — захваченные "
            f"{inner}-ом аккаунты залипнут занятыми")
