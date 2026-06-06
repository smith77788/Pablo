from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("MANAGER_BOT_TOKEN", "test-token")
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")
os.environ.setdefault("TG_API_ID", "1")
os.environ.setdefault("TG_API_HASH", "test-hash")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tg-manager"))
PROJECT_ROOT = Path(__file__).resolve().parents[1]

from services.account_manager import (
    classify_spambot_reply,
    is_verified_account_restriction,
)
from services.flood_engine import (
    _flood_state,
    get_account_state,
    recommended_delay,
)


def test_classify_spambot_reply_detects_healthy_account() -> None:
    reply = "Good news, no limits are applied to your account."
    assert classify_spambot_reply(reply) == "active"


def test_classify_spambot_reply_detects_real_restriction() -> None:
    reply = "Your account is limited due to spam reports."
    assert classify_spambot_reply(reply) == "spamblock"


def test_verified_restriction_requires_real_status() -> None:
    assert is_verified_account_restriction("spamblock")
    assert is_verified_account_restriction("banned")
    assert is_verified_account_restriction("session_expired", has_session=True)
    assert not is_verified_account_restriction("session_expired", has_session=False)
    assert not is_verified_account_restriction("cooldown")


def test_recommended_delay_uses_safe_action_baseline() -> None:
    _flood_state.clear()
    assert recommended_delay(101, "join") >= 55.0
    assert recommended_delay(101, "leave") >= 35.0


def test_recommended_delay_grows_with_risk_and_cooldown() -> None:
    _flood_state.clear()
    state = get_account_state(202)
    state.risk_score = 0.8
    state.consecutive_floods = 3
    state.cooldown_until = 9999999999.0

    assert recommended_delay(202, "join") > 140.0


def test_status_persistence_uses_shared_verified_restriction_helper() -> None:
    account_health_source = (
        PROJECT_ROOT / "tg-manager/services/account_health.py"
    ).read_text(encoding="utf-8")
    dashboard_source = (
        PROJECT_ROOT / "tg-manager/bot/handlers/health_dashboard.py"
    ).read_text(encoding="utf-8")

    assert "is_verified_account_restriction(" in account_health_source
    assert "is_verified_account_restriction(" in dashboard_source
