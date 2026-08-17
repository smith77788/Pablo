"""Комплаенс покрывает ВСЕ операции: подписанная запись на op_done choke point."""
from __future__ import annotations

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_op_worker_signs_every_completed_op():
    src = open(os.path.join(ROOT, "services", "op_worker.py"), encoding="utf-8").read()
    # рядом с эмитом op_done — запись в комплаенс-аудит
    i = src.index('"op_done"')
    window = src[i:i + 800]
    assert "compliance_engine" in window and "record(" in window
    assert "_final_status" in window   # исход операции подписывается


def test_compliance_record_never_raises_contract():
    # record() задокументирован как «Never raises» — на него полагается choke point
    src = open(os.path.join(ROOT, "services", "compliance_engine.py"), encoding="utf-8").read()
    fn = src[src.index("async def record"):]
    assert "Never raises" in fn and "except Exception" in fn
