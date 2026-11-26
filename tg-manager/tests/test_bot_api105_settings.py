"""Bot API 10.5 patches preserve explicit changes and isolate their owner."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from services import bot_api, bot_settings, mini_app_api, mini_app_bot_settings

BOT_ID = 12345
OWNER_ID = 42
SECRET = "DO_NOT_DISCLOSE_TOKEN"


def _getme(**extra):
    return {"ok": True, "result": {"id": BOT_ID, "is_bot": True, "first_name": "Тест",
                                   "username": "test_bot", **extra}}


def test_strict_patch_keeps_false_zero_and_empty_string():
    payload = {"can_join_groups": False, "inline_feedback_share_denominator": 0,
               "inline_query_placeholder": "", "callback_query_mode": "auto"}
    assert bot_settings.validate_patch(payload) == payload
    assert len(bot_settings.FIELDS) == 15
    assert set(bot_settings.validate_patch({"has_topic_sidebar": True})) == {"has_topic_sidebar"}


@pytest.mark.parametrize("raw", [None, [], {}, {"token": "secret"}, {"can_join_groups": "false"},
    {"can_join_groups": 0}, {"can_join_groups": None}, {"callback_query_mode": "message"},
    {"inline_feedback_share_denominator": True}, {"inline_feedback_share_denominator": 2},
    {"inline_query_placeholder": 1}, {"inline_query_placeholder": "x" * 1025}])
def test_invalid_settings_are_rejected(raw):
    with pytest.raises(ValueError):
        bot_settings.validate_patch(raw)


def test_public_readback_does_not_invent_missing_values_or_expose_secrets():
    info = _getme(active_usernames=["alias_bot", "bad/name", 12], token=SECRET,
                  can_join_groups=False, callback_query_mode="inline")["result"]
    result = bot_settings.public_info(info)
    assert result["settings"]["can_join_groups"] is False
    assert result["settings"]["has_topic_sidebar"] is None
    assert result["settings"]["callback_query_mode"] is None
    assert result["bot"]["active_usernames"] == ["alias_bot"]
    assert result["invite_link"] == "https://t.me/botinvite/test_bot"
    assert SECRET not in json.dumps(result)
    assert bot_settings.public_info({"id": BOT_ID, "username": "bad/name"})["invite_link"] is None


def test_mutation_checks_live_bot_identity_and_sends_only_patch(monkeypatch):
    call = AsyncMock(side_effect=[_getme(), {"ok": True, "result": True}])
    monkeypatch.setattr(bot_api, "_call", call)
    patch = {"can_join_groups": False, "inline_query_placeholder": ""}
    result = asyncio.run(bot_settings.update_settings(None, SECRET, BOT_ID, patch))
    assert result == {"ok": True, "applied": patch}
    assert call.await_args_list[0].args == (None, SECRET, "getMe")
    assert call.await_args_list[1].args == (None, SECRET, "setMySettings")
    assert call.await_args_list[1].kwargs == patch


def test_mismatching_live_identity_prevents_mutation(monkeypatch):
    call = AsyncMock(return_value=_getme(id=999))
    monkeypatch.setattr(bot_api, "_call", call)
    with pytest.raises(bot_settings.BotSettingsError, match="другому боту"):
        asyncio.run(bot_settings.update_settings(None, SECRET, BOT_ID, {"can_join_groups": False}))
    assert call.await_count == 1


@pytest.mark.parametrize("reply,status", [
    ({"ok": False, "error_code": 401, "description": SECRET}, 409),
    ({"ok": False, "error_code": 429, "parameters": {"retry_after": 90}}, 429),
    ({"ok": False, "error_code": 500, "description": SECRET}, 503),
    ({"ok": True, "result": False}, 502),
])
def test_transport_error_is_public_and_not_false_success(monkeypatch, reply, status):
    call = AsyncMock(side_effect=[_getme(), reply])
    monkeypatch.setattr(bot_api, "_call", call)
    with pytest.raises(bot_settings.BotSettingsError) as error:
        asyncio.run(bot_settings.update_settings(None, SECRET, BOT_ID, {"has_topics_enabled": True}))
    assert error.value.status == status
    assert SECRET not in str(error.value)


def _route(pool, monkeypatch, method="GET", uid=OWNER_ID):
    monkeypatch.setattr(mini_app_api, "_get_uid", lambda request: uid)
    if not hasattr(pool, "fetchval"):
        pool.fetchval = AsyncMock(return_value=None)
    app = web.Application()
    mini_app_bot_settings.setup_routes(app, pool)
    return next(r.handler for r in app.router.routes() if r.method == method)


def _request(body=None, bot_id=BOT_ID):
    return SimpleNamespace(match_info={"bot_id": str(bot_id)}, json=AsyncMock(return_value=body))


def test_route_auth_refuses_before_db_or_telegram(monkeypatch):
    pool = SimpleNamespace(fetchrow=AsyncMock())
    call = AsyncMock()
    monkeypatch.setattr(bot_api, "_call", call)
    response = asyncio.run(_route(pool, monkeypatch, uid=None)(_request()))
    assert response.status == 401
    pool.fetchrow.assert_not_awaited()
    call.assert_not_awaited()


def test_foreign_bot_is_not_found_and_never_contacts_telegram(monkeypatch):
    pool = SimpleNamespace(fetchrow=AsyncMock(return_value=None))
    call = AsyncMock()
    monkeypatch.setattr(bot_api, "_call", call)
    response = asyncio.run(_route(pool, monkeypatch, "POST")(_request({"settings": {"can_join_groups": False}})))
    assert response.status == 404
    sql, bot_id, uid = pool.fetchrow.await_args.args
    assert "added_by=$2" in sql and "deleted_at IS NULL" in sql and "is_active" in sql
    assert (bot_id, uid) == (BOT_ID, OWNER_ID)
    call.assert_not_awaited()


def test_owner_route_returns_fullinfo_without_credentials(monkeypatch):
    pool = SimpleNamespace(fetchrow=AsyncMock(return_value={"bot_id": BOT_ID, "token": SECRET}))
    monkeypatch.setattr(bot_api, "_call", AsyncMock(return_value=_getme(has_topic_sidebar=True)))
    response = asyncio.run(_route(pool, monkeypatch)(_request()))
    assert response.status == 200
    payload = json.loads(response.text)
    assert payload["settings"]["has_topic_sidebar"] is True
    assert SECRET not in response.text


def test_post_unknown_fields_never_call_telegram(monkeypatch):
    pool = SimpleNamespace(fetchrow=AsyncMock(return_value={"bot_id": BOT_ID, "token": SECRET}))
    call = AsyncMock()
    monkeypatch.setattr(bot_api, "_call", call)
    response = asyncio.run(_route(pool, monkeypatch, "POST")(_request({"settings": {"token": SECRET}})))
    assert response.status == 400 and SECRET not in response.text
    call.assert_not_awaited()


def test_post_rate_limit_preserves_retry_and_does_not_claim_success(monkeypatch):
    pool = SimpleNamespace(fetchrow=AsyncMock(return_value={"bot_id": BOT_ID, "token": SECRET}))
    call = AsyncMock(side_effect=[_getme(), {"ok": False, "error_code": 429, "parameters": {"retry_after": 90}}])
    monkeypatch.setattr(bot_api, "_call", call)
    response = asyncio.run(_route(pool, monkeypatch, "POST")(_request({"settings": {"can_join_groups": False}})))
    assert response.status == 429
    assert response.headers["Retry-After"] == "90"
    assert json.loads(response.text)["retry_after"] == 90
    assert "ok" not in json.loads(response.text)


def test_unavailable_database_is_an_error_not_empty_settings(monkeypatch):
    pool = SimpleNamespace(fetchrow=AsyncMock(side_effect=RuntimeError(SECRET)))
    response = asyncio.run(_route(pool, monkeypatch)(_request()))
    assert response.status == 503 and SECRET not in response.text


@pytest.mark.parametrize("protected_kind", ["manager", "mirror"])
def test_manager_and_mirror_keep_message_context(monkeypatch, protected_kind):
    pool = SimpleNamespace(fetchrow=AsyncMock(return_value={"bot_id": BOT_ID, "token": SECRET}),
                           fetchval=AsyncMock(return_value=1 if protected_kind == "mirror" else None))
    monkeypatch.setattr(mini_app_api, "_bot_token", lambda: f"{BOT_ID if protected_kind == 'manager' else 999}:fixture")
    call = AsyncMock(return_value=_getme())
    monkeypatch.setattr(bot_api, "_call", call)
    response = asyncio.run(_route(pool, monkeypatch, "POST")(_request({"settings": {"callback_query_mode": "inline"}})))
    assert response.status == 400
    call.assert_not_awaited()
    response = asyncio.run(_route(pool, monkeypatch)(_request()))
    field = next(f for f in json.loads(response.text)["fields"] if f["key"] == "callback_query_mode")
    assert field["choices"] == ["default"]
    if protected_kind == "mirror":
        assert pool.fetchval.await_args.args[1:] == (BOT_ID, OWNER_ID)


def test_regular_owned_bot_can_opt_into_inline_callbacks(monkeypatch):
    pool = SimpleNamespace(fetchrow=AsyncMock(return_value={"bot_id": BOT_ID, "token": SECRET}),
                           fetchval=AsyncMock(return_value=None))
    monkeypatch.setattr(mini_app_api, "_bot_token", lambda: "999:fixture")
    call = AsyncMock(side_effect=[_getme(), {"ok": True, "result": True}])
    monkeypatch.setattr(bot_api, "_call", call)
    response = asyncio.run(_route(pool, monkeypatch, "POST")(_request({"settings": {"callback_query_mode": "inline"}})))
    assert response.status == 200
    assert call.await_args.kwargs == {"callback_query_mode": "inline"}
