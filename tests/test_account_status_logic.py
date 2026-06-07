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
    effective_account_status,
    generate_device_fingerprint,
    is_verified_account_restriction,
    should_persist_account_status,
)
from bot.handlers.accounts import _display_acc_status
from services.flood_engine import (
    _flood_state,
    gaussian_delay,
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


def test_session_expired_persists_only_on_auth_error() -> None:
    assert should_persist_account_status("active")
    assert should_persist_account_status("spamblock")
    assert not should_persist_account_status("session_expired", has_session=True)
    assert should_persist_account_status(
        "session_expired",
        auth_error=True,
        has_session=True,
    )


def test_display_status_hides_stale_session_expired_for_active_session() -> None:
    assert (
        _display_acc_status(
            {
                "acc_status": "session_expired",
                "is_active": True,
                "has_session": True,
            }
        )
        == "active"
    )
    assert (
        _display_acc_status(
            {
                "acc_status": "session_expired",
                "is_active": False,
                "has_session": True,
            }
        )
        == "archived"
    )


def test_effective_account_status_normalizes_stale_and_missing_sessions() -> None:
    assert effective_account_status("session_expired", has_session=True) == "active"
    assert (
        effective_account_status("session_expired", has_session=False) == "no_session"
    )
    assert effective_account_status("active", is_active=False) == "archived"


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


def test_gaussian_delay_respects_bounds() -> None:
    for _ in range(25):
        delay = gaussian_delay(10.0, minimum=8.0, maximum=12.0)
        assert 8.0 <= delay <= 12.0


def test_device_fingerprint_binds_locale_to_country() -> None:
    de = generate_device_fingerprint("DE")
    ua = generate_device_fingerprint("UA")

    assert de["lang_code"] == "de"
    assert de["system_lang_code"] == "de-DE"
    assert ua["lang_code"] == "uk"
    assert ua["system_lang_code"] == "uk-UA"


def test_status_persistence_uses_shared_helper() -> None:
    account_health_source = (
        PROJECT_ROOT / "tg-manager/services/account_health.py"
    ).read_text(encoding="utf-8")
    dashboard_source = (
        PROJECT_ROOT / "tg-manager/bot/handlers/health_dashboard.py"
    ).read_text(encoding="utf-8")

    assert "should_persist_account_status(" in account_health_source
    assert "should_persist_account_status(" in dashboard_source


def test_accounts_handler_reloads_real_session_string_before_checks() -> None:
    accounts_source = (PROJECT_ROOT / "tg-manager/bot/handlers/accounts.py").read_text(
        encoding="utf-8"
    )

    assert (
        'session_str = (\n                (acc_dict.get("session_str") if acc_dict else None)'
        in accounts_source
    )
    assert (
        "result = await check_account_status_full(\n                session_str,"
        in accounts_source
    )
    assert (
        "result = await account_manager.scan_owned_assets(\n                session_str,"
        in accounts_source
    )


def test_spambot_status_flow_has_no_legacy_fallback_block() -> None:
    manager_source = (
        PROJECT_ROOT / "tg-manager/services/account_manager.py"
    ).read_text(encoding="utf-8")

    assert "spambot_status = classify_spambot_reply(reply_text)" in manager_source
    assert 'reply_lower = ""' not in manager_source


def test_missing_session_wording_is_neutral_not_misleading() -> None:
    manager_source = (
        PROJECT_ROOT / "tg-manager/services/account_manager.py"
    ).read_text(encoding="utf-8")
    dashboard_source = (
        PROJECT_ROOT / "tg-manager/bot/handlers/health_dashboard.py"
    ).read_text(encoding="utf-8")

    assert "session_str отсутствует" in manager_source
    assert "аккаунт не импортирован" not in manager_source
    assert "сессия недоступна для реальной проверки" in dashboard_source


def test_health_dashboard_groups_accounts_by_effective_status() -> None:
    dashboard_source = (
        PROJECT_ROOT / "tg-manager/bot/handlers/health_dashboard.py"
    ).read_text(encoding="utf-8")

    assert "acc_status = _effective_acc_status(acc)" in dashboard_source


def test_topology_uses_shared_effective_account_status() -> None:
    topology_source = (PROJECT_ROOT / "tg-manager/bot/handlers/topology.py").read_text(
        encoding="utf-8"
    )
    health_source = (PROJECT_ROOT / "tg-manager/services/account_health.py").read_text(
        encoding="utf-8"
    )

    assert (
        "from services.account_manager import effective_account_status"
        in topology_source
    )
    assert "effective_account_status(" in topology_source
    assert "effective_account_status(" in health_source


def test_purge_expired_revalidates_accounts_before_delete() -> None:
    accounts_source = (PROJECT_ROOT / "tg-manager/bot/handlers/accounts.py").read_text(
        encoding="utf-8"
    )

    assert (
        "DELETE FROM tg_accounts WHERE owner_id=$1 AND acc_status='session_expired' AND is_active=TRUE"
        not in accounts_source
    )
    assert "WHERE owner_id=$1 AND acc_status='session_expired'\"" in accounts_source
    assert (
        "result = await check_account_status_full(\n                session_str,"
        in accounts_source
    )


def test_trust_and_intelligence_do_not_treat_all_session_expired_as_dead() -> None:
    trust_source = (PROJECT_ROOT / "tg-manager/services/trust_engine.py").read_text(
        encoding="utf-8"
    )
    intelligence_source = (
        PROJECT_ROOT / "tg-manager/services/intelligence_engine.py"
    ).read_text(encoding="utf-8")
    advisor_source = (PROJECT_ROOT / "tg-manager/services/infra_advisor.py").read_text(
        encoding="utf-8"
    )

    assert "effective_account_status(" in trust_source
    assert "effective_account_status(" in intelligence_source
    assert "effective_account_status(" in advisor_source
    assert (
        "NOT IN ('spamblock', 'banned', 'deactivated', 'session_expired')"
        not in trust_source
    )
    assert (
        "NOT IN ('spamblock', 'banned', 'deactivated', 'session_expired')"
        not in intelligence_source
    )


def test_mtproto_queries_carry_proxy_and_locale_context() -> None:
    db_source = (PROJECT_ROOT / "tg-manager/database/db.py").read_text(encoding="utf-8")
    selector_source = (
        PROJECT_ROOT / "tg-manager/services/resource_selector.py"
    ).read_text(encoding="utf-8")
    pool_source = (PROJECT_ROOT / "tg-manager/services/session_pool.py").read_text(
        encoding="utf-8"
    )
    flood_source = (PROJECT_ROOT / "tg-manager/services/flood_engine.py").read_text(
        encoding="utf-8"
    )

    assert "lang_code" in db_source
    assert "system_lang_code" in db_source
    assert "proxy_id" in db_source
    assert "geo_country" in db_source
    assert "lang_code" in selector_source
    assert "system_lang_code" in selector_source
    assert "geo_country" in selector_source
    assert "lang_code" in pool_source
    assert "system_lang_code" in pool_source
    assert "geo_country" in pool_source
    assert "record_peer_flood" in flood_source
    assert "gaussian_delay" in flood_source


def test_account_manager_enforces_proxy_isolation_for_bound_sessions() -> None:
    manager_source = (
        PROJECT_ROOT / "tg-manager/services/account_manager.py"
    ).read_text(encoding="utf-8")

    assert "class ProxyIsolationError(ConnectionError):" in manager_source
    assert "Account proxy is required for this session" in manager_source
    assert (
        "Account proxy is configured, but its URL could not be parsed."
        in manager_source
    )


def test_op_worker_uses_penalty_requeue_and_long_peer_flood_isolation() -> None:
    worker_source = (PROJECT_ROOT / "tg-manager/services/op_worker.py").read_text(
        encoding="utf-8"
    )

    assert "_PEER_FLOOD_PATTERNS" in worker_source
    assert "_maybe_requeue(pool, op_id, e, params, op_type)" in worker_source
    assert "record_peer_flood(" in worker_source
    assert "_peer_flood_wait = 48 * 3600" in worker_source
