"""Массовые действия показывают реальный результат, включая ноль."""
from __future__ import annotations

from pathlib import Path


def test_zero_mass_result_is_not_replaced_with_selected_count():
    html = (
        Path(__file__).resolve().parents[1] / "mini_app" / "index.html"
    ).read_text(encoding="utf-8")
    start = html.index("async function runAccMass")
    body = html[start:html.index("\nasync function purgeDeadAccounts", start)]

    assert "r.count||n" not in body
    assert "Number.isFinite(Number(r.count)) ? Number(r.count) : n" in body
