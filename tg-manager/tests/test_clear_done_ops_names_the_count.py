"""«Очистить» в журнале операций называет, сколько завершённых уйдёт.

Подтверждение говорило «все завершённые операции» без числа, хотя счётчики
по всей очереди уже лежат на экране. В журнале проекта счётчики свои, а
очистка — по всей очереди владельца, поэтому там число не показываем.
"""
import re
from pathlib import Path

INDEX = (Path(__file__).resolve().parents[1] / "mini_app/index.html").read_text(encoding="utf-8")


def _fn(name):
    m = re.search(r"\n(?:async )?function " + name + r"\([\s\S]*?\n}\n", INDEX)
    assert m, name
    return m.group(0)


def test_counts_are_kept_only_for_the_whole_queue():
    assert "OPS_LAST_COUNTS = (c && !OPS_PROJECT_ID) ? c : null;" in _fn("loadOps")


def test_confirm_names_the_number():
    body = _fn("clearDoneOps")
    assert "['done','failed','cancelled']" in body
    assert "plural(fin,'завершённую операцию','завершённые операции','завершённых операций')" in body
    assert "askConfirm('Удалить из очереди '+what+" in body
