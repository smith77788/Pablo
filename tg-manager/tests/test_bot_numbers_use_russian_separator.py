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


def test_service_texts_for_the_owner_use_space_too():
    """Тексты, которые сервисы присылают в бот (сводки аудитории, ДНК,
    итоги сетевой рассылки), — та же запятая между разрядами."""
    services = BOT.parent / "services"
    bad = []
    for p in sorted(services.glob("*.py")):
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"[А-Яа-яЁё]", line) and re.search(r"\{[^{}]+:,(\.\d+f)?\}", line) \
                    and '.replace(",", " ")' not in line:
                bad.append(f"{p.name}:{n}: {line.strip()[:90]}")
    assert not bad, "разряды через запятую:\n" + "\n".join(bad)


def test_plural():
    from bot.utils.fmt import plural
    forms = ("бот", "бота", "ботов")
    assert [plural(n, *forms) for n in (0, 1, 2, 4, 5, 11, 12, 14, 21, 22, 25, 101, 111)] == [
        "ботов", "бот", "бота", "бота", "ботов", "ботов", "ботов", "ботов",
        "бот", "бота", "ботов", "бот", "ботов",
    ]
    assert plural(None, *forms) == "ботов"


def test_no_bracket_plurals_in_bot_texts():
    """«3 аккаунт(ов)», «шаг(а/ов)» — окончание в скобках вместо склонения."""
    bad = []
    for p in sorted(BOT.rglob("*.py")):
        if p.name == "fmt.py":
            continue
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if re.search(r"(\}|</b>) ?[а-яё]+\((ов|а|ей|а/ов|ы/ов)\)", line):
                bad.append(f"{p.relative_to(BOT)}:{n}: {line.strip()[:90]}")
    assert not bad, "окончание в скобках:\n" + "\n".join(bad)
