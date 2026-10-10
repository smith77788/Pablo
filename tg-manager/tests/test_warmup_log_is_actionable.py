"""Журнал прогрева: с него можно действовать, а не только читать.

Экран «🌱 Что делает аккаунт» — то место, где видно, что прогрев сыплется:
красные строки, «Аккаунт в спам-блоке», пятьдесят неудач за неделю. И именно
на нём не было ни одной кнопки: пауза, продолжение и остановка жили в списке
прогрева, то есть на экран назад. Решение принимают здесь, а исполняют там.

Плюс две мелочи, из-за которых журнал читался наполовину: причина сбоя
обрезалась на 70 символах без шанса увидеть остаток, а среди шестидесяти
действий нельзя было оставить только упавшие.
"""
from __future__ import annotations

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = open(os.path.join(ROOT, "mini_app", "index.html"), encoding="utf-8").read()


def _fn(name: str) -> str:
    m = re.search(r"(?:async\s+)?function\s+" + re.escape(name) + r"\s*\(", HTML)
    assert m, f"функция {name} не найдена"
    i = HTML.index("{", m.end() - 1)
    depth = 0
    for j in range(i, len(HTML)):
        if HTML[j] == "{":
            depth += 1
        elif HTML[j] == "}":
            depth -= 1
            if depth == 0:
                return HTML[i:j + 1]
    raise AssertionError(name)


def test_detector_sees_the_screen():
    assert '<div class="screen" id="s-warmuplog">' in HTML
    assert len(_fn("openWarmupLog")) > 800


def test_plan_can_be_paused_and_stopped_from_the_log():
    f = _fn("openWarmupLog")
    for fn in ("pauseWarmupPlan(", "resumeWarmupPlan(", "cancelWarmupPlan("):
        assert fn in f, f"на экране журнала нет действия {fn}"
    # Кнопки показываются по состоянию плана, а не всегда.
    assert "p.status==='active'" in f and "p.status==='paused'" in f, f[:600]
    # Возобновление предлагается только тогда, когда движок его разрешает:
    # жать «Продолжить» на забаненном аккаунте — вредить ему.
    assert "h.resumable" in f, "кнопка «Продолжить» не спрашивает движок"


def test_full_error_is_reachable():
    """Обрезка на 70 символах прятала как раз хвост, где сказано, что делать."""
    t = _fn("wlogTap")
    assert "showInfo(" in t, t
    assert "70" in t, t


def test_action_row_opens_the_channel_it_touched():
    assert "openTgUser(" in _fn("wlogTap")
    assert "wlogTap(" in _fn("wlogRender"), "строка действия не нажимается"


def test_failures_can_be_shown_alone():
    f = _fn("openWarmupLog")
    assert "Неудачные" in f, "фильтра по неудачам нет"
    r = _fn("wlogRender")
    assert "_WL_FAIL_ONLY" in r and "a.success" in r, r


def test_filter_label_does_not_argue_with_the_weekly_summary():
    """Фильтр считает загруженные действия, сводка — неделю; это разные числа.

    Поэтому в пустом состоянии прямо сказано «из загруженных», иначе «0
    неудачных» под шапкой «7 неудачных за неделю» выглядит как поломка.
    """
    assert "Из загруженных действий" in _fn("wlogRender")


def test_circle_channels_are_openable():
    """Имя канала раньше приходилось переписывать руками."""
    assert "wlogOpenChannel(" in _fn("openWarmupLog")
    assert "openTgUser(" in _fn("wlogOpenChannel")


def test_external_names_do_not_go_into_onclick():
    """@username канала приходит из Telegram — в атрибуте это код."""
    f = _fn("openWarmupLog")
    assert "openTgUser('" not in f, f
    assert re.search(r"wlogOpenChannel\(\$\{i\}\)", f), f
