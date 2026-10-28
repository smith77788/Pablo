"""Возобновлённая DM-кампания ПРОДОЛЖАЕТ прогресс, а не начинает с нуля.

Жалоба владельца (скрин): упавшая/дошедшая до потолка DM-кампания при
продолжении показывает «304 / 2,8K», хотя до остановки было «337 / 2,8K» —
выглядит как начатая заново. Отправки при этом НЕ дублируются (цели из
dm_campaign_log исключены в _get_targets), ломается именно отображение
прогресса: `_get_targets` отдаёт только НЕотправленных, поэтому `total` и
`done_items`, посчитанные от этого прогона, стартуют с нуля.

Фикс: прогресс считается КУМУЛЯТИВНО — already_done (обработанные в прошлых
прогонах) + этот прогон. total_targets на возобновлении не ужимается до
остатка (GREATEST). Движок по-прежнему не шлёт повторно (см.
test_dm_campaign_resume.test_run_campaign_is_resume_safe).
"""
from __future__ import annotations

import inspect

from services import dm_engine


def _run_campaign_src() -> str:
    return inspect.getsource(dm_engine.run_campaign)


def test_progress_counts_already_done_from_the_log():
    src = _run_campaign_src()
    # already_done берётся из dm_campaign_log по уже обработанным статусам.
    assert "already_done" in src, "кумулятив прошлых прогонов не вычисляется"
    assert "dm_campaign_log" in src and "status IN ('sent','blocked','skip','unconfirmed')" in src, (
        "already_done должен считать уже обработанные цели из журнала рассылки")


def test_total_is_cumulative_and_never_shrinks():
    src = _run_campaign_src()
    assert "already_done + len(targets)" in src, (
        "total обязан быть кумулятивным (обработанные + остаток), иначе на "
        "возобновлении «2800» превратится в остаток")
    # total_targets/total_items не ужимаются при возобновлении.
    assert "GREATEST(COALESCE(total_targets,0)" in src, (
        "total_targets ужимается до остатка — полная аудитория теряется")
    assert "GREATEST(COALESCE(total_items,0)" in src, (
        "total_items операции ужимается до остатка")


def test_done_items_is_cumulative_not_per_run():
    src = _run_campaign_src()
    # Полосу прогресса двигают кумулятивом, а не счётчиком этого прогона.
    assert "already_done + sent + failed" in src, (
        "done_items пишется как sent+failed (этот прогон) — на возобновлении "
        "полоса стартует с нуля")
    # И нигде не осталось записи голого per-run счётчика в done_items.
    import re
    bare = re.findall(r"done_items=\$1[\s\S]{0,80}?\n\s*sent \+ failed,", src)
    assert not bare, "осталась запись done_items по per-run счётчику sent+failed"


def test_milestones_use_cumulative_percent():
    """25/50/75% считаются от кумулятива — иначе на возобновлении шлются повторно."""
    src = _run_campaign_src()
    assert "_done = already_done + sent + failed" in src, (
        "процент вехи считается от этого прогона — на возобновлении 25/50/75 "
        "сработают повторно")


def test_final_summary_reports_cumulative_counts():
    """Итог берёт кумулятивные sent_count/fail_count кампании, не только этот прогон."""
    src = _run_campaign_src()
    assert "SELECT sent_count, fail_count FROM dm_campaigns" in src, (
        "итог кампании считает отправленное только за этот прогон")
