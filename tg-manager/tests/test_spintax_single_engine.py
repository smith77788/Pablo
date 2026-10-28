"""Spintax: один движок и честная оценка разнообразия перед рассылкой.

В проекте есть полноценный `services/spintax_engine` (лексер, парсер,
валидатор) и обёртка `spintax_service`. Рядом жили две наивные копии на
`re.sub` + `random.choice`, и массовые операции использовали именно их:
`op_worker` импортировал `dm_engine.expand_spintax` в шести местах.

Наивная замена раскрывает внутреннюю группу раньше внешней и потому даёт не
тот вариант, который написал пользователь. Но и прямая замена на движок
недопустима: он валидирующий и бросает на текстах, которые в рассылках
встречаются постоянно — незакрытая скобка, `{{CITY}}` из генератора
инфраструктуры, пустая группа. Исключение означало бы оборванную посреди сети
рассылку.

Отсюда контракт фасада, который здесь и защищается: движок как основной путь,
прежнее поведение как фолбэк, никаких исключений наружу.
"""
from __future__ import annotations

import ast
import random
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DM = ROOT / "services" / "dm_engine.py"
PROFILE = ROOT / "services" / "profile_setter_engine.py"
MASS_OPS = ROOT / "bot" / "handlers" / "mass_ops.py"


def _load(path: Path, names: set[str], ns: dict | None = None):
    """Выдернуть функции из модуля без его тяжёлых импортов (aiogram, asyncpg)."""
    src = path.read_text(encoding="utf-8")
    code = "\n".join(
        ast.get_source_segment(src, n)
        for n in ast.parse(src).body
        if isinstance(n, ast.FunctionDef) and n.name in names
    )
    scope = {"re": re, "random": random, **(ns or {})}
    exec(code, scope)  # noqa: S102 — тестовая загрузка чистых функций
    return scope


_expand = _load(DM, {"expand_spintax", "_expand_spintax_naive"})["expand_spintax"]


# ── Фасад не бросает наружу ────────────────────────────────────────────────

@pytest.mark.parametrize(
    "text",
    [
        "{незакрытая",
        "Привет {}",
        "Новости {{CITY}}",          # плейсхолдер генератора инфраструктуры
        "{" * 40,
        "}{",
        "100% {скидка}",
        "",
        "обычный текст без скобок",
    ],
)
def test_facade_never_raises(text):
    """Рассылка не должна обрываться из-за скобки в пользовательском тексте.

    Движок на этих входах бросает — фасад обязан деградировать к прежнему
    поведению, а не наверх.
    """
    out = _expand(text)
    assert isinstance(out, str)


def test_facade_expands_simple_group():
    out = _expand("Привет {друг|коллега}!")
    assert out in ("Привет друг!", "Привет коллега!")


def test_nested_groups_resolved_outer_first():
    """Регресс наивной замены: она раскрывала внутреннюю группу первой.

    В `{вложенный {A|B}|C}` пользователь описал выбор между «вложенный {A|B}»
    и «C». Построчная regex-замена сначала схлопывала `{A|B}`, из-за чего
    вариант «C» вообще не мог выпасть.
    """
    seen = {_expand("{вложенный {A|B}|C}") for _ in range(60)}
    assert "C" in seen, f"внешняя альтернатива недостижима: {seen}"


def test_no_braces_left_for_valid_template():
    out = _expand("{А|Б} и {В|Г}")
    assert "{" not in out and "|" not in out


def test_pathological_input_terminates():
    # Ограничитель проходов: без него наивный цикл на «{{{{…» крутился бы,
    # держа воркер массовой операции.
    assert isinstance(_expand("{" * 200 + "a" + "}" * 200), str)


# ── Одна реализация, а не три ──────────────────────────────────────────────

def test_profile_setter_delegates_to_shared_facade():
    src = PROFILE.read_text(encoding="utf-8")
    body = src[src.find("def expand_spintax") : src.find("def expand_spintax") + 700]
    assert "dm_engine" in body, "профили должны использовать общий фасад"
    assert "random.choice" not in body, "третья копия разбора вернулась"


def test_facade_delegates_to_engine():
    src = DM.read_text(encoding="utf-8")
    body = src[src.find("def expand_spintax") :]
    assert "spintax_service" in body, "фасад обязан звать движок, а не свою регулярку"
    assert "except Exception" in body, "фасад обязан быть fail-soft"


def test_no_new_naive_spintax_implementations():
    """Гейт от расползания: четвёртая копия разбора не должна появиться.

    Ищем связку «regex по фигурным скобкам + random.choice» — именно её
    писали оба раза, когда движок был под рукой.
    """
    offenders = []
    for path in (ROOT / "services").rglob("*.py"):
        if "spintax_engine" in str(path):
            continue  # сам движок
        src = path.read_text(encoding="utf-8", errors="ignore")
        for m in re.finditer(r"random\.choice\(\s*m\.group\(1\)\.split\(\"\|\"\)", src):
            line = src[: m.start()].count("\n") + 1
            # Фолбэк фасада — единственное легитимное место.
            if path == DM and "_expand_spintax_naive" in src[max(0, m.start() - 600) : m.start()]:
                continue
            offenders.append(f"{path.relative_to(ROOT)}:{line}")
    assert not offenders, (
        f"наивная реализация spintax вне движка: {offenders}. "
        "Используйте services.dm_engine.expand_spintax или spintax_service."
    )


def test_worker_uses_shared_facade():
    src = (ROOT / "services" / "op_worker.py").read_text(encoding="utf-8")
    assert "from services.dm_engine import expand_spintax" in src, (
        "массовые операции обязаны идти через общий фасад"
    )


# ── Честная оценка разнообразия перед рассылкой ────────────────────────────

_hint = _load(MASS_OPS, {"spintax_diversity_hint"})["spintax_diversity_hint"]


def test_plain_text_warns_about_identical_message():
    """Одинаковый текст в сотнях каналов — самый заметный признак рассылки.

    Пользователь узнавал об этом только по последствиям.
    """
    out = _hint("Привет всем!", 500)
    assert "одинаковый" in out and "500" in out


def test_poor_template_reports_repeat_count():
    # Не «мало вариантов», а конкретно сколько раз повторится каждый.
    out = _hint("{А|Б}", 500)
    assert "250" in out, out


def test_rich_template_reassures():
    out = _hint("{А|Б|В|Г}{1|2|3|4}{x|y|z}", 5)
    assert "48" in out and "⚠️" not in out


def test_hint_silent_when_nothing_to_say():
    assert _hint("", 100) == ""
    assert _hint("текст", 1) == ""


@pytest.mark.parametrize(
    "n,expected", [(1, "цель"), (2, "цели"), (4, "цели"), (5, "целей"),
                   (11, "целей"), (21, "цель"), (100, "целей")]
)
def test_target_word_is_declined(n, expected):
    """«на 4 целей» читается как машинный текст — для продукта это заметно."""
    if n == 1:
        pytest.skip("при одной цели подсказка не показывается")
    out = _hint("Привет!", n)
    assert expected in out, out


def test_hint_is_wired_into_confirm_screen():
    """Подсказка бесполезна, если её никто не показывает."""
    src = MASS_OPS.read_text(encoding="utf-8")
    confirm = src[src.find('F.action == "mp_confirm"') :]
    assert "spintax_diversity_hint(" in confirm


def test_hint_survives_broken_template():
    # Оценка — вспомогательная: её сбой не должен ломать экран постановки.
    assert isinstance(_hint("{незакрытая", 100), str)
