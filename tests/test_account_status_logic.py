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
    account_rank_score,
    gaussian_delay,
    get_account_state,
    min_trust_for_action,
    normalize_trust_score,
    recommended_delay,
)
from services.account_readiness import calculate_readiness, is_ready_for_action


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
    assert effective_account_status("no_session", has_session=True) == "active"
    assert effective_account_status("no_session", has_session=False) == "no_session"
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


def test_trust_score_is_normalized_to_zero_one_range() -> None:
    assert normalize_trust_score(None) == 0.0
    assert normalize_trust_score(0.73) == 0.73
    assert normalize_trust_score(73) == 0.73
    assert normalize_trust_score(150) == 1.0


def test_account_ranking_uses_full_normalized_trust_weight() -> None:
    _flood_state.clear()
    get_account_state(1).risk_score = 0.10
    get_account_state(2).risk_score = 0.10

    assert account_rank_score(1, 0.90) < account_rank_score(2, 0.20)
    assert account_rank_score(1, 90) == account_rank_score(1, 0.90)


def test_capacity_planner_uses_normalized_trust_scores() -> None:
    capacity_source = (
        PROJECT_ROOT / "tg-manager/services/capacity_planner.py"
    ).read_text(encoding="utf-8")

    assert "normalize_trust_score(" in capacity_source
    assert "avg_trust / 100.0" not in capacity_source
    assert "or 50" not in capacity_source


def test_outbound_actions_require_readiness_thresholds() -> None:
    assert min_trust_for_action("invite") >= 0.50
    assert min_trust_for_action("dm_campaign") >= 0.50
    assert min_trust_for_action("join") >= 0.35
    assert min_trust_for_action("mass_publish") >= 0.25


def test_readiness_blocks_missing_sessions_and_low_quality_outbound() -> None:
    missing = {
        "id": 1,
        "is_active": True,
        "session_str": None,
        "acc_status": "active",
        "trust_score": 1.0,
    }
    assert calculate_readiness(missing).level == "blocked"
    assert not is_ready_for_action(missing, "invite")

    ready = {
        "id": 2,
        "is_active": True,
        "session_str": "session",
        "acc_status": "active",
        "trust_score": 0.72,
        "proxy_id": 10,
        "proxy_url": "socks5://127.0.0.1:1080",
    }
    assert calculate_readiness(ready, successes_7d=8, failures_7d=0).level in {
        "ready",
        "veteran",
    }
    assert is_ready_for_action(ready, "invite", successes_7d=8, failures_7d=0)


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

    assert 'db.get_account_for_telethon(pool, acc["id"], uid)' in accounts_source
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
    assert 'if acc_status == "no_session" or not has_session:' in dashboard_source


def test_successful_account_login_resets_stale_session_status() -> None:
    db_source = (PROJECT_ROOT / "tg-manager/database/db.py").read_text(encoding="utf-8")

    assert "acc_status='active'" in db_source
    assert "status_reason=NULL" in db_source


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
    assert "await db.get_tg_accounts(pool, uid)" in accounts_source
    assert 'if acc.get("acc_status") == "session_expired"' in accounts_source
    assert (
        'acc_dict = await db.get_account_for_telethon(pool, acc["id"], uid)'
        in accounts_source
    )
    assert (
        "result = await check_account_status_full(\n                session_str,"
        in accounts_source
    )


