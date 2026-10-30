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


# ── «Частично» без счётчиков ────────────────────────────────────────────────
#
# Исход решает не слово исполнителя, а `op_status.classify_final`, и решает по
# СЧЁТЧИКАМ: `status` — это намерение, а ok/failed — факт (так и задумано, см.
# докстринг). Долгое время из этого следовало опасное: возврат, заявивший
# `partial` без ok/failed, превращался в `done`, слово терялось молча, и
# недоведённая работа закрывалась зелёным. Я наступил на это дважды в один
# день, в своих же правках: связка сетей и включение bot-to-bot оба честно
# говорили «partial» и оба получали «done».
#
# Саму потерю слова теперь не допускает классификатор: прямое `partial` он
# больше не поднимает до `done` (см.
# `test_an_interrupted_sync_is_not_called_done` — там была ветка, у которой
# счётчиков нет и быть не может). Храповик ниже остаётся, и причина у него
# теперь другая: счётчики — не украшение статуса. По ok/failed считаются сводки
# и метрики, по ним разбор «что не доехало» и письмо в мёртвую очередь решают,
# сколько именно работы потеряно, и по ним же классификатор отличает «часть
# сделана» от «не сделано ничего» (ok=0 при failed>0 — это `failed`). Возврат,
# заявивший «частично» и не сказавший СКОЛЬКО, доносит до владельца слово без
# числа — а решает он по числу.
#
# Внимание на границу этого храповика: он требует НАЛИЧИЯ ключей, а не
# ненулевых значений. Ветка таймаута `contacts_sync` проходила его с
# `ok: 0, failed: 0` — она и была той дырой.

def _status_words_of_return(fn: ast.AST, node: ast.Return) -> set[str]:
    """Слова, которыми этот возврат может назвать исход (включая переменную)."""
    words: set[str] = set()
    for k, v in zip(node.value.keys, node.value.values):
        if not (isinstance(k, ast.Constant) and k.value == "status"):
            continue
        if isinstance(v, ast.Name):
            for n in ast.walk(fn):
                tgts = (n.targets if isinstance(n, ast.Assign)
                        else [n.target] if isinstance(n, ast.AnnAssign) else [])
                if any(isinstance(t, ast.Name) and t.id == v.id for t in tgts):
                    for sub in ast.walk(n.value) if n.value is not None else []:
                        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                            words.add(sub.value)
        else:
            for sub in ast.walk(v):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                    words.add(sub.value)
    return words


def _partial_by_assignment(fn: ast.AST) -> list[int]:
    """Строки, где исход правится НА МЕСТЕ: `res["status"] = "partial"`.

    Третья моя правка этой ветки теряла «частично» именно так, и перепись её не
    видела: она смотрела только на словарь в `return`. Исход, собранный
    присваиванием в уже готовый результат, — тот же исход.
    """
    out = []
    for n in ast.walk(fn):
        if not isinstance(n, ast.Assign) or not isinstance(n.value, ast.Constant):
            continue
        if n.value.value != "partial":
            continue
        for t in n.targets:
            if isinstance(t, ast.Subscript) and isinstance(t.slice, ast.Constant) \
                    and t.slice.value == "status":
                out.append(n.lineno)
    return out


def partial_without_counters(src: str) -> list[tuple[str, int]]:
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
            if "partial" not in _status_words_of_return(fn, n):
                continue
            keys = {k.value for k in n.value.keys if isinstance(k, ast.Constant)}
            if not ("ok" in keys and "failed" in keys):
                out.append((fn.name, n.lineno))
    # Исход, правленный на месте: счётчик обязан быть поправлен рядом, иначе
    # classify_final всё равно скажет «выполнено».
    for fn in tree.body:
        if not isinstance(fn, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        if not fn.name.startswith("_exec_"):
            continue
        fixes_the_counter = any(
            isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Subscript)
                    and isinstance(t.slice, ast.Constant)
                    and t.slice.value == "failed" for t in n.targets)
            for n in ast.walk(fn))
        if fixes_the_counter:
            continue
        for line in _partial_by_assignment(fn):
            out.append((fn.name, line))
    return sorted(out)


def test_a_partial_outcome_carries_its_counters():
    silent = partial_without_counters(_src())
    assert not silent, (
        f"исход «partial» заявлен без счётчиков ok/failed: {silent}. "
        "classify_final судит по счётчикам, а не по слову: при пустых "
        "счётчиках «partial» превращается в «done», и недоведённая работа "
        "снова закрывается зелёным")


def test_the_mechanism_is_really_this_way():
    """Не догадка: вот как именно слово доходит, а числа — нет.

    Слово «частично» классификатор теперь не теряет даже на пустых счётчиках.
    Но исход — это ещё не отчёт: с ok=0/failed=0 владелец видит «частично» без
    единой цифры, и ровно этого храповик выше не разрешает.
    """
    from services import op_status

    assert op_status.classify_final("partial", ok=0, failed=0) == op_status.PARTIAL
    assert op_status.classify_final("partial", ok=1, failed=1) == op_status.PARTIAL
    # Счётчики по-прежнему сильнее слова — вниз: часть не сделана, а НИЧЕГО не
    # сделано, и это другой исход.
    assert op_status.classify_final("partial", ok=0, failed=4) == op_status.FAILED


def test_the_counter_census_bites():
    sick = ("async def _exec_x(pool, op_id):\n"
            "    return {'status': 'partial', 'summary': 'часть'}\n")
    assert partial_without_counters(sick), "перепись не видит partial без счётчиков"


def test_the_counter_census_sees_a_variable_status():
    """Слово бывает в переменной — на этом сбилась первая прикидка."""
    sick = ("async def _exec_x(pool, op_id):\n"
            "    status = 'partial' if left else 'done'\n"
            "    return {'status': status, 'summary': 'часть'}\n")
    assert partial_without_counters(sick), (
        "перепись смотрит только на литерал в самом возврате — исход, "
        "собранный в переменной, она пропустит")


def test_the_counter_census_accepts_counters():
    healthy = ("async def _exec_x(pool, op_id):\n"
               "    return {'status': 'partial', 'ok': 3, 'failed': 1, "
               "'summary': 'часть'}\n")
    assert not partial_without_counters(healthy)


def test_the_census_sees_a_status_fixed_in_place():
    """Исход правят и присваиванием — перепись обязана это видеть."""
    sick = ("async def _exec_x(pool, op_id):\n"
            "    res = await inner()\n"
            "    if left:\n"
            "        res['status'] = 'partial'\n"
            "    return res\n")
    assert partial_without_counters(sick), (
        "исход, правленный на месте, перепись пропускает — ровно так и "
        "потерялась третья правка")
