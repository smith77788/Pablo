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

# Коллекции, которые растут вместе с тарифом пользователя или с платформой.
# Тарифы, языки, статусы и прочие константы сюда не входят: их число задано
# кодом и не меняется от того, сколько у человека аккаунтов.
GROWING = {
    "accounts", "accs", "bots", "channels", "users", "groups", "sessions",
    "profiles", "targets", "available", "active", "recipients", "templates",
    "runs", "keys", "campaigns", "meshes", "funnels", "packs", "panels",
    "workspaces", "orders",
}


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
    assert len(capped) >= 50, f"обрезанных пикеров всего {len(capped)} — потолок растеряли"


def test_account_pickers_are_capped():
    """Ни один цикл по растущей коллекции не строит кнопку на каждый элемент."""
    bad = []
    for path, line, fn, it, fsrc in _picker_loops():
        if it not in GROWING:
            continue
        # Уже ограничено запросом, срезом или ранним выходом на постраничность —
        # тоже годится. Последнее — про `quick_pick_kb`: он строит кнопки только
        # когда список короткий, а длинный отдаёт постраничному варианту.
        if re.search(r"\bLIMIT\s+\d+|\[:\s*\d+\s*\]|islice\(", fsrc, re.I):
            continue
        if re.search(r"if\s+len\([^)]+\)\s*>\s*\w+\s*:\s*\n\s*return", fsrc):
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


def test_notice_knows_every_section_code():
    """Код раздела в подписи обработчик обязан уметь назвать словом."""
    handler = (BOT / "handlers" / "picker_cap_notice.py").read_text(encoding="utf-8")
    known = set(re.findall(r'"(\w+)":\s*"', handler))
    used = set()
    for path in sorted(BOT.rglob("*.py")):
        used |= set(re.findall(r'_cap_note\([^)]*what="(\w+)"', path.read_text(encoding="utf-8")))
    unknown = used - known
    assert not unknown, (
        f"подпись ссылается на разделы, которых обработчик не знает: {sorted(unknown)} "
        f"— человек увидит расплывчатое «объектов»"
    )


# ── То же самое, но в тексте сообщения ────────────────────────────────────────
#
# Telegram отвергает сообщение длиннее 4096 символов. Экран, клеящий строку на
# каждый объект, перестаёт открываться ровно так же, как клавиатура: у человека
# с двумя сотнями ботов в кластере «Сеть» просто не открывалась.

_ACCUMULATES = re.compile(r"\b\w+\s*\+=\s*[f\"']|\.append\(\s*f?[\"']")
_TEXT_BOUND = re.compile(
    r"\[:\s*\d+\s*\]|islice\(|\bLIMIT\s+\d+|_cap_text\(|_cap\(|"
    r"\blimit\s*=\s*\d+|\blimit\s*=\s*\w*(?:PAGE|SIZE|LIMIT)\w*|4096|3900|3500",
    re.I,
)


def test_message_text_does_not_grow_without_bound():
    """Ни один экран не клеит строку на каждый элемент растущей коллекции."""
    bad = []
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
            if _TEXT_BOUND.search(fsrc):
                continue
            if re.search(r"if\s+len\([^)]+\)\s*>\s*\w+\s*:\s*\n\s*return", fsrc):
                continue
            for node in ast.walk(fn):
                if not isinstance(node, (ast.For, ast.AsyncFor)):
                    continue
                it = (ast.get_source_segment(src, node.iter) or "").strip()
                if it not in GROWING:
                    continue
                body = ast.get_source_segment(src, node) or ""
                if not _ACCUMULATES.search(body):
                    continue
                bad.append(
                    f"    {path.relative_to(ROOT)}:{node.lineno}: {fn.name}() "
                    f"клеит строку на каждый элемент {it}"
                )
    assert not bad, (
        "сообщение перерастёт предел Telegram в 4096 символов:\n" + "\n".join(bad)
    )


def test_text_ceiling_keeps_the_message_sendable():
    """Мера: длина сообщения при потолке не зависит от числа объектов."""
    import sys

    sys.path.insert(0, str(ROOT))
    from bot.utils.picker_cap import TEXT_LIMIT, cap_text, cap_text_append  # noqa: WPS433

    assert TEXT_LIMIT <= 40, f"{TEXT_LIMIT} строк — это уже тысячи символов"

    def length(total: int) -> int:
        items = list(range(total))
        shown = cap_text(items)
        lines = ["🌐 <b>Кластер: main</b>", "<b>Боты:</b>"]
        for i in shown:
            lines.append(f"  🟢 @bot_with_a_longish_name_{i} [⚙️] — 12 345 юз.")
        cap_text_append(lines, items, shown, "ботов")
        return len("\n".join(lines))

    assert length(10_000) < 4096, f"сообщение всё ещё {length(10_000)} символов"
    assert abs(length(10_000) - length(1000)) <= 8, "длина зависит от числа объектов"


def test_capped_text_says_so():
    """Обрезали текст — сказали. Молчаливо укороченный список хуже длинного:
    человек видит тридцать ботов и думает, что их тридцать."""
    bad = []
    for path in sorted(BOT.rglob("*.py")):
        src = path.read_text(encoding="utf-8")
        if "_cap_text(" not in src:
            continue
        try:
            tree = ast.parse(src)
        except SyntaxError:  # pragma: no cover
            continue
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            fsrc = ast.get_source_segment(src, fn) or ""
            for m in re.finditer(r"_shown_t\s*=\s*_cap_text\((\w+)\)", fsrc):
                coll = m.group(1)
                if not re.search(rf"_cap_text_note\(\s*\w+\s*,\s*{coll}\s*,", fsrc):
                    bad.append(
                        f"    {path.relative_to(ROOT)}: {fn.name}() режет {coll} молча"
                    )
    assert not bad, "срез текста без строки об остатке:\n" + "\n".join(bad)
