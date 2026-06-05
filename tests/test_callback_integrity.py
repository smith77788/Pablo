from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_bulk_empty_state_does_not_use_dead_legacy_callbacks() -> None:
    source = (PROJECT_ROOT / "tg-manager/bot/handlers/bulk.py").read_text(
        encoding="utf-8"
    )

    assert 'callback_data="bots_list"' not in source
    assert 'callback_data="bm_main"' not in source
    assert 'BotCb(action="list"' in source
    assert 'BmCb(action="main"' in source


def test_intent_navigation_uses_registered_targets() -> None:
    source = (PROJECT_ROOT / "tg-manager/bot/handlers/intent_engine.py").read_text(
        encoding="utf-8"
    )

    assert 'BmCb(action="accounts")' not in source
    assert 'BmCb(action="visibility")' not in source
    assert 'AccCb(action="menu")' in source
    assert 'VisCb(action="dashboard")' in source
