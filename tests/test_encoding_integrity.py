from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]

_MOJIBAKE_MARKERS = (
    chr(0xFFFD),
    chr(0x0420) + chr(0x045F),
    chr(0x0420) + chr(0x045C),
    chr(0x0420) + chr(0x045E),
    chr(0x0420) + chr(0x045B),
    chr(0x0420) + chr(0x201D),
    chr(0x0420) + chr(0x00A7),
    chr(0x0420) + chr(0x2014),
    chr(0x0432) + chr(0x045C),
    chr(0x0432) + chr(0x2020),
    chr(0x0432) + chr(0x201D),
    chr(0x0440) + chr(0x045F),
)


def test_python_sources_do_not_contain_common_mojibake() -> None:
    bad: list[str] = []
    for path in (PROJECT_ROOT / "tg-manager").rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="replace")
        for line_no, line in enumerate(text.splitlines(), start=1):
            if any(marker in line for marker in _MOJIBAKE_MARKERS):
                bad.append(f"{path.relative_to(PROJECT_ROOT)}:{line_no}")

    assert bad == []
