from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TG_MANAGER_ROOT = PROJECT_ROOT / "tg-manager"


def _schema_text() -> str:
    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(TG_MANAGER_ROOT.glob("schema*.sql"))
    )


def test_infra_health_tables_have_migrations() -> None:
    schema = _schema_text().lower()

    required_tables = (
        "recovery_events",
        "anomaly_events",
        "system_health_snapshots",
        "infrastructure_alerts",
    )
    for table in required_tables:
        assert f"create table if not exists {table}" in schema
