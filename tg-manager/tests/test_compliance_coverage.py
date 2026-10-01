"""Комплаенс покрывает ВСЕ операции: подписанная запись на op_done choke point."""
from __future__ import annotations

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_op_worker_signs_every_completed_op():
    """У КАЖДОГО эмита op_done рядом стоит подпись того же самого исхода.

    Раньше проверялся только первый эмит, и подписанный исход сверялся с
    литералом `_final_status`. Когда у отмены появился свой финал
    (`_finish_cancelled_op`), первым стал он — проверка начала падать, хотя
    комплаенс как раз и был добавлен в этот путь. Поэтому смотрим все choke
    point'ы и требуем, чтобы подписывался ровно тот исход, который ушёл в
    событие шины: так правило не зависит ни от порядка функций в файле, ни от
    имени переменной.
    """
    src = open(os.path.join(ROOT, "services", "op_worker.py"), encoding="utf-8").read()
    sites = [m.start() for m in re.finditer(r'"op_done"', src)]
    assert sites, "choke point op_done исчез — комплаенс больше ничего не покрывает"
    for i in sites:
        line = src[:i].count("\n") + 1
        window = src[i:i + 800]
        assert "compliance_engine" in window and "record(" in window, (
            f"эмит op_done в строке {line} не подписывается в комплаенс-аудите")
        outcome = re.search(r'"status":\s*([A-Za-z_.][\w.]*)', window)
        assert outcome, f"в событии op_done (строка {line}) нет исхода операции"
        signed = window[window.index("record("):]
        assert outcome.group(1) in signed, (
            f"в строке {line} в аудит уходит не тот исход, что в событие: "
            f"ожидали {outcome.group(1)}")


def test_compliance_record_never_raises_contract():
    # record() задокументирован как «Never raises» — на него полагается choke point
    src = open(os.path.join(ROOT, "services", "compliance_engine.py"), encoding="utf-8").read()
    fn = src[src.index("async def record"):]
    assert "Never raises" in fn and "except Exception" in fn
