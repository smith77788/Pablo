"""Гейт «сценарий доходит до результата»: загрузчик, поставивший крутилку, на ошибке
не оставляет её навсегда (иначе — тупик без возможности повтора).

Класс тупика: функция пишет spin-wrap в элемент, а её catch только тостит — крутилка
висит вечно, кнопки/повтора нет. Ловим такие функции и требуем, чтобы catch тоже
перерисовывал элемент (innerHTML/txt/errHtml/empty), а не только показывал тост.

ИЗМЕРИТЕЛЬ ПРАВИЛСЯ (25.11.2026). Тело catch бралось регуляркой "{([^}]*)}",
то есть до ПЕРВОЙ закрывающей скобки. С появлением догрузки списков («Загрузить
ещё») в catch появился ранний возврат:

    } catch (e) {
      if (ROWS.length) { toast('⚠️ ...'); return; }   // строки уже на экране
      txt('list', empty('⚠️','Ошибка', ...));          // крутилка на экране — перерисовываем
    }

Обрезанное тело кончалось на скобке `if`, поэтому детектор видел только тост и
объявлял тупиком `loadErrReports` — функцию, у которой перерисовка стоит
строкой ниже. Теперь тело catch берётся со СЧЁТОМ скобок. Пробник на заведомо
больном примере — ниже: без него правка измерителя не стоит доверия.
"""
from __future__ import annotations
import re
from pathlib import Path

HTML = (Path(__file__).resolve().parent.parent / "mini_app" / "index.html").read_text(encoding="utf-8")
_JS = "\n".join(re.findall(r"<script>(.*?)</script>", HTML, re.DOTALL))

_REDRAW = ("txt(", "innerHTML", "errHtml", "empty(")


def _catch_bodies(body: str):
    """Тела catch со счётом скобок: ранний возврат внутри `if` тело не обрывает."""
    for m in re.finditer(r"catch\s*\([^)]*\)\s*\{", body):
        i = m.end()
        depth = 1
        while i < len(body) and depth:
            if body[i] == "{":
                depth += 1
            elif body[i] == "}":
                depth -= 1
            i += 1
        yield body[m.end():i - 1]


def _stuck_spinner_functions(src: str | None = None):
    risky = []
    for name, body in re.findall(r"(?:async )?function (\w+)\([^)]*\)\s*\{(.*?)\n\}",
                                 src if src is not None else _JS, re.DOTALL):
        if "spin-wrap" not in body:
            continue
        for c in _catch_bodies(body):
            if "toast" in c and not any(k in c for k in _REDRAW):
                risky.append(name)
                break
    return sorted(set(risky))


def test_the_measurer_still_sees_a_real_dead_end():
    """Заведомо больной пример: тост вместо перерисовки — находка."""
    sick = ("function loadSick() {\n"
            "  txt('box', '<div class=\"spin-wrap\"><div class=\"spin\"></div></div>');\n"
            "  try { go(); } catch (e) { toast('ошибка'); }\n}\n")
    assert _stuck_spinner_functions(sick) == ["loadSick"]


def test_the_measurer_does_not_flag_an_early_return_for_load_more():
    """И заведомо здоровый: тост для догрузки, перерисовка для первой загрузки."""
    ok = ("function loadOk() {\n"
          "  if (!more) txt('box', '<div class=\"spin-wrap\"><div class=\"spin\"></div></div>');\n"
          "  try { go(); } catch (e) {\n"
          "    if (ROWS.length) { toast('не вышло'); return; }\n"
          "    txt('box', empty('error'));\n  }\n}\n")
    assert _stuck_spinner_functions(ok) == []


def test_no_load_leaves_stuck_spinner():
    stuck = _stuck_spinner_functions()
    assert not stuck, ("Загрузчики оставляют крутилку на ошибке (тупик без повтора):\n"
                       + "\n".join(stuck))
