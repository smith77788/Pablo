"""Регрессия: Strike «выглядел неэффективным», потому что endpoint strike_history
читал только operation_queue (status + N/N) и не показывал реальные метрики.

_exec_strike пишет богатые метрики (peer_reported, msgs_reported, blocked,
verified_down, spambot_escalation, …) в ТАБЛИЦУ strike_history, но UI их не
видел. Теперь endpoint читает таблицу strike_history для завершённых страйков и
отдаёт metrics, а из operation_queue берёт только идущие (pending/running).
"""
from __future__ import annotations

import inspect
import re

from services import mini_app_api


def _handler_src() -> str:
    src = inspect.getsource(mini_app_api)
    m = re.search(r"async def strike_history\(.*?\n(.*?)async def ", src, re.DOTALL)
    assert m, "strike_history handler not found"
    return m.group(1)


def test_strike_history_reads_metrics_table():
    body = _handler_src()
    assert "FROM strike_history" in body, (
        "endpoint должен читать таблицу strike_history с реальными метриками"
    )
    # ключевые метрики эффективности должны выбираться
    for col in ("peer_reported", "verified_down", "blocked", "msgs_reported"):
        assert col in body, f"метрика {col} должна возвращаться из strike_history"


def test_strike_history_returns_metrics_object():
    body = _handler_src()
    assert '"metrics"' in body, "завершённые страйки должны нести объект metrics"
    assert "reports_total" in body, "должна считаться суммарная эффективность (жалобы)"


def test_strike_history_scoped_by_owner():
    body = _handler_src()
    # оба запроса (очередь + история) скоупятся по owner_id=$1
    assert body.count("owner_id=$1") >= 2, (
        "и очередь, и strike_history должны фильтроваться по owner_id (не утечка)"
    )
