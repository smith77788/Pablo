"""Регресс: экран, в который нельзя попасть, — это не экран, а мусор.

Что случилось. Второй дашборд («📈 Дашборд метрик», `s-analytics-dashboard`)
свели в единый хаб `openUnifiedDashboard` и убрали вход в него — но саму
разметку экрана и 186 строк его кода забыли. Получилось худшее из состояний:
кода нет в работе, а в файле он есть. Его правили бы при рефакторингах, он
попадал бы в поиск, он весил в каждой загрузке мини-аппа, и любой следующий
агент мог бы «починить» его и вернуть второй дашборд, который владелец просил
убрать. Комментарий рядом («Второй «Дашборд метрик» удалён») уже утверждал, что
экрана нет, — и был неправдой ровно на 235 строк.

Что проверяем. Каждый экран, объявленный в разметке, должен быть достижим:
либо его открывают явным `push('s-…')`, либо он — корень вкладки из
`TAB_SCREENS`. Проверка нарочно узкая: имя экрана, встреченное где-то в коде
(например, внутри `if (STACK[STACK.length-1] === 's-analytics-dashboard')`),
достижимости НЕ даёт — именно так мёртвый дашборд и выглядел «живым» при
обычном поиске по строке.

Детектор проверен на здоровом наборе: из 163 экранов он находил ровно один —
тот самый. Если он однажды начнёт выдавать длинный список, сначала чините
детектор, а не экраны.
"""
from __future__ import annotations

import re

from tests.miniapp_source import miniapp_html, miniapp_source


def _declared_screens() -> set[str]:
    """Экраны, объявленные в разметке."""
    # у стартового экрана class="screen show" — поэтому [^"]*, а не точное совпадение
    return set(re.findall(r'<div class="screen[^"]*"[^>]*id="([^"]+)"', miniapp_html()))


def _pushed_screens(src: str) -> set[str]:
    """Экраны, открываемые явным push('s-…')."""
    return set(re.findall(r"""push\(\s*['"]([^'"]+)['"]""", src))


def _tab_roots(src: str) -> set[str]:
    """Корни вкладок нижней навигации — их открывает goTab, а не push."""
    m = re.search(r"TAB_SCREENS\s*=\s*\[([^\]]*)\]", src)
    assert m, "не найден список TAB_SCREENS — проверка достижимости ослепла"
    return set(re.findall(r"""['"]([^'"]+)['"]""", m.group(1)))


def test_every_screen_is_reachable():
    src = miniapp_source()
    screens = _declared_screens()
    assert len(screens) > 100, f"экранов подозрительно мало ({len(screens)}) — сломан разбор разметки"
    unreachable = sorted(screens - _pushed_screens(src) - _tab_roots(src))
    assert not unreachable, (
        "Экраны есть в разметке, но открыть их нечем — ни одного push('…'), "
        "ни строки в TAB_SCREENS: " + ", ".join(unreachable) + ". "
        "Либо заведите вход, либо удалите экран вместе с его кодом."
    )


def test_tab_roots_exist_in_markup():
    """Вкладка, ведущая в несуществующий экран, — пустой низ приложения."""
    missing = sorted(_tab_roots(miniapp_source()) - _declared_screens())
    assert not missing, f"вкладка ведёт в несуществующий экран: {missing}"


def test_second_dashboard_stays_removed():
    """Владелец просил один дашборд. Второй не возвращать."""
    src = miniapp_source()
    for dead in ("s-analytics-dashboard", "startDashAutoRefresh", "openAnalyticsDashboard"):
        assert dead not in src, (
            f"вернулся второй дашборд ({dead}) — его метрики уже во вкладке "
            "«📈 Аналитика» единого хаба openUnifiedDashboard"
        )
    assert "openUnifiedDashboard" in src, "пропал единый дашборд"
