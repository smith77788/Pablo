from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _handler_body(source: str, name: str) -> str:
    start = source.index(f"async def {name}")
    next_route = source.find("@router.", start + 1)
    return source[start:] if next_route == -1 else source[start:next_route]


def test_start_does_not_clear_active_fsm_state() -> None:
    source = (PROJECT_ROOT / "tg-manager/bot/handlers/start.py").read_text(
        encoding="utf-8"
    )

    assert "await state.clear()" not in _handler_body(source, "cmd_start")
    assert "await state.clear()" in _handler_body(source, "cmd_cancel")
