"""Терминальные экраны бота обязаны иметь выход.

96 мест в 37 хендлерах показывали финальный экран — «❌ Ошибка…», «✅ Готово…»,
«🚫 Отменено» — без единой кнопки. Выйти оттуда можно было только командой
`/menu`, которую надо знать. Для экрана ошибки это особенно плохо: человек уже
столкнулся с проблемой и вместо действия получает текст.

Гейт-храповик: число тупиков не должно расти. Ноль зафиксирован после разбора
всех 96 — любой новый терминальный экран без `reply_markup` уронит сборку.

Приглашения к вводу («Введите название…») тупиками не считаются: там ожидается
сообщение пользователя, а не нажатие.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HANDLERS = ROOT / "bot" / "handlers"

# Маркеры завершённого действия: экран показан и сценарий на нём кончается.
_TERMINAL_MARKERS = (
    "⚠️", "❌", "✅", "🚫", "Ошибка", "Готово", "Не удалось", "завершен", "Успешно",
)
# Экран ждёт ввода — пользователь отвечает сообщением, кнопка не обязательна.
_PROMPT = re.compile(r"Введите|Отправьте|Пришлите|Укажите")

# Достигнутое состояние. Двигать можно только ВНИЗ.
BASELINE_DEAD_ENDS = 0


def _dead_ends() -> list[str]:
    found: list[str] = []
    for path in sorted(HANDLERS.glob("*.py")):
        src = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(src)
        except SyntaxError:  # пусть падает профильный синтаксический гейт
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if not isinstance(fn, ast.Attribute):
                continue
            if fn.attr not in ("edit_text", "answer", "edit_caption"):
                continue
            if "message" not in ast.unparse(fn.value):
                continue
            if "reply_markup" in {kw.arg for kw in node.keywords}:
                continue
            text = ast.unparse(node.args[0]) if node.args else ""
            # Короткие строки — тосты и технические ответы, не экраны.
            if len(text) < 60:
                continue
            if not any(m in text for m in _TERMINAL_MARKERS):
                continue
            if _PROMPT.search(text):
                continue
            found.append(f"{path.name}:{node.lineno}")
    return found


def test_no_dead_end_screens():
    found = _dead_ends()
    assert len(found) <= BASELINE_DEAD_ENDS, (
        f"терминальных экранов без кнопок: {len(found)} (порог {BASELINE_DEAD_ENDS}).\n"
        + "\n".join(f"  {x}" for x in found[:20])
        + "\n\nДобавьте reply_markup=terminal_kb() — иначе пользователь упирается "
        "в экран и может только вспомнить /menu."
    )


def test_baseline_is_not_stale():
    """Порог не должен быть завышен: иначе храповик перестаёт защищать."""
    found = _dead_ends()
    assert len(found) >= BASELINE_DEAD_ENDS - 5, (
        f"тупиков стало заметно меньше порога ({len(found)} < {BASELINE_DEAD_ENDS}) — "
        "опустите BASELINE_DEAD_ENDS"
    )


# ── Сам хелпер ──────────────────────────────────────────────────────────────

def _helper_src(name: str) -> str:
    """Исходник хелпера: в тестовой среде aiogram застаблен, собрать реальную
    клавиатуру нельзя — проверяем контракт по коду."""
    src = (ROOT / "bot" / "utils" / "op_helpers.py").read_text(encoding="utf-8")
    for node in ast.parse(src).body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    raise AssertionError(f"{name} не найден в op_helpers")


def test_terminal_kb_always_offers_main_menu():
    """Выход в корень доступен всегда и не зависит от тарифа раздела."""
    body = _helper_src("terminal_kb")
    assert 'BmCb(action="main")' in body, "нет выхода в главное меню"
    # Кнопка в корень должна ставиться в ОБЕИХ ветках — и когда раздел известен,
    # и когда нет: иначе часть экранов снова остаётся без гарантированного выхода.
    assert body.count('BmCb(action="main")') >= 2


def test_terminal_kb_adds_section_back_when_known():
    body = _helper_src("terminal_kb")
    assert "if back is not None" in body
    assert "back_text" in body, "возврат в раздел должен быть подписываемым"


def test_retry_kb_puts_action_first():
    """У ошибки, которую можно повторить, действие важнее навигации."""
    body = _helper_src("retry_kb")
    first_btn = body.index("kb.button")
    assert "retry_text" in body[first_btn : first_btn + 120], (
        "первой кнопкой должно быть само действие, а не «Назад»"
    )


def test_main_menu_clears_wizard_state():
    """«В меню» из середины мастера обязано завершать мастер.

    Иначе пользователь визуально вышел, но остался в FSM: следующее сообщение
    уходит в брошенный шаг, и бот отвечает про поле, которое человек покинул.
    Кнопка «В меню» теперь стоит на 96 экранах — без очистки это стало бы
    массовой путаницей.
    """
    src = (HANDLERS / "botmother_menu.py").read_text(encoding="utf-8")
    idx = src.find('BmCb.filter(F.action == "main")')
    assert idx > 0
    body = src[idx : idx + 1200]
    assert "state.clear()" in body, "главное меню не завершает активный мастер"
    assert "FSMContext" in body, "обработчик не получает состояние"


def test_helper_is_actually_used():
    """Хелпер без применения — мёртвый код, а экраны остались бы тупиками."""
    users = [
        p.name
        for p in HANDLERS.glob("*.py")
        if "terminal_kb()" in p.read_text(encoding="utf-8")
    ]
    assert len(users) >= 30, f"хелпер применён лишь в {len(users)} модулях"


def test_every_user_imports_helper():
    """Использование без импорта = NameError на экране ошибки.

    Худший момент для падения: пользователь уже столкнулся с проблемой.
    """
    missing = []
    for p in HANDLERS.glob("*.py"):
        src = p.read_text(encoding="utf-8")
        if "terminal_kb()" not in src:
            continue
        if not re.search(r"^from bot\.utils\.op_helpers import [^\n]*\bterminal_kb\b", src, re.M):
            missing.append(p.name)
    assert not missing, f"terminal_kb используется без импорта: {missing}"
