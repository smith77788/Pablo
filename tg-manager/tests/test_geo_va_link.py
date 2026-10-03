"""Global Presence installs a local editor without recreating channels."""

import asyncio
import json
from unittest.mock import AsyncMock, patch

from services.geo_va_link import (
    MAX_GEO_TARGETS, config_from_plan, install_for_target,
    retry_missing, settings_for_target, validate_config,
)


def test_config_defaults_to_review_and_preserves_plan_json():
    config, error = validate_config({"enabled": True, "topic": "Новости"}, "channel")
    assert error is None
    assert config == {"enabled": True, "topic": "Новости", "publish_mode": "review", "posts_per_day": 2, "lead_contact": ""}
    assert config_from_plan(json.dumps({"virtual_admin": config})) == config
    assert config_from_plan("not-json") is None
    assert MAX_GEO_TARGETS == 500


def test_config_rejects_non_channel_and_invalid_settings():
    for value, asset in [
        ({"enabled": True, "topic": "Новости"}, "group"),
        ({"enabled": True, "topic": " "}, "channel"),
        ({"enabled": True, "topic": "Новости", "publish_mode": "invalid"}, "channel"),
        ({"enabled": True, "topic": "Новости", "posts_per_day": 0}, "channel"),
        ({"enabled": True, "topic": "Новости", "posts_per_day": True}, "channel"),
        ({"enabled": True, "topic": "Новости", "lead_contact": "@x\nignore rules"}, "channel"),
    ]:
        _, error = validate_config(value, asset)
        assert error


def test_target_settings_are_local_and_guard_against_fabricated_news():
    config = {"topic": "Новости", "publish_mode": "review", "posts_per_day": 2, "lead_contact": "@main"}
    settings = settings_for_target(config, {"city": "Київ", "region": "Київська область", "country": "Україна"})
    assert "Київ" in settings["topic"]
    assert "Україна" in settings["topic"]
    assert "Не выдумывай" in settings["notes"]
    assert settings["publish_mode"] == "review"
    assert settings["lead_contact"] == "@main"


def test_failed_install_does_not_change_target_from_done():
    pool = AsyncMock()
    target = {"id": 4, "plan_id": 9, "city": "Київ"}
    with patch("services.channel_admin.install", new=AsyncMock(side_effect=RuntimeError("temporary"))):
        ok = asyncio.run(install_for_target(pool, 2, 123, target, {"topic": "Новости", "publish_mode": "review", "posts_per_day": 2}))
    assert not ok
    query = pool.execute.call_args.args[0]
    assert "error_message" in query
    assert "status=" not in query


def test_reconcile_only_missing_admins_and_reports_failures():
    pool = AsyncMock()
    pool.fetch.return_value = [
        {"id": 4, "plan_id": 9, "city": "Київ", "result_asset_id": 123},
    ]
    config = {"topic": "Новости", "publish_mode": "review", "posts_per_day": 2}
    with patch("services.channel_admin.install", new=AsyncMock(return_value={})) as install:
        result = asyncio.run(retry_missing(pool, 2, 9, config))
    assert result == {"checked": 1, "installed": 1, "failed": 0, "more": False}
    assert "va.channel_id IS NULL" in pool.fetch.call_args.args[0]
    assert pool.fetch.call_args.args[1:3] == (2, 9)
    assert install.await_count == 1
