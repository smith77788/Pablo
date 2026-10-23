"""Прогресс-уведомления не должны спамить одной и той же операцией.

Жалоба: «уведомления об одной и той же операции каждую минуту». contacts_sync
(синхронизация 28 аккаунтов) держится в статусе running и не двигает done_items
во время прохода → _progress_monitor слал «0%» каждый цикл. Два барьера:
  1. обслуживающие op-типы (contacts_sync) вообще не мониторятся — только итог;
  2. монитор не шлёт одинаковый прогресс повторно (pct не изменился);
  3. сам синк ограничен по времени, чтобы не висеть в running бесконечно.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKER = (ROOT / "services" / "op_worker.py").read_text(encoding="utf-8")


def test_contacts_sync_is_quiet():
    assert re.search(r"_QUIET_PROGRESS_OPS\s*=\s*\{[^}]*\"contacts_sync\"", WORKER), \
        "contacts_sync не помечен как тихая операция"


def test_monitor_skipped_for_quiet_ops():
    """Монитор прогресса не запускается для обслуживающих операций."""
    assert "if op_type not in _QUIET_PROGRESS_OPS:" in WORKER, \
        "монитор запускается безусловно — тихие op снова будут спамить"
    # Проверяем, что охранник стоит ИМЕННО перед созданием монитора.
    idx = WORKER.index("_progress_monitor(pool, bot, op_id, owner_id, op_type)")
    guard_region = WORKER[idx - 260: idx]
    assert "_QUIET_PROGRESS_OPS" in guard_region, "нет охранника перед запуском монитора"


def test_monitor_dedupes_identical_progress():
    """Одинаковый прогресс не отправляется повторно (застрявшая операция)."""
    m = re.search(r"async def _progress_monitor\(.*?\n(?:async def |\Z)", WORKER, re.S)
    body = m.group(0)
    assert "_last_sent_pct" in body, "нет трекинга последнего отправленного прогресса"
    assert "pct != _last_sent_pct" in body, "нет защиты от повторной отправки того же %"


def test_contacts_sync_has_timeout():
    """Синк ограничен по времени — не держит слот воркера бесконечно."""
    m = re.search(r"async def _exec_contacts_sync\(.*?\n\n\nasync def ", WORKER, re.S)
    body = m.group(0)
    assert "wait_for(sync_all_accounts" in body, "нет таймаута вокруг синхронизации флота"
