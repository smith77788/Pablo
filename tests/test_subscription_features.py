from __future__ import annotations

import sys
from pathlib import Path
import asyncio
import os


os.environ.setdefault("MANAGER_BOT_TOKEN", "test-token")
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")
os.environ.setdefault("TG_API_ID", "1")
os.environ.setdefault("TG_API_HASH", "test-hash")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tg-manager"))

from bot.utils.subscription import (
    BOT_LIMITS,
    PLAN_LEVELS,
    coerce_plan,
    feature_required_plan,
    get_bot_limit,
    get_free_mode,
    get_plan,
    invalidate_plan_cache,
    normalize_plan,
    require_plan,
    set_free_mode,
)
from bot.handlers.subscription import _usdt_reference_offset


class FakePlanPool:
    def __init__(self, plan: str | None) -> None:
        self.plan = plan

    async def fetchrow(self, _query: str, _user_id: int) -> dict[str, str] | None:
        if self.plan is None:
            return None
        return {"plan": self.plan}


def test_max_alias_maps_to_enterprise() -> None:
    assert normalize_plan("max") == "enterprise"
    assert normalize_plan("maximum") == "enterprise"


def test_unknown_plan_is_never_promoted() -> None:
    assert coerce_plan("vip") == "free"
    assert coerce_plan("enterprise_plus") == "free"


def test_core_revenue_features_require_highest_plan() -> None:
    for feature in (
        "ai_assistant",
        "autonomous_engine",
        "global_presence",
        "swarm",
        "workspaces",
        "strike",
        "email_oauth",
        "infra_intelligence",
    ):
        assert feature_required_plan(feature) == "enterprise"


def test_free_plan_is_demo_not_full_product() -> None:
    assert BOT_LIMITS["free"] == 1
    assert feature_required_plan("basic_broadcast") == "starter"

    accounts_source = (
        Path(__file__)
        .resolve()
        .parents[1]
        .joinpath("tg-manager/bot/handlers/accounts.py")
        .read_text(encoding="utf-8")
    )
    assert '"free": 0' in accounts_source
    assert '"starter": 1' in accounts_source
    assert '"pro": 3' in accounts_source
    assert "def _next_account_plan" in accounts_source


def test_global_free_mode_requires_explicit_env_opt_in(monkeypatch) -> None:
    monkeypatch.delenv("ALLOW_GLOBAL_FREE_MODE", raising=False)
    set_free_mode(True)
    assert get_free_mode() is False

    monkeypatch.setenv("ALLOW_GLOBAL_FREE_MODE", "true")
    set_free_mode(True)
    assert get_free_mode() is True
    set_free_mode(False)


def test_admin_free_mode_persists_actual_guarded_state() -> None:
    source = (
        Path(__file__)
        .resolve()
        .parents[1]
        .joinpath("tg-manager/bot/handlers/admin.py")
        .read_text(encoding="utf-8")
    )
    block = source[
        source.index('elif action == "free_mode_toggle"') : source.index(
            'elif action == "block_ask"'
        )
    ]

    assert "actual_state = get_free_mode()" in block
    assert '"true" if actual_state else "false"' in block
    assert '"true" if new_state else "false"' not in block
    assert "ALLOW_GLOBAL_FREE_MODE=true" in block


def test_revenue_entrypoints_have_plan_gates() -> None:
    source = (
        Path(__file__)
        .resolve()
        .parents[1]
        .joinpath("tg-manager/bot/handlers/botmother_menu.py")
        .read_text(encoding="utf-8")
    )

    for handler in (
        "async def cb_comms",
        "async def cb_broadcasts",
        "async def cb_inbox",
        "async def cb_ai_assistant",
    ):
        start = source.index(handler)
        body = source[start : source.index("@router.callback_query", start + 1)]
        assert "await require_plan(" in body
        assert "subscription_locked_markup(" in body


