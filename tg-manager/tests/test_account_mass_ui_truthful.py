"""Массовые действия показывают реальный результат, включая ноль."""
from __future__ import annotations

from tests.miniapp_source import js_function


def test_zero_mass_result_is_not_replaced_with_selected_count():
    # Тело берём по скобкам: соседка purgeDeadAccounts уехала в
    # screens/accounts_list.js, и срез «до неё» больше не существует.
    body = js_function("runAccMass")

    assert "r.count||n" not in body
    assert "Number.isFinite(Number(r.count)) ? Number(r.count) : n" in body
