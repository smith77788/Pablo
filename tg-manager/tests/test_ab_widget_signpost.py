"""Регресс: вестигиальный A/B-виджет на экране рассылок ведёт к реальной системе.

`renderBcAbList` читает `b.ab_wins_a/ab_wins_b/ab_variant`, но этих колонок нет ни
в схеме, ни в ответе `/api/miniapp/broadcasts`, ни в композере рассылки. Значит
`all.filter(b=>b.ab_variant)` ВСЕГДА пуст → виджет всегда показывал пустое состояние
с подсказкой «Включите A/B при создании рассылки» — фичи, которой на рассылках нет
(настоящий A/B — отдельная система `experiments` / openExperiments). Это вводящий в
заблуждение тупик (класс 4/5). Пустое состояние должно вести в реальную систему A/B.
"""
from __future__ import annotations

import re
from pathlib import Path

INDEX = Path(__file__).resolve().parents[1] / "mini_app" / "index.html"


def _render_src() -> str:
    html = INDEX.read_text(encoding="utf-8")
    m = re.search(r"function renderBcAbList\(items\)\s*\{.*?\n\}", html, re.DOTALL)
    assert m, "renderBcAbList не найдена"
    return m.group(0)


def test_ab_widget_points_to_real_experiments():
    src = _render_src()
    assert "openExperiments()" in src, (
        "пустое состояние A/B-виджета должно вести в реальную систему экспериментов"
    )
    assert "Включите A/B при создании рассылки" not in src, (
        "нельзя обещать A/B-тумблер в композере рассылки — его там нет"
    )
