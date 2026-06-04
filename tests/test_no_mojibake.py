from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCAN_ROOTS = (PROJECT_ROOT / "tg-manager", PROJECT_ROOT / "tests")
MOJIBAKE_MARKERS = (
    "\u0420\u045f",
    "\u0420\u0452",
    "\u0420\u045c",
    "\u0420\u040e",
    "\u0420\u0402",
    "\u0420\u00a0",
    "\u0421\u0403",
    "\u0421\u0453",
    "\u0421\u201a",
    "\u0421\u0402",
    "\u0432\u0402",
    "\u0432\u045a",
    "\u0432\u045c",
    "\u0440\u045f",
)


def test_python_sources_do_not_contain_common_mojibake_markers() -> None:
    offenders: list[str] = []
    for root in SCAN_ROOTS:
        for path in root.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if any(marker in text for marker in MOJIBAKE_MARKERS):
                offenders.append(str(path.relative_to(PROJECT_ROOT)))

    assert offenders == []
