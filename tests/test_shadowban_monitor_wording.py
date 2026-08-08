from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_flood_history_is_not_reported_as_verified_restriction() -> None:
    source = (PROJECT_ROOT / "tg-manager/services/shadowban_monitor.py").read_text(
        encoding="utf-8"
    )

    assert "account_flood_risk" in source
    assert "Высокий риск лимитов аккаунта" in source
    assert "Это не подтверждённый спамблок" in source
    assert "Аккаунт под ограничением" not in source
    assert '"account_restricted"' not in source
