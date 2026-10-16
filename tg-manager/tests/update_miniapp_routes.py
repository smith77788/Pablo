"""Обновить слепок маршрутов мини-аппа. Запускать осознанно, из tg-manager:

    python tests/update_miniapp_routes.py

Дифф слепка должен ехать в том же коммите, что и код, который его меняет.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from miniapp_routes import registered_routes  # noqa: E402

rows = registered_routes()
out = ROOT / "tests" / "miniapp_routes_snapshot.txt"
out.write_text("\n".join(rows) + "\n", encoding="utf-8")
print(f"записано маршрутов: {len(rows)} → {out.relative_to(ROOT)}")
