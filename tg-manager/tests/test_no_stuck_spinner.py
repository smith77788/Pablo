"""Гейт «сценарий доходит до результата»: загрузчик, поставивший крутилку, на ошибке
не оставляет её навсегда (иначе — тупик без возможности повтора).

Класс тупика: функция пишет spin-wrap в элемент, а её catch только тостит — крутилка
висит вечно, кнопки/повтора нет. Ловим такие функции и требуем, чтобы catch тоже
перерисовывал элемент (innerHTML/txt/errHtml/empty), а не только показывал тост.
"""
from __future__ import annotations
import re
from pathlib import Path

HTML = (Path(__file__).resolve().parent.parent / "mini_app" / "index.html").read_text(encoding="utf-8")
_JS = "\n".join(re.findall(r"<script>(.*?)</script>", HTML, re.DOTALL))


def _stuck_spinner_functions():
    risky = []
    for name, body in re.findall(r"(?:async )?function (\w+)\([^)]*\)\s*\{(.*?)\n\}", _JS, re.DOTALL):
        if "spin-wrap" not in body:
            continue
        for cm in re.finditer(r"catch\s*\([^)]*\)\s*\{([^}]*)\}", body):
            c = cm.group(1)
            if "toast" in c and not any(k in c for k in ("txt(", "innerHTML", "errHtml", "empty(")):
                risky.append(name)
                break
    return sorted(set(risky))


def test_no_load_leaves_stuck_spinner():
    stuck = _stuck_spinner_functions()
    assert not stuck, ("Загрузчики оставляют крутилку на ошибке (тупик без повтора):\n"
                       + "\n".join(stuck))
