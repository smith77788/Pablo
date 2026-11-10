"""Операция жалоб не выдаёт ложный успех: 0 отправленных ≠ «выполнено».

Было: `_exec_mass_report` возвращал `{"status": "done"}` БЕЗУСЛОВНО — даже когда
ok_count==0 (не отправлено ни одной жалобы) или все цели упали. Операция, не
сделавшая ничего, показывалась владельцу зелёной. Это ложь продукта об исходе
(тот же класс, что «partial без счётчиков → выполнено»).

Стало: исход считается по ФАКТУ (счётчикам) — 0 отправлено = failed, часть не
прошла = partial. Этот тест не даёт вернуть безусловный «done».
"""
from __future__ import annotations

import inspect

from services import op_worker


def test_mass_report_outcome_is_by_counters_not_always_done():
    src = inspect.getsource(op_worker._exec_mass_report)
    # честная ветка: ни одной отправленной — провал
    assert 'ok_count == 0' in src and 'status = "failed"' in src, (
        "0 отправленных жалоб обязаны давать failed, а не done"
    )
    # часть не прошла — partial
    assert 'status = "partial"' in src, "частичный исход обязан быть partial"
    # финальный возврат отдаёт ВЫЧИСЛЕННЫЙ статус, а не литерал "done"
    assert 'return {"status": status' in src, (
        "исход должен возвращаться по счётчикам, а не безусловным \"done\""
    )
    # и прежней безусловной лжи больше нет
    assert 'return {"status": "done", "ok": ok_count' not in src


def test_report_peer_already_honest():
    # Сосед по классу: _exec_report_peer уже честен (0 ok → failed). Сторожим,
    # чтобы не деградировал обратно к безусловному успеху.
    src = inspect.getsource(op_worker._exec_report_peer)
    assert '"done" if ok_count else "failed"' in src
