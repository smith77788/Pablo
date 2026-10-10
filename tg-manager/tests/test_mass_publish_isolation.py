"""Регрессия: изоляция ОДНОГО аккаунта не должна обрывать всю mass_publish.

Баг «3 успеха / 56 ошибок»: каждый канал управляется одним аккаунтом (mc.acc_id),
каналы отсортированы по channel_id (перемешаны по аккаунтам). Когда аккаунт
изолируется после сетевого сбоя, следующий его канал даёт acc is None. Раньше
код делал `fail_count += remaining; break` — валил ВСЕ оставшиеся каналы, включая
управляемые здоровыми аккаунтами. Должно быть `continue` (пропустить лишь канал).
"""
from __future__ import annotations

import ast
import inspect

from services import op_worker


def _isolated_branch_source() -> str:
    src = inspect.getsource(op_worker._exec_mass_publish)
    # берём тело ветки `if acc is None:` до следующего dedent-оператора цикла
    lines = src.splitlines()
    out, capture, indent = [], False, None
    for ln in lines:
        if "if acc is None:" in ln:
            capture = True
            indent = len(ln) - len(ln.lstrip())
            out.append(ln)
            continue
        if capture:
            if ln.strip() and (len(ln) - len(ln.lstrip())) <= indent:
                break
            out.append(ln)
    # отбрасываем строки-комментарии — проверяем КОД, а не пояснения
    code = [ln for ln in out if not ln.lstrip().startswith("#")]
    return "\n".join(code)


def test_isolated_account_does_not_abort_whole_operation():
    branch = _isolated_branch_source()
    assert branch, "ветка `if acc is None:` не найдена"
    # НЕ должно валить все оставшиеся каналы разом
    assert "+= remaining" not in branch, (
        "isolated-ветка валит все оставшиеся каналы (fail += remaining) — вернулся баг 3/56"
    )
    # должно пропускать только текущий канал и продолжать
    assert "continue" in branch, "isolated-ветка должна continue (не break) — идти к каналам здоровых аккаунтов"
    assert "fail_count += 1" in branch, "должен помечаться неудачным только текущий канал"
    assert "break" not in branch, "break обрывает операцию на первом изолированном аккаунте"


def test_exec_mass_publish_still_valid_python():
    # защищаемся от синтаксической поломки при правке большого исполнителя
    ast.parse(inspect.getsource(op_worker._exec_mass_publish))
