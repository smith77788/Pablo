from __future__ import annotations

import sys
import os
from pathlib import Path
from types import SimpleNamespace


os.environ.setdefault("DATABASE_URL", "postgres://test:test@localhost/test")
os.environ.setdefault("MANAGER_BOT_TOKEN", "test-token")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tg-manager"))

from services.account_manager import (
    _select_report_option_for_reason,
    format_telegram_join_ref_display,
    normalize_telegram_join_ref,
)
from services.strike_engine import format_mini_result


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


def test_format_private_invite_display_never_uses_public_at_prefix() -> None:
    assert format_telegram_join_ref_display("https://t.me/+AbC-123") == (
        "https://t.me/+AbC-123"
    )
    assert format_telegram_join_ref_display("+AbC-123") == "https://t.me/+AbC-123"
    assert format_telegram_join_ref_display("@telegram") == "@telegram"


def test_select_report_option_for_reason_prefers_matching_text() -> None:
    options = [
        SimpleNamespace(text="Other", option=b"other"),
        SimpleNamespace(text="Spam or advertising", option=b"spam"),
    ]

    assert _select_report_option_for_reason(options, "spam") == b"spam"


def test_mini_result_formats_private_invite_as_link_not_username() -> None:
    text = format_mini_result(
        {
            "target": "+QiQsOVYBgE1kMjli",
            "category_label": "Content",
            "severity": "normal",
            "tg": {},
            "emails": [],
            "abuse_form": {},
            "email_accounts_used": [],
            "total_tg_reports": 0,
            "total_emails": 0,
            "errors": [],
        }
    )

    assert "https://t.me/+QiQsOVYBgE1kMjli" in text
    assert "@+QiQsOVYBgE1kMjli" not in text
