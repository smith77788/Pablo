"""История запусков, из которой нельзя открыть запуск.

Шесть экранов показывают свою историю операций — проверка номеров, жалобы,
накрутка, продвижение в нише, массовая публикация, журнал масс-действий. Каждая
строка выводит «готово · 120/120» и ничем не нажимается. А весь СМЫСЛ запуска —
в его результате: карточка операции показывает итог, причину ошибки и журнал по
каждой цели с фильтром «только неудачные». Открыть её с экрана, который сам же
этот запуск и породил, было нечем: нужно было уйти в общий список операций и
искать там свою среди чужих.

Поэтому здесь правило, а не шесть точечных проверок: экран, который читает
список операций, обязан вести из строки в карточку операции. Список таких
экранов вычисляется из кода — новый экран с историей запусков попадёт под
правило сам.
"""
from __future__ import annotations

import glob
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _js() -> str:
    parts = [open(os.path.join(ROOT, "mini_app", "index.html"), encoding="utf-8").read()]
    for f in sorted(glob.glob(os.path.join(ROOT, "mini_app", "screens", "*.js"))):
        parts.append(open(f, encoding="utf-8").read())
    return "\n".join(parts)


def _functions(js: str):
    for m in re.finditer(r"\n(?:async )?function (\w+)\s*\([^)]*\)\s*\{", js):
        i = m.end() - 1
        depth = 0
        while i < len(js):
            if js[i] == "{":
                depth += 1
            elif js[i] == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        yield m.group(1), js[m.end():i]


# Экраны, читающие список операций владельца.
_READS_OPS = re.compile(r"api\(\s*['\"`]/api/miniapp/(operations|mass_ops)")


def _history_screens(js: str) -> dict:
    """Функции, которые читают список операций И РИСУЮТ его строками.

    Кнопки «Пауза»/«Очистить» тоже ходят по этим адресам, но ничего не
    показывают — им открывать нечего, и в правило они не входят.
    """
    out = {}
    for name, body in _functions(js):
        if name == "openOpDetail":
            continue
        if not _READS_OPS.search(body):
            continue
        if ".map(" not in body or "txt(" not in body:
            continue
        out[name] = body
    return out


def test_probe_finds_the_history_screens():
    """Пустой список сделал бы правило вечно зелёным."""
    found = _history_screens(_js())
    assert len(found) >= 5, f"экраны с историей запусков перестали находиться: {sorted(found)}"
    assert "openPhoneChecker" in found, sorted(found)


def test_every_history_row_opens_its_operation():
    silent = sorted(n for n, body in _history_screens(_js()).items()
                    if "openOpDetail(" not in body)
    assert not silent, (
        f"экраны показывают свои запуски и не дают их открыть: {silent}. "
        f"Итог, причина ошибки и журнал по каждой цели живут в карточке "
        f"операции — без перехода строка остаётся числом")