def test_proxy_manager_actions_require_pro_plan() -> None:
    source = (
        Path(__file__)
        .resolve()
        .parents[1]
        .joinpath("tg-manager/bot/handlers/proxy_manager.py")
        .read_text(encoding="utf-8")
    )

    assert '_PROXY_PLAN = "pro"' in source
    assert 'locked_text("Управление прокси", _PROXY_PLAN)' in source
    assert "subscription_locked_markup(_PROXY_PLAN)" in source
    assert 'require_plan(pool, callback.from_user.id, "starter")' not in source

    for handler in (
        "async def cb_proxy_menu",
        "async def cb_proxy_list",
        "async def cb_proxy_add",
        "async def cb_skip_label",
        "async def cb_check_all",
        "async def cb_detect_geo",
        "async def cb_proxy_delete",
        "async def cb_free_pool",
        "async def cb_free_pool_refresh",
    ):
        start = source.index(handler)
        next_route = source.find("@router.callback_query", start + 1)
        body = source[start:] if next_route == -1 else source[start:next_route]
        assert "_require_proxy_manager(callback, pool)" in body


def test_ai_template_generation_requires_starter_plan() -> None:
    source = (
        Path(__file__)
        .resolve()
        .parents[1]
        .joinpath("tg-manager/bot/handlers/templates.py")
        .read_text(encoding="utf-8")
    )

    assert "from bot.utils.subscription import locked_text, require_plan" in source
    assert "subscription_locked_markup" in source
    for handler in (
        "async def cb_template_ai_gen",
        "async def msg_ai_template_prompt",
        "async def cb_template_ai_regen",
    ):
        start = source.index(handler)
        next_route = source.find("@router.", start + 1)
        body = source[start:] if next_route == -1 else source[start:next_route]
        assert 'require_plan(pool, callback.from_user.id, "starter")' in body or (
            handler == "async def msg_ai_template_prompt"
            and 'require_plan(pool, message.from_user.id, "starter")' in body
        )
        assert 'locked_text("AI-генерация шаблонов", "starter")' in body


def test_unknown_features_default_to_enterprise() -> None:
    assert feature_required_plan("new_unclassified_feature") == "enterprise"


def test_plan_levels_are_monotonic() -> None:
    assert PLAN_LEVELS["free"] < PLAN_LEVELS["starter"]
    assert PLAN_LEVELS["starter"] < PLAN_LEVELS["pro"]
    assert PLAN_LEVELS["pro"] < PLAN_LEVELS["enterprise"]


def test_unknown_db_plan_gets_free_limits(monkeypatch) -> None:
    user_id = 490001
    monkeypatch.delenv("ADMIN_IDS", raising=False)
    invalidate_plan_cache(user_id)
    pool = FakePlanPool("vip")

    assert asyncio.run(get_plan(pool, user_id)) == "free"
    assert asyncio.run(get_bot_limit(pool, user_id)) == BOT_LIMITS["free"]
    assert asyncio.run(require_plan(pool, user_id, "starter")) is False


def test_subscription_purchase_flow_does_not_fake_delivery() -> None:
    source = (
        Path(__file__)
        .resolve()
        .parents[1]
        .joinpath("tg-manager/bot/handlers/subscription.py")
        .read_text(encoding="utf-8")
    )

    menu_block = source[
        source.index("async def _build_menu_text_and_kb") : source.index(
            '@router.message(Command("subscription"))'
        )
    ]
    request_block = source[
        source.index("async def cb_request_sub") : source.index(
            '@router.callback_query(SubCb.filter(F.action == "admin_grant"))',
            source.index("async def cb_request_sub"),
        )
    ]

    assert "plan = sub_utils.coerce_plan(plan)" in menu_block
    assert "lim = sub_utils.BOT_LIMITS[plan]" in menu_block
    assert "if plan not in PLAN_PRICES_USD:" in request_block
    assert "if not admin_ids:" in request_block
    assert "subscription request cannot be delivered" in request_block


def test_usdt_payments_get_reference_amount_suffix() -> None:
    source = (
        Path(__file__)
        .resolve()
        .parents[1]
        .joinpath("tg-manager/bot/handlers/subscription.py")
        .read_text(encoding="utf-8")
    )

    assert 0.01 <= _usdt_reference_offset("PAY-ABC123") <= 0.49
    assert _usdt_reference_offset("PAY-ABC123") == _usdt_reference_offset("PAY-ABC123")
    assert _usdt_reference_offset("PAY-ABC123") != _usdt_reference_offset("PAY-XYZ789")
    assert 'if currency == "USDT_TRC20":' in source
    assert "crypto = round(crypto + _usdt_reference_offset(ref), 2)" in source
