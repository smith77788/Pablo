"""Волна M: DM-кампания (dm_engine) не шлёт с карантинных аккаунтов (fail-open).
Завершает покрытие рефлекса пульса по всем массовым отправителям:
join+leave+publish+invite+strike+dm."""
from __future__ import annotations
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def test_dm_engine_respects_quarantine():
    de = _read("services/dm_engine.py")
    seg = de[de.index("acc_cycle = list(accounts)"):]
    seg = seg[:1200]
    assert "is_account_quarantined" in seg
    assert "if _kept and len(_kept) != len(acc_cycle)" in seg  # fail-open, не обнуляем
    assert "dm_engine campaign=%s: пропущено" in seg


def test_all_mass_senders_covered():
    """Все ключевые отправители уважают пульс."""
    ow = _read("services/op_worker.py")
    de = _read("services/dm_engine.py")
    se = _read("services/strike_engine.py")
    # op_worker: join+leave+publish+invite (≥4)
    assert ow.count("_infra_mem.is_account_quarantined(pool") >= 4
    # strike + dm
    assert "is_account_quarantined" in se
    assert "is_account_quarantined" in de
