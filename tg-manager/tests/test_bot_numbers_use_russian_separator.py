"""Разряды в текстах бота разделяются пробелом, а не запятой.

`f"{n:,}"` печатал «12,345 подписчиков». По-русски запятая отделяет дробную
часть, так что владелец читал «двенадцать целых и сколько-то». Таких мест в
боте было 104 (плюс четыре суммы сделок `:,.0f` в CRM).
"""
import re
from pathlib import Path

from bot.utils.fmt import num_ru

BOT = Path(__file__).resolve().parents[1] / "bot"


def test_num_ru():
    assert num_ru(12345) == "12 345"
    assert num_ru(1234567) == "1 234 567"
    assert num_ru(999) == "999"
    assert num_ru(1234.5) == "1 234.5"
    assert num_ru(None) == "None"


def test_no_comma_thousands_left_in_bot_texts():
    bad = []
    for p in sorted(BOT.rglob("*.py")):
        if p.name == "fmt.py":
            continue
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"\{[^{}]+:,(\.\d+f|d)?\}", line):
                bad.append(f"{p.relative_to(BOT)}:{n}: {line.strip()[:90]}")
    assert not bad, "разряды через запятую:\n" + "\n".join(bad)