def test_get_tg_accounts_returns_full_session_material() -> None:
    db_source = (PROJECT_ROOT / "tg-manager/database/db.py").read_text(encoding="utf-8")
    get_accounts_block = db_source[
        db_source.index("async def get_tg_accounts") : db_source.index(
            "async def update_acc_status"
        )
    ]

    assert "session_str, " in get_accounts_block
    assert (
        "(session_str IS NOT NULL AND session_str <> '') AS has_session"
        in get_accounts_block
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
    assert '("spamblock", "banned", "deactivated", "no_session")' in intelligence_source
    assert (
        "NOT IN ('spamblock', 'banned', 'deactivated', 'session_expired')"
        not in trust_source
    )
    assert (
        "NOT IN ('spamblock', 'banned', 'deactivated', 'session_expired')"
        not in intelligence_source
    )
    assert (
        '("spamblock", "banned", "deactivated", "session_expired")'
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
    geo_router_source = (PROJECT_ROOT / "tg-manager/services/geo_router.py").read_text(
        encoding="utf-8"
    )
    health_source = (PROJECT_ROOT / "tg-manager/services/account_health.py").read_text(
        encoding="utf-8"
    )
    channel_ops_source = (
        PROJECT_ROOT / "tg-manager/bot/handlers/channel_ops.py"
    ).read_text(encoding="utf-8")

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
    assert "lang_code" in geo_router_source
    assert "system_lang_code" in geo_router_source
    assert "proxy_id" in geo_router_source
    assert "lang_code" in health_source
    assert "system_lang_code" in health_source
    assert "proxy_id" in health_source
    assert "lang_code" in channel_ops_source
    assert "system_lang_code" in channel_ops_source
    assert "proxy_id" in channel_ops_source
    assert "proxy_url" in channel_ops_source
    assert "geo_country" in channel_ops_source
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
    selector_source = (
        PROJECT_ROOT / "tg-manager/services/resource_selector.py"
    ).read_text(encoding="utf-8")
    flood_source = (PROJECT_ROOT / "tg-manager/services/flood_engine.py").read_text(
        encoding="utf-8"
    )

    assert "_PEER_FLOOD_PATTERNS" in worker_source
    assert "_maybe_requeue(pool, op_id, e, params, op_type)" in worker_source
    assert "record_peer_flood(" in worker_source
    assert "_peer_flood_wait = 48 * 3600" in worker_source
    assert "min_trust_for_action(action_type)" in selector_source
    assert "account_rank_score(" in flood_source
    assert 'action_type="join"' in worker_source
    assert 'action_type="mass_publish"' in worker_source


def test_legacy_active_account_helper_uses_resource_selector() -> None:
    helper_source = (PROJECT_ROOT / "tg-manager/bot/utils/op_helpers.py").read_text(
        encoding="utf-8"
    )

    assert "from services import resource_selector" in helper_source
    assert "resource_selector.select_all_active(" in helper_source
    assert "FROM tg_accounts a" not in helper_source


def test_warmup_supplements_actions_with_readiness_refresh() -> None:
    warmer_source = (PROJECT_ROOT / "tg-manager/services/account_warmer.py").read_text(
        encoding="utf-8"
    )
    readiness_source = (
        PROJECT_ROOT / "tg-manager/services/account_readiness.py"
    ).read_text(encoding="utf-8")

    assert "refresh_account_readiness(pool, account_id, owner_id)" in warmer_source
    assert "ReadinessResult" in readiness_source
    assert "It does not create artificial Telegram activity." in readiness_source


def test_account_cleaner_reloads_full_account_context() -> None:
    cleaner_source = (
        PROJECT_ROOT / "tg-manager/bot/handlers/account_cleaner.py"
    ).read_text(encoding="utf-8")

    assert "from database import db" in cleaner_source
    assert "async def _get_telethon_account(" in cleaner_source
    assert "db.get_account_for_telethon(pool, account_id, owner_id)" in cleaner_source
    assert (
        "SELECT session_str, device_model, system_version, app_version, phone, first_name "
        not in cleaner_source
    )


def test_more_handlers_reload_full_telethon_account_context() -> None:
    channel_factory_source = (
        PROJECT_ROOT / "tg-manager/bot/handlers/channel_factory.py"
    ).read_text(encoding="utf-8")
    group_factory_source = (
        PROJECT_ROOT / "tg-manager/bot/handlers/group_factory.py"
    ).read_text(encoding="utf-8")
    activity_engine_source = (
        PROJECT_ROOT / "tg-manager/services/activity_engine.py"
    ).read_text(encoding="utf-8")
    account_warmer_source = (
        PROJECT_ROOT / "tg-manager/services/account_warmer.py"
    ).read_text(encoding="utf-8")
    invite_engine_source = (
        PROJECT_ROOT / "tg-manager/services/invite_engine.py"
    ).read_text(encoding="utf-8")

    assert (
        "db.get_account_for_telethon(pool, acc_id, callback.from_user.id)"
        in channel_factory_source
    )
    assert group_factory_source.count("db.get_account_for_telethon(") >= 3
    assert "db.get_account_for_telethon(pool, acc_id)" in activity_engine_source
    assert "db.get_account_for_telethon(pool, account_id)" in account_warmer_source
    assert account_warmer_source.count("db.get_account_for_telethon(pool, acc_id)") >= 1
    assert (
        'db.get_account_for_telethon(pool, acc_ref["id"], owner_id)'
        in invite_engine_source
    )


def test_mass_publish_isolates_network_failed_accounts() -> None:
    worker_source = (PROJECT_ROOT / "tg-manager/services/op_worker.py").read_text(
        encoding="utf-8"
    )

    assert "_NETWORK_PATTERNS" in worker_source
    assert "_record_network_isolation(" in worker_source
    assert 'if result.get("proxy_error"):' in worker_source
    assert "isolated_accounts: set[int] = set()" in worker_source
    assert "if acc is None:" in worker_source
    assert "await release_accounts(mp_used_acc_ids)" in worker_source


def test_mass_publish_can_fallback_between_candidate_accounts() -> None:
    worker_source = (PROJECT_ROOT / "tg-manager/services/op_worker.py").read_text(
        encoding="utf-8"
    )

    mass_publish_source = worker_source[
        worker_source.index("async def _exec_mass_publish") : worker_source.index(
            "async def _exec_bulk_join"
        )
    ]
    assert "SELECT DISTINCT ON (mc.channel_id)" not in mass_publish_source
    assert "target_map: dict[int, dict] = {}" in mass_publish_source
    assert 'target_map[channel_id]["accounts"].append(acc)' in mass_publish_source
    assert "for fallback_acc in candidate_accounts[1:]" in mass_publish_source
    assert (
        "fallback_result = await account_manager.post_to_channel("
        in mass_publish_source
    )


def test_subscription_handler_uses_module_admin_lookup() -> None:
    source = (PROJECT_ROOT / "tg-manager/bot/handlers/subscription.py").read_text(
        encoding="utf-8"
    )

    assert "from bot.utils import subscription as sub_utils" in source
    assert "sub_utils.is_platform_admin(" in source
    assert "sub_utils.get_plan(" in source
