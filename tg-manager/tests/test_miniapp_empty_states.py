"""Пустые экраны мини-аппа не должны быть тупиками.

Функция `empty()` с самого начала принимала четвёртый аргумент — кнопку-CTA.
Из 207 вызовов её передавали 15. Остальные объясняли пользователю словами
(«Нажмите + Создать», «Добавьте канал через меню в Telegram») и не вели никуда,
а 73 из них — это вообще экран «Ошибка» с текстом исключения и без единой
кнопки: упёрся и можешь только закрыть мини-апп.

Здесь защищаются два свойства:
  1. выход есть ВСЕГДА — либо явный CTA, либо «Назад» из самой `empty()`;
  2. число экранов с осмысленным CTA не падает (храповик).
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "mini_app" / "index.html"

# Факт на момент введения гейта. Двигать ВНИЗ нельзя, ВВЕРХ — по мере того,
# как экраны получают осмысленный призыв к действию.
BASELINE_CTA = 19


def _html() -> str:
    return INDEX.read_text(encoding="utf-8")


def _call_args(src: str, start: int) -> list[str]:
    """Аргументы вызова по балансу скобок (регуляркой такое не разобрать)."""
    depth, buf, args, i = 0, "", [], start
    while i < len(src):
        c = src[i]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            if depth == 0:
                args.append(buf)
                break
            depth -= 1
        if c == "," and depth == 0:
            args.append(buf)
            buf = ""
            i += 1
            continue
        buf += c
        i += 1
    return args


def _empty_calls() -> list[list[str]]:
    src = _html()
    return [_call_args(src, m.end()) for m in re.finditer(r"\bempty\(", src)]


def test_empty_always_offers_a_way_out():
    """Регресс: `empty()` рисовала кнопку ТОЛЬКО при явном action.

    Это и делало 192 экрана тупиками. Теперь при отсутствии CTA подставляется
    «Назад» — но лишь когда есть куда возвращаться: на корневом экране кнопка
    была бы ложным обещанием.
    """
    src = _html()
    body = src[src.find("function empty(") :]
    body = body[: body.find("\n}") + 2]
    assert "back()" in body, "без явного CTA экран остаётся без выхода"
    assert "STACK" in body, "кнопка «Назад» обязана зависеть от глубины стека"


def test_cta_count_does_not_regress():
    calls = _empty_calls()
    with_cta = sum(1 for a in calls if len(a) >= 4 and a[3].strip())
    assert with_cta >= BASELINE_CTA, (
        f"экранов с призывом к действию стало меньше: {with_cta} < {BASELINE_CTA}. "
        "Пустое состояние без действия — тупик первого запуска."
    )


def test_cta_targets_exist():
    """CTA, ведущий в несуществующую функцию, — мёртвая кнопка.

    Гейт test_no_dead_onclick_handlers ловит onclick в разметке; здесь тот же
    контроль для функций, которые передаются в `empty()` параметром.
    """
    src = _html()
    missing = []
    for args in _empty_calls():
        if len(args) < 4 or not args[4 - 1].strip():
            continue
        m = re.search(r"fn\s*:\s*'([a-zA-Z_$][\w$]*)\(", args[3])
        if not m:
            continue
        fn = m.group(1)
        if not re.search(rf"(async\s+)?function\s+{re.escape(fn)}\s*\(|{re.escape(fn)}\s*=\s*(async\s*)?\(", src):
            missing.append(fn)
    assert not missing, f"CTA ведут в несуществующие функции: {sorted(set(missing))}"


def test_key_screens_have_meaningful_cta():
    """Ключевые пустые списки обязаны предлагать действие, а не инструкцию.

    «Добавьте канал через меню в Telegram» — это переадресация пользователя
    словами из приложения, которое само умеет создавать каналы.
    """
    src = _html()
    for marker in ("'📡','Нет каналов'", "'🔄','Нет воронок','Воронка", "'📢','Нет рассылок'"):
        idx = src.find(marker)
        assert idx > 0, f"экран {marker} не найден"
        window = src[idx : idx + 320]
        assert "label:" in window and "fn:" in window, (
            f"{marker}: пустое состояние без призыва к действию"
        )


def test_no_instructions_instead_of_buttons():
    """Текст-инструкция вместо кнопки — признак незавершённого экрана."""
    src = _html()
    assert "Добавьте канал через меню в Telegram" not in src, (
        "инструкция заменена кнопкой — старый текст не должен вернуться"
    )
