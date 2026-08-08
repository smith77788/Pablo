from __future__ import annotations

import os
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("MANAGER_BOT_TOKEN", "test-token")
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")
os.environ.setdefault("TG_API_ID", "1")
os.environ.setdefault("TG_API_HASH", "test-hash")
sys.path.insert(0, str(PROJECT_ROOT / "tg-manager"))

from bot.handlers.botmother_menu import _format_progress_bar


def test_operation_progress_bar_is_mobile_safe_ascii() -> None:
    assert _format_progress_bar(0, 10) == "----------"
    assert _format_progress_bar(5, 10) == "#####-----"
    assert _format_progress_bar(10, 10) == "##########"
    assert _format_progress_bar(12, 10) == "##########"
    assert _format_progress_bar(1, 0) == "----------"


def test_operation_detail_does_not_use_block_glyph_progress() -> None:
    source = (PROJECT_ROOT / "tg-manager/bot/handlers/botmother_menu.py").read_text(
        encoding="utf-8"
    )
    detail_body = source[
        source.index("async def cb_op_detail") : source.index(
            "@router.callback_query(BmCb.filter(F.action == \"op_retry\"))"
        )
    ]

    assert '"█"' not in detail_body
    assert '"░"' not in detail_body
    assert "_format_progress_bar(done, total)" in detail_body
