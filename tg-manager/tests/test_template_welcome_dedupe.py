"""Пресеты ботов: не слать два приветствия на /start (класс #4 — шумный дубль).

Прод-скрин: на /start бот прислал ДВА почти одинаковых приветствия. Причина —
каждый бот-пресет несёт и welcome_message (стартовый автоответ), и funnel[0] с
delay_hours=0 почти тем же текстом. strip_duplicate_welcome_steps убирает 0-шаги,
когда стартовое приветствие уже есть и воронка триггерится на 'start'.
"""
from __future__ import annotations

import pathlib

from services.preset_templates import (
    get_presets,
    strip_duplicate_welcome_steps,
)


def test_strips_zero_delay_step_when_welcome_present():
    steps = [
        {"delay_hours": 0, "message": "привет-дубль"},
        {"delay_hours": 24, "message": "follow-up"},
        {"delay_hours": 72, "message": "follow-up2"},
    ]
    out = strip_duplicate_welcome_steps(
        steps, has_start_welcome=True, funnel_trigger="start"
    )
    assert [s["delay_hours"] for s in out] == [24, 72]


def test_keeps_all_when_no_start_welcome():
    steps = [{"delay_hours": 0, "message": "welcome-as-funnel"}, {"delay_hours": 24, "message": "x"}]
    out = strip_duplicate_welcome_steps(
        steps, has_start_welcome=False, funnel_trigger="start"
    )
    assert len(out) == 2  # без отдельного приветствия 0-шаг ЯВЛЯЕТСЯ приветствием


def test_keeps_all_when_trigger_not_start():
    steps = [{"delay_hours": 0, "message": "a"}, {"delay_hours": 24, "message": "b"}]
    out = strip_duplicate_welcome_steps(
        steps, has_start_welcome=True, funnel_trigger="keyword"
    )
    assert len(out) == 2


def test_every_bot_preset_has_no_duplicate_welcome_after_apply():
    """У каждого бот-пресета welcome_message есть → 0-шаг воронки уходит."""
    for p in get_presets("bot"):
        t = p["template"]
        assert t.get("welcome_message"), f"{p['id']}: ожидали welcome_message"
        out = strip_duplicate_welcome_steps(
            t.get("funnel_steps", []), has_start_welcome=True, funnel_trigger="start"
        )
        assert all(float(s.get("delay_hours", 0)) > 0 for s in out), (
            f"{p['id']}: остался 0-задержечный дубль приветствия"
        )
        # таймерные follow-up сохранены (воронка не выпотрошена в ноль)
        assert out, f"{p['id']}: воронка не должна стать пустой"


def test_both_apply_paths_call_dedupe():
    root = pathlib.Path(__file__).resolve().parents[1]
    bots = (root / "bot" / "handlers" / "bots.py").read_text(encoding="utf-8")
    at = (root / "bot" / "handlers" / "asset_templates.py").read_text(encoding="utf-8")
    assert "strip_duplicate_welcome_steps" in bots, "bots.py не дедуплицирует приветствие"
    assert "strip_duplicate_welcome_steps" in at, "asset_templates не дедуплицирует приветствие"
