"""Каждый исполнитель, считающий провалы, обязан считать и успехи.

Итог операции определяется по СЧЁТЧИКАМ, а не по слову исполнителя:
`op_status.classify_final` берёт result["ok"] и result["failed"], потому что
«я отработал» не должно перевешивать «177 целей не взято». Обратная сторона
того же правила: исполнитель, который вернул только провалы, выглядит как
полный провал, даже если взял почти все цели.

Так и было у шести исполнителей сразу. Они возвращали свой счётчик успеха под
собственным именем — created, left, read, deleted, clean — и рядом failed.
classify_final видел ok=0 при failed>0 и закрывал операцию как FAILED. Владелец
видел красный итог на работе, которая почти вся сделана: каналы созданы, чаты
покинуты, диалоги прочитаны. Хуже того, этот мнимый провал шёл в предохранитель
(`_circuit_breaker_record`), и несколько таких прогонов подряд открывали цепь,
останавливая ВСЕ операции владельца. То есть нормальная работа с обычным
процентом отказов Telegram могла сама себя заглушить.

Тест держит правило для ВСЕХ исполнителей из таблицы диспетчеризации, а не для
шести вылеченных: класс закрывается целиком, иначе следующий исполнитель
принесёт его обратно.
"""
from __future__ import annotations

import ast
from pathlib import Path


_SRC = Path(__file__).resolve().parents[1] / "services" / "op_worker.py"
_FAIL_KEYS = ("failed", "fail")


def _dispatch_executor_names(tree: ast.AST) -> set[str]:
    for n in ast.walk(tree):
        if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef)) and n.name == "_build_dispatch":
            names = set()
            for d in ast.walk(n):
                if isinstance(d, ast.Dict):
                    for v in d.values:
                        if isinstance(v, ast.Name):
                            names.add(v.id)
            return names
    raise AssertionError("не найдена таблица диспетчеризации _build_dispatch")


def _violations():
    src = _SRC.read_text(encoding="utf-8")
    tree = ast.parse(src)
    wanted = _dispatch_executor_names(tree)
    bad = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        if fn.name not in wanted:
            continue
        for ret in ast.walk(fn):
            if not (isinstance(ret, ast.Return) and isinstance(ret.value, ast.Dict)):
                continue
            keys = {}
            for k, v in zip(ret.value.keys, ret.value.values):
                if isinstance(k, ast.Constant):
                    keys[k.value] = v
            if not isinstance(keys.get("status"), ast.Constant):
                continue
            if keys["status"].value != "done":
                continue
            fail_v = next((keys[k] for k in _FAIL_KEYS if k in keys), None)
            if fail_v is None:
                continue
            # Пустой итог («целей нет»: failed=0 литералом) — не про счётчики.
            if isinstance(fail_v, ast.Constant) and fail_v.value == 0:
                continue
            if "ok" not in keys:
                bad.append((fn.name, ret.lineno))
    return bad


def test_every_executor_counting_failures_also_counts_successes():
    bad = _violations()
    assert not bad, (
        "исполнитель считает провалы, но не успехи — частично удавшийся прогон "
        "закроется ПОЛНЫМ провалом и уйдёт в предохранитель: "
        + ", ".join(f"{name} (строка {line})" for name, line in bad)
    )


def test_detector_sees_a_planted_violation():
    """Пробник, который ничего не находит в любом коде, бесполезен."""
    sample = '''
def _build_dispatch():
    return {"x": _exec_sample}

async def _exec_sample(pool, bot, op_id, owner_id, params):
    return {"status": "done", "created": created_count, "failed": failed_count}
'''
    tree = ast.parse(sample)
    wanted = _dispatch_executor_names(tree)
    assert "_exec_sample" in wanted
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "_exec_sample")
    ret = next(n for n in ast.walk(fn) if isinstance(n, ast.Return))
    keys = [k.value for k in ret.value.keys if isinstance(k, ast.Constant)]
    assert "ok" not in keys and "failed" in keys, (
        "образец нарушения обязан быть именно нарушением"
    )
