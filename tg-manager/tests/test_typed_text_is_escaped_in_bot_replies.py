"""Текст, который человек набрал сам, в ответ бота попадает экранированным.

Ответы бота идут в HTML (parse_mode по умолчанию). Имя, набранное как
«Shop <VIP>» или «A & B», без экранирования ломает разметку: Telegram
отвечает «can't parse entities», и человек вместо «✅ Готово» не видит
ничего, хотя действие уже выполнено. Так было в массовом переименовании
ботов (двух копиях), названии кластера, сделке CRM, конкуренте, получателе
подарков и эксперименте Stars.

Измеритель: в обработчике переменная, взятая из `message.text`, не может
стоять голой в f-строке ответа (`answer`/`edit_text`/`reply` и сборщиков
текста результата).
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HANDLERS = ROOT / "bot" / "handlers"

_SINKS = {"answer", "edit_text", "reply", "send_message", "_result_text"}
_ESCAPED = re.compile(r"escape|_esc\(|num_ru|int\(|float\(|len\(")


def _raw_typed_in_replies(src: str) -> list[str]:
    """Строка с пометкой «HTML намеренно» (пост, который владелец пишет
    разметкой) — исключение по смыслу, а не по месту."""
    lines = src.split("\n")
    tree = ast.parse(src)
    bad = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        typed: set[str] = set()
        for n in ast.walk(fn):
            if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
                v = ast.unparse(n.value)
                if "message.text" in v and not _ESCAPED.search(v):
                    typed.add(n.targets[0].id)
        if not typed:
            continue
        for call in ast.walk(fn):
            if not isinstance(call, ast.Call):
                continue
            f = call.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
            if name not in _SINKS or (isinstance(f, ast.Attribute) and "callback" in ast.unparse(f.value)
                                      and "message" not in ast.unparse(f.value)):
                continue
            for arg in call.args:
                for js in ast.walk(arg):
                    if not isinstance(js, ast.JoinedStr):
                        continue
                    for v in js.values:
                        if isinstance(v, ast.FormattedValue):
                            e = ast.unparse(v.value)
                            root = re.match(r"\w+", e)
                            if "HTML намеренно" in lines[v.lineno - 1]:
                                continue
                            if root and root.group(0) in typed and not _ESCAPED.search(e):
                                bad.append(f"{fn.name}:{v.lineno}: {{{e}}}")
    return bad


def test_typed_text_is_escaped_in_bot_replies():
    bad = []
    for p in sorted(HANDLERS.glob("*.py")):
        bad += [f"{p.name}:{x}" for x in _raw_typed_in_replies(p.read_text(encoding="utf-8"))]
    assert not bad, "набранный текст без экранирования в HTML-ответе:\n" + "\n".join(bad)


def test_probe_sees_raw_typed_name_and_spares_escaped():
    raw = (
        "async def h(message):\n"
        "    name = message.text.strip()\n"
        "    await message.answer(f'✅ Название: <b>{name}</b>')\n"
    )
    ok = raw.replace("{name}", "{html.escape(name)}")
    assert _raw_typed_in_replies(raw) == ["h:3: {name}"]
    assert _raw_typed_in_replies(ok) == []
