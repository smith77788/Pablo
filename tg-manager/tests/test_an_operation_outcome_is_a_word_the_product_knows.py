"""Исход операции — слово из закрытого словаря, и объяснение владелец читает.

СЛОВАРЬ. Исходы операции описаны в `services/op_status.py`: `done`, `partial`,
`failed`, `cancelled` плюс служебный `requeue`. Исполнителей 77, возвратов с
исходом — больше двухсот шестидесяти, и слово в них пишется руками. Опечатка
или свой, местный исход не падают ошибкой: `op_status.classify_final`
незнакомое слово молча игнорирует и судит по счётчикам, а при пустых счётчиках
это `done`. То есть **отказ, названный своим словом, превращается в
«выполнено»**.

Так и было: `_exec_bulk_bot_edit` на неизвестное поле возвращал
`{'status': 'skipped', 'reason': ...}` — отказ ДО работы, который владелец
видел как успешно выполненную операцию.

ОБЪЯСНЕНИЕ. Причину отказа исполнители пишут в двух разных ключах, и читаются
они по-разному: `reason` воркер читает в текст ОШИБКИ (`error_msg`), а
`summary` — в сводку, которую владелец видит у любого исхода. Поэтому на
НЕ-провальном исходе `reason` виден никому: операция закрывается без единого
слова. Второй такой случай — план присутствия, у которого не осталось целей:
«выполнено» и пустая строка.

Эти две переписи закрытые: словарь и наличие сводки проверяются по ВСЕМ
исполнителям, а не по списку, который ведётся руками.
"""
from __future__ import annotations

import ast
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "services", "op_worker.py")

# Служебные исходы решаются выше по стеку и в TERMINAL не входят намеренно.
_SERVICE_OUTCOMES = {"requeue"}


def _outcomes(src: str) -> list[tuple[str, int, str, set]]:
    """Возвраты исполнителя с исходом: (функция, строка, исход, ключи).

    Только ВЕРХНИЙ уровень исполнителя: вложенные функции возвращают результат
    ОДНОЙ цели, и там свой словарь («no_session», «unknown» у проверки
    здоровья) — считать их исходом операции нельзя. На этом сбилась первая
    прикидка: она дала три «одиночки» вместо одной.
    """
    tree = ast.parse(src)
    out = []
    for fn in tree.body:
        if not isinstance(fn, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        if not fn.name.startswith("_exec_"):
            continue
        nested = {id(x) for g in ast.walk(fn)
                  if isinstance(g, (ast.FunctionDef, ast.AsyncFunctionDef)) and g is not fn
                  for x in ast.walk(g)}
        for n in ast.walk(fn):
            if id(n) in nested or not isinstance(n, ast.Return):
                continue
            if not isinstance(n.value, ast.Dict):
                continue
            keys = {k.value for k in n.value.keys if isinstance(k, ast.Constant)}
            for k, v in zip(n.value.keys, n.value.values):
                if isinstance(k, ast.Constant) and k.value == "status" \
                        and isinstance(v, ast.Constant):
                    out.append((fn.name, n.lineno, v.value, keys))
    return out


def _src() -> str:
    with open(SRC, encoding="utf-8") as f:
        return f.read()


# ── Словарь исходов ──────────────────────────────────────────────────────────

def test_every_executor_outcome_is_a_word_the_product_knows():
    from services import op_status

    known = set(op_status.TERMINAL) | _SERVICE_OUTCOMES
    strangers = sorted({(fn, line, st) for fn, line, st, _ in _outcomes(_src())
                        if st not in known})
    assert not strangers, (
        "исход операции назван словом, которого нет в op_status: "
        f"{strangers}. Такое слово не падает ошибкой — classify_final его "
        "игнорирует и при пустых счётчиках выдаёт done, то есть ОТКАЗ "
        f"превращается в «выполнено». Словарь: {sorted(known)}")


def test_a_word_outside_the_vocabulary_really_becomes_done():
    """Почему это опасно именно так, а не иначе: механизм, а не догадка."""
    from services import op_status

    assert op_status.classify_final("skipped", ok=0, failed=0) == op_status.DONE
    assert op_status.classify_final("failed", ok=0, failed=0) == op_status.FAILED


def test_the_vocabulary_census_bites():
    """Измеритель, молчащий на заведомо больном примере, бесполезен."""
    sick = (
        "async def _exec_made_up(pool, bot, op_id, owner_id, params):\n"
        "    return {'status': 'skipped', 'reason': 'нет такого поля'}\n")
    got = _outcomes(sick)
    assert got and got[0][2] == "skipped", f"перепись не видит исход: {got}"


def test_the_vocabulary_census_ignores_a_per_target_dict():
    """Исход ОДНОЙ цели — не исход операции, и путать их нельзя."""
    healthy = (
        "async def _exec_made_up(pool, bot, op_id, owner_id, params):\n"
        "    async def _one(acc):\n"
        "        return {'status': 'no_session', 'reason': ''}\n"
        "    await _one(1)\n"
        "    return {'status': 'done', 'summary': 'готово'}\n")
    assert [o[2] for o in _outcomes(healthy)] == ["done"], (
        "словарь одной цели принят за исход операции — перепись начнёт "
        "требовать переименований там, где всё цело")


# ── Сводка на НЕ-провальном исходе ───────────────────────────────────────────

def test_every_non_failing_outcome_carries_a_summary():
    silent = sorted({(fn, line, st) for fn, line, st, keys in _outcomes(_src())
                     if st in ("done", "partial") and "summary" not in keys})
    assert not silent, (
        "операция закрылась без сводки: " + str(silent) + ". `reason` здесь не "
        "спасает — воркер читает его только в текст ошибки, а на успешном "
        "исходе владелец видит пустую строку вместо объяснения")


def test_the_worker_reads_the_summary_and_not_the_reason():
    """Почему сводка обязательна именно на успешном исходе."""
    src = _src()
    assert 'result.get("summary", "")' in src or 'result.get("summary") or ""' in src
    assert 'result.get("reason") or result.get("summary")' in src, (
        "воркер перестал читать reason в текст ошибки — проверьте, читается ли "
        "он вообще, иначе причина отказа теряется на ЛЮБОМ исходе")
