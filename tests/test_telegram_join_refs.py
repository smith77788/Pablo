from __future__ import annotations

import sys
import os
from pathlib import Path


os.environ.setdefault("MANAGER_BOT_TOKEN", "test-token")
os.environ.setdefault("DATABASE_URL", "postgres://test:test@localhost/test")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tg-manager"))

from services.account_manager import normalize_telegram_join_ref


def test_normalize_private_invite_links() -> None:
    assert normalize_telegram_join_ref("https://t.me/+AbC-123") == (
        "invite",
        "AbC-123",
    )
    assert normalize_telegram_join_ref("https://t.me/joinchat/AbC_123?x=1") == (
        "invite",
        "AbC_123",
    )
    assert normalize_telegram_join_ref("tg://join?invite=AbC-123") == (
        "invite",
        "AbC-123",
    )
    assert normalize_telegram_join_ref("+AbC-123") == ("invite", "AbC-123")


def test_normalize_public_channel_refs() -> None:
    assert normalize_telegram_join_ref("@telegram") == ("public", "telegram")
    assert normalize_telegram_join_ref("https://t.me/telegram") == (
        "public",
        "telegram",
    )
    assert normalize_telegram_join_ref("https://t.me/telegram/123?single") == (
        "public",
        "telegram",
    )
    assert normalize_telegram_join_ref("https://t.me/s/telegram") == (
        "public",
        "telegram",
    )
