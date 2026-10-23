"""Инвайт: не откладывать «на завтра», когда НИ ОДИН аккаунт не подключился.

ЖАЛОБА (скриншот, операция #112): 0/457 приглашено, «Аккаунты: работали 0 из 26»,
все 26 — «не ответили (сессия/сеть)», в логе connect → «The authorization key
(session file) was used under two different IP addresses simultaneously»
(AUTH_KEY_DUPLICATED). И при этом «Осталось 457 — продолжим завтра автоматически».

Проблема: когда все аккаунты не подключились, дело НЕ в суточных лимитах — завтра те
же мёртвые/задублированные сессии упадут так же, и так до _MAX_INVITE_CHAIN дней
холостых операций. Продолжение = ложная надежда, а причина (чинить сессии/прокси) не
показана.

Фикс поверх уже существующего честного учёта (_used_accounts/_idle_other): собираем
ТЕКСТ ошибок подключения (_noconnect_errs), выводим _all_failed_connect (никто не
сработал + были сбои) → продолжение НЕ планируем, распознаём AUTH_KEY_DUPLICATED и
даём действие (аккаунт залогинен где-то ещё / нет прокси → релог).
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKER = (ROOT / "services" / "op_worker.py").read_text(encoding="utf-8")


def _exec_body() -> str:
    m = re.search(r"async def _exec_mass_invite\(.*?(?=\nasync def )", WORKER, re.DOTALL)
    assert m
    return m.group(0)


def test_captures_connect_error_text_in_both_failure_paths():
    body = _exec_body()
    assert "_noconnect_errs" in body, "нужно собирать ТЕКСТ ошибок подключения (не только счётчик)"
    # заполняется и при исключении батча, и при attempted==0
    assert body.count("_noconnect_errs.append") >= 2, (
        "сбой подключения должен фиксироваться и при исключении, и при attempted==0"
    )


def test_no_continuation_when_all_accounts_failed_connect():
    body = _exec_body()
    assert "_all_failed_connect" in body, "нужен признак «никто не подключился»"
    assert "(len(_used_accounts) == 0) and bool(_noconnect_errs)" in body, (
        "_all_failed_connect = ни один не сработал И были сбои подключения"
    )
    # Решение переехало в чистую функцию — признак передаётся ей явно, а сам
    # запрет проверяем поведением, а не расположением строк.
    assert "all_failed_connect=_all_failed_connect" in body, (
        "признак обязан доходить до решения о продолжении"
    )
    from services import invite_recovery as ir

    assert ir.should_schedule_continuation(
        left=100, group_broken=False, all_failed_connect=True,
        chain=0, max_chain=5)[0] is False, (
        "при полном провале подключения продолжение планировать нельзя"
    )


def test_surfaces_auth_key_duplicated_cause_actionably():
    body = _exec_body()
    assert "TWO DIFFERENT IP" in body or "AUTH_KEY_DUPLICATED" in body, (
        "движок должен распознавать ошибку двух IP"
    )
    assert "переавторизуйте" in body.lower() and "прокси" in body.lower(), (
        "нужна русская подсказка «что делать» (прокси/релог), а не сырой английский"
    )


def test_honest_not_executed_line_instead_of_defer():
    body = _exec_body()
    assert "Инвайт не выполнен" in body, (
        "при полном провале нужен честный итог вместо ложного «продолжим завтра»"
    )
    # и «осталось в очереди» не должно показываться при полном провале
    i = body.index("Осталось в очереди")
    seg = body[i - 40:i + 220]
    assert "not _all_failed_connect" in seg, (
        "строку «осталось в очереди» надо гасить при полном провале подключения"
    )


def test_result_dict_exposes_connection_diagnostics():
    body = _exec_body()
    assert '"all_failed_connect"' in body and '"worked_accounts"' in body, (
        "результат операции должен нести диагностику подключения"
    )
