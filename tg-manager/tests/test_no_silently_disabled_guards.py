"""Проверка, которая перестала проверять, хуже отсутствующей.

О ЧЁМ ЭТО. В наборе принят приём «вырезать кусок исходника и убедиться, что в
нём есть/нет нужного»:

    i = src.index("async def foo")
    body = src[i:i + 3000]          # ← окно фиксированной длины
    assert "claim" in body

Окно живёт ровно до следующей правки: стоит функции подрасти или сдвинуться, и
кусок перестаёт совпадать с тем, что имелось в виду. Дальше исход зависит от
знака утверждения:
  • ПОЛОЖИТЕЛЬНОЕ («в теле есть X») — тест краснеет. Шумно, но честно.
  • ОТРИЦАТЕЛЬНОЕ («в теле НЕТ X») — тест ЗЕЛЕНЕЕТ навсегда: в пустом или чужом
    куске искомого, разумеется, нет. Защита выключилась, и никто не узнал.

Второй случай уже случался: четыре таких окна караулили инварианты захвата
сессий и подстановки шаблонов и держались на длине среза. Все переведены на
границы функций (AST) либо на разбор конкретных присваиваний.

ЭТОТ ТЕСТ запрещает возвращать связку «окно фиксированной длины + отрицательное
утверждение». Положительные окна остаются: они шумят, но не врут — их много, и
переписывать всё разом значило бы трогать 35 файлов ради стиля.
"""
from __future__ import annotations

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.dirname(os.path.abspath(__file__))

# var = something[i:i + 900]   или   var = something[max(0, i - 400):...]
_WINDOW = re.compile(
    r"(\w+)\s*=\s*\w+\[\s*\w+\s*:\s*\w+\s*\+\s*(\d{3,})\s*\]"
    r"|(\w+)\s*=\s*\w+\[\s*max\(\s*0\s*,\s*\w+\s*-\s*(\d{3,})\s*\)\s*:")
_LOOKAHEAD = 12          # строк, в которых ищем утверждение об этом куске


def _negative_assert(line: str, var: str) -> bool:
    v = re.escape(var)
    return bool(
        re.search(rf"assert\s+.*\bnot in\b.*\b{v}\b", line)
        or re.search(rf"assert\s+not\s+.*\b{v}\b", line)
    )


def offenders() -> list[str]:
    out: list[str] = []
    for fname in sorted(os.listdir(TESTS)):
        if not (fname.startswith("test_") and fname.endswith(".py")):
            continue
        if fname == os.path.basename(__file__):
            continue
        path = os.path.join(TESTS, fname)
        lines = open(path, encoding="utf-8", errors="ignore").read().split("\n")
        for i, line in enumerate(lines):
            m = _WINDOW.search(line)
            if not m:
                continue
            var = m.group(1) or m.group(3)
            if not var:
                continue
            for j in range(i + 1, min(i + 1 + _LOOKAHEAD, len(lines))):
                if _negative_assert(lines[j], var):
                    out.append(f"tests/{fname}:{i + 1} → утверждение на строке {j + 1}")
                    break
    return out


def test_no_negative_assertion_on_a_fixed_window():
    bad = offenders()
    assert not bad, (
        "отрицательная проверка внутри окна фиксированной длины:\n  "
        + "\n  ".join(bad)
        + "\n\nСдвинулся код — окно промахнулось — «искомого нет» стало правдой, "
          "и защита выключилась молча.\nБерите границы функции: "
          "ast.parse + node.lineno/node.end_lineno (примеры — "
          "tests/test_process_roles.py, tests/test_session_concurrency_safety.py)."
    )


def test_detector_recognises_the_shape():
    """Детектор обязан ловить именно ту связку, ради которой написан."""
    assert _WINDOW.search("    body = src[i:i + 3000]")
    assert _WINDOW.search("    seg = html[max(0, i - 400): i + 200]")
    assert not _WINDOW.search("    body = src[i:j]"), "срез по границам — не окно"
    assert _negative_assert('    assert "x" not in body', "body")
    assert _negative_assert("    assert not body.count", "body")
    assert not _negative_assert('    assert "x" in body', "body")


def test_detector_actually_scans_the_suite():
    """Пустой обход сделал бы тест зелёным навсегда."""
    files = [f for f in os.listdir(TESTS)
             if f.startswith("test_") and f.endswith(".py")]
    assert len(files) > 100, f"в обходе всего {len(files)} файлов тестов"
