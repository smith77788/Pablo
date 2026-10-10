"""Честное зеркало: прогресс операции не показывает done>total («34/17») и pct>100%.

Корень (накопление done_items при ретрае) уже пофикшен в бэкенде, но старые операции
в БД уже имеют done>total. Фронт защитно клампит показ: dDone=min(done,total),
pct=min(100,…) — и в списке, и в детали.
"""
from __future__ import annotations

import re
from pathlib import Path

HTML = (Path(__file__).resolve().parent.parent / "mini_app" / "index.html").read_text(encoding="utf-8")


def test_list_and_detail_clamp_pct_to_100():
    assert HTML.count("Math.min(100,Math.round((o.done_items||0)/o.total_items*100))") >= 2, \
        "pct должен клампиться ≤100 и в списке, и в детали"


def test_clamped_done_used_in_progress_display():
    # dDone = min(done, total) вычисляется в обоих местах
    assert HTML.count("Math.min(o.done_items||0,o.total_items)") >= 2
    # список показывает dDone, а не сырой done_items
    assert "num(dDone)+'/'+num(o.total_items||0)" in HTML
    # деталь показывает dDone в строке «Прогресс». Точное написание строки здесь
    # больше не годится: числа обёрнуты в <span> с якорями data-op-cnt/data-op-pct,
    # чтобы живой прогресс по SSE обновлял открытую карточку, а не замирал на
    # момент открытия. Проверяем состав строки, а не её буквальный текст.
    row = re.search(r">Прогресс<.{0,400}?</div></div>", HTML, re.DOTALL)
    assert row, "строка «Прогресс» в карточке операции не найдена"
    cell = row.group(0)
    assert "num(dDone)" in cell, "карточка показывает сырой done_items вместо зажатого"
    assert "num(o.total_items||0)" in cell, "в карточке пропал знаменатель"
    assert "${pct}" in cell, "в карточке пропал процент"
