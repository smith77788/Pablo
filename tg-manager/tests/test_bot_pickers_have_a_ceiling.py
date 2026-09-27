# -*- coding: utf-8 -*-
"""Экран выбора аккаунта не растёт вместе с числом аккаунтов.

Экраны строили по кнопке на каждый аккаунт без верхней границы. На платных
тарифах число аккаунтов не ограничено, а Telegram принимает reply_markup
ограниченного размера: клавиатура из 10 кнопок весит 0,8 КБ, из 200 — 15 КБ,
из 1000 — 75 КБ. То есть экран не «становился неудобным» с ростом числа
аккаунтов — он переставал открываться, и человек видел одну ошибку.

Ничего в коде этого не ловило. `bot/utils/target_selector.py` умеет
постраничность с 2025 года, но им не пользовался ни один экран.

Потолок — не постраничность: выбрать аккаунт за его пределами по-прежнему
нельзя, и подпись об этом честно говорит, отправляя в мини-апп, где есть поиск
и страницы. Здесь проверяется, что потолок стоит везде и что молчаливым он не
бывает.
"""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BOT = ROOT / "bot"

# Коллекции, которые растут вместе с тарифом пользователя.
GROWING = {"accounts", "accs"}


def _picker_loops():
    """(файл, строка, функция, по чему цикл) для циклов, строящих кнопки."""
    out = []
    for path in sorted(BOT.rglob("*.py")):
        src = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(src)
        except SyntaxError:  # pragma: no cover
            continue
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            fsrc = ast.get_source_segment(src, fn) or ""
            for node in ast.walk(fn):
                if not isinstance(node, (ast.For, ast.AsyncFor)):
                    continue
                if not any(
                    isinstance(c, ast.Call)
                    and isinstance(c.func, ast.Attribute)
                    and c.func.attr == "button"
                    for c in ast.walk(node)
                ):
                    continue
                it = (ast.get_source_segment(src, node.iter) or "").strip()
                out.append((path.relative_to(ROOT), node.lineno, fn.name, it, fsrc))
    return out


def test_detector_sees_the_pickers():
    """Анти-пустота: без этого тест ниже прошёл бы на пустом списке."""
    loops = _picker_loops()
    assert len(loops) > 200, f"циклов с кнопками найдено {len(loops)} — сломан разбор"
    capped = [l for l in loops if l[3] == "_shown"]
    assert len(capped) >= 20, f"обрезанных пикеров всего {len(capped)} — потолок растеряли"


def test_account_pickers_are_capped():
    """Ни один цикл по аккаунтам не строит кнопки напрямую по всей коллекции."""
    bad = []
    for path, line, fn, it, fsrc in _picker_loops():
        if it not in GROWING:
            continue
        # уже ограничено запросом или срезом — тоже годится
        if re.search(r"\bLIMIT\s+\d+|\[:\s*\d+\s*\]|islice\(", fsrc, re.I):
            continue
        bad.append(f"    {path}:{line}: {fn}() строит кнопку на каждый элемент {it}")
    assert not bad, (
        "экран сломается, когда аккаунтов станет много:\n" + "\n".join(bad)
    )


def test_capped_pickers_say_so():
    """Обрезали — сказали. Молчаливое усечение хуже сломанного экрана."""
    bad = []
    for path, line, fn, it, fsrc in _picker_loops():
        if it != "_shown":
            continue
        if "_cap_note(" not in fsrc:
            bad.append(f"    {path}:{line}: {fn}() режет список молча")
    assert not bad, "срез без подписи об остатке:\n" + "\n".join(bad)


def test_ceiling_keeps_the_keyboard_within_telegram_limits():
    """Мера, ради которой всё затевалось: вес разметки при потолке."""
    aiogram = pytest.importorskip("aiogram")
    from aiogram.utils.keyboard import InlineKeyboardBuilder  # noqa: WPS433

    import sys

    sys.path.insert(0, str(ROOT))
    from bot.utils.picker_cap import cap_note_button, cap_slice  # noqa: WPS433

    def weight(total: int) -> int:
        items = list(range(total))
        shown = cap_slice(items)
        kb = InlineKeyboardBuilder()
        for i in shown:
            kb.button(text=f"🟢 +7999{i:06d}", callback_data=f"pick:{i}")
        cap_note_button(kb, items, shown)
        kb.button(text="◀️ Назад", callback_data="back")
        kb.adjust(1)
        m = kb.as_markup()
        return len(json.dumps(m.model_dump(exclude_none=True), ensure_ascii=False).encode())

    # Без потолка 1000 аккаунтов давали бы 75 КБ. С потолком вес не зависит
    # от числа аккаунтов вообще — это и есть починка.
    # Сам потолок — тоже часть гарантии, и проверять его надо до взвешивания:
    # задери его до тысяч, и весовой тест будет минуты строить клавиатуру,
    # вместо того чтобы сразу сказать, что именно сломано.
    from bot.utils.picker_cap import DEFAULT_LIMIT  # noqa: WPS433

    assert DEFAULT_LIMIT <= 60, (
        f"потолок {DEFAULT_LIMIT} кнопок — это уже килобайты разметки и "
        f"неподъёмный список на телефоне"
    )
    w_small, w_huge = weight(10), weight(1000)
    assert w_huge <= 4096, f"разметка при 1000 аккаунтов всё ещё {w_huge} байт"
    # От числа аккаунтов вес больше не зависит: растёт только счётчик в подписи,
    # то есть на длину числа. Разница между тысячей и десятью тысячами — байты,
    # а без потолка это были бы 75 КБ против 750.
    assert abs(w_huge - weight(200)) <= 8, (
        f"вес всё ещё зависит от числа аккаунтов: {weight(200)} → {w_huge}"
    )
    assert w_small < w_huge, "маленький список не должен весить как обрезанный"


def test_notice_button_explains_itself():
    """Подпись — не тупик: у неё есть обработчик, который говорит, что скрыто."""
    handler = (BOT / "handlers" / "picker_cap_notice.py").read_text(encoding="utf-8")
    assert "CapCb.filter()" in handler, "нажатие на подпись никто не обрабатывает"
    assert "show_alert=True" in handler, "ответ без всплывающего окна человек не заметит"
    main = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "picker_cap_notice_handler.router" in main, "роутер подписи не подключён"
