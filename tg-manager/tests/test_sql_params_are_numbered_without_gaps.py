"""Запрос не имеет права пропускать номер параметра.

ЗАЧЕМ. asyncpg передаёт аргументы позиционно: `$1` — первый, `$2` — второй.
Если текст запроса упоминает `$2`, но не `$1`, то неупомянутому параметру
негде взять тип, и Postgres отказывает ещё на подготовке — значит запрос
падает КАЖДЫЙ раз, при любом вызове, на любой схеме.

ПОЧЕМУ ОТДЕЛЬНЫМ ФАЙЛОМ. `test_sql_runs_on_real_schema_postgres.py` отдаёт
запросы живой базе и такую поломку увидел бы, но он молча пропускал класс
ошибок `IndeterminateDatatypeError` с объяснением «тип параметра приходит от
asyncpg, это не про схему». Для честного `COALESCE($1, x)` это верно, а для
дырявой нумерации — нет: там пропуск и был поломкой. Пропуск с 2026-10-08
сужен, но проверка номеров не требует базы вовсе, поэтому живёт отдельно и
работает в любом окружении.

СКОЛЬКО ЕЩЁ ПРЯТАЛОСЬ ЗА ТЕМ ПРОПУСКОМ. Замерено на живой схеме 2026-10-09:
из 2802 успешно подготовленных запросов продукта `IndeterminateDatatypeError`
без дырявой нумерации не даёт НИ ОДИН. То есть весь тот пропуск скрывал ровно
две поломки ниже — расширять проверку дальше нечего (а если появится честный
`COALESCE($1, x)`, пропуск в соседнем тесте его по-прежнему пропустит).

ЧТО НАШЛА. Два запроса, падавших у владельца всегда:
  • `ecosystem_brain.compute_health` считал успешность операций по `owner_id=$2`
    при двух аргументах. Падение гасил общий `except`, и всё, что считается
    после (надёжность, восстановление, рост, стабильность), оставалось на
    умолчаниях 1.0 — экран здоровья показывал 100% любой экосистеме, даже если
    все её аккаунты в бане;
  • `contacts_hub.relationship_engine.get_relationships` упоминал $1 и $3, но не
    $2 — список связей контакта всегда приходил пустым.
"""
from __future__ import annotations

import importlib.util
import pathlib

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_PROBE = _ROOT / "tests" / "test_sql_runs_on_real_schema_postgres.py"


def _probe():
    """Берём извлекатель запросов у соседа, чтобы не плодить второй разбор."""
    spec = importlib.util.spec_from_file_location("_sqlprobe", _PROBE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_measurer_sees_the_product_queries():
    """Самопроверка: пустой список запросов означает сломанный извлекатель."""
    qs = list(_probe().driver_queries())
    assert len(qs) > 1500, (
        f"извлекатель вернул всего {len(qs)} запросов — он сломан, а не код")


def test_the_detector_fires_on_a_known_gap():
    """Обратный контроль: на заведомо дырявом запросе детектор обязан сработать."""
    gap = _probe()._param_gap("SELECT 1 FROM t WHERE owner_id=$2")
    assert gap == {1}, f"детектор не видит пропуск $1, вернул {gap}"
    assert not _probe()._param_gap("SELECT 1 FROM t WHERE a=$1 AND b=$2")


def test_no_query_skips_a_parameter_number():
    mod = _probe()
    bad = []
    seen = set()
    for rel, lineno, sql in mod.driver_queries():
        gap = mod._param_gap(sql)
        if not gap or (rel, lineno) in seen:
            continue
        seen.add((rel, lineno))
        bad.append(f"  {rel}:{lineno} — нет {sorted(gap)}\n    "
                   + " ".join(sql.split())[:120])
    assert not bad, (
        "запросы с пропущенным номером параметра (падают при каждом вызове, "
        "ошибку гасит except, владелец видит ноль или пустой экран):\n"
        + "\n".join(bad))


# ── вторая сторона того же: аргументов столько, сколько номеров в тексте ─────
#
# Пропуск номера — одна половина поломки, вторая — расхождение по счёту:
# запрос знает $1..$2, а в вызов передали три аргумента. asyncpg отвечает
# «the server expects 2 arguments for this query, 3 were passed», то есть
# вызов падает тоже всегда. Считаем только вызовы, где SQL — чистый литерал,
# а аргументы перечислены поимённо: там счёт сверить можно надёжно.

_ARG_METHODS = {"fetch", "fetchrow", "fetchval", "execute"}
_SKIP_DIRS = {".git", "__pycache__", "node_modules", "mini_app", "deploy",
              "tests"}


def _literal_sql(node):
    """Строковый литерал (с неявной и явной склейкой литералов) или None."""
    import ast
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _literal_sql(node.left), _literal_sql(node.right)
        if left is None or right is None:
            return None
        return left + right
    return None


def _literal_calls():
    """[(файл, строка, sql, число_аргументов)] по вызовам с литеральным SQL."""
    import ast
    import os
    out = []
    for dirpath, dirs, files in os.walk(_ROOT):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for fname in files:
            if not fname.endswith(".py"):
                continue
            path = os.path.join(dirpath, fname)
            try:
                tree = ast.parse(
                    open(path, encoding="utf-8").read())
            except (SyntaxError, UnicodeDecodeError):
                continue
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr in _ARG_METHODS and node.args):
                    continue
                sql = _literal_sql(node.args[0])
                if sql is None or "$" not in sql:
                    continue
                rest = node.args[1:]
                # Распаковка (*args) и **kwargs прячут настоящее число —
                # такие вызовы не судим.
                if any(isinstance(a, ast.Starred) for a in rest):
                    continue
                if any(k.arg is None for k in node.keywords):
                    continue
                rel = os.path.relpath(path, _ROOT)
                out.append((rel, node.lineno, sql, len(rest)))
    return out


def test_measurer_sees_the_literal_calls():
    """Самопроверка: вызовов с литеральным SQL в продукте заведомо много."""
    calls = _literal_calls()
    assert len(calls) > 500, (
        f"обход нашёл всего {len(calls)} вызовов — он сломан, а не код")


def _mismatch(sql: str, nargs: int) -> bool:
    """Текст и число аргументов расходятся (пропуск номера или перекос счёта)."""
    import re as _re
    nums = {int(x) for x in _re.findall(r"\$(\d+)", sql)}
    if not nums:
        return False
    return nums != set(range(1, nargs + 1))


def test_the_count_detector_fires_on_a_known_mismatch():
    """Обратный контроль: иначе проверка зелена по любой другой причине."""
    assert _mismatch("UPDATE t SET a=$1 WHERE id=$2", 3), "перекос не виден"
    assert _mismatch("UPDATE t SET a=$1 WHERE id=$2", 1), "недостача не видна"
    assert _mismatch("SELECT 1 FROM t WHERE a=$2", 2), "пропуск $1 не виден"
    assert not _mismatch("UPDATE t SET a=$1 WHERE id=$2", 2)


def test_arguments_match_the_numbers_in_the_text():
    import re as _re
    bad = []
    for rel, lineno, sql, nargs in _literal_calls():
        nums = {int(x) for x in _re.findall(r"\$(\d+)", sql)}
        if _mismatch(sql, nargs):
            bad.append(f"  {rel}:{lineno} — в тексте {sorted(nums)}, "
                       f"аргументов {nargs}\n    "
                       + " ".join(sql.split())[:110])
    assert not bad, (
        "вызов передаёт не те аргументы, которые нумерует запрос "
        "(asyncpg отвергает такой вызов всегда):\n" + "\n".join(bad))
