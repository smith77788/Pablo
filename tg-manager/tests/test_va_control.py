"""Центр решений администратора связывает модули и объясняет выбор."""

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from services import channel_admin as ca
from services import infra_memory, operation_bus, va_control


def test_plan_audit_finds_duplicate_slots_topics_and_sales_overflow():
    now = datetime.now(UTC)
    plan = [
        {"slot_at": now.isoformat(), "topic": "Одна тема", "pillar": "Продажа"},
        {"slot_at": now.isoformat(), "topic": " одна тема ", "pillar": "Акции"},
    ]
    issues = va_control.audit_plan({"business": {"sales_share": 10}}, plan, [], now)
    assert any("одно время" in issue for issue in issues)
    assert any("повторяются темы" in issue for issue in issues)
    assert any("лимит продающих" in issue for issue in issues)


def test_control_cards_explain_knowledge_sources_accounts_and_learning():
    profile = {"title": "Свежие новости", "topic": "новости", "business": {"goal": "leads"}}
    strategy = {"enabled": False, "business": {}}
    result = va_control.build(
        profile, strategy, [], [], [], [], [], None, [], [], now=datetime.now(UTC),
    )
    by_key = {card["key"]: card for card in result["cards"]}
    assert by_key["knowledge"]["level"] == "warning"
    assert by_key["sources"]["level"] == "warning"
    assert by_key["accounts"]["level"] == "warning"
    assert "минимум две" in by_key["learning"]["detail"]
    assert result["summary"]["total"] == len(result["cards"])
    assert result["summary"]["warnings"] == sum(
        card["level"] == "warning" for card in result["cards"]
    )
    assert 0 <= result["summary"]["score"] <= 100


def test_knowledge_marks_local_and_network_sources_separately():
    profile = {"business": {"facts": "местный факт", "network_role": "discovery"}}
    strategy = {"enabled": True, "destination": "@main", "action": "подписаться",
                "cta_share": 20, "business": {"faq": "общий ответ"}}
    rows = {row["key"]: row for row in va_control.knowledge(profile, strategy)}
    assert rows["facts"]["source"] == "канал"
    assert rows["faq"]["source"] == "общая стратегия"


@pytest.mark.asyncio
async def test_network_actions_do_not_demand_calendar_plan_from_newsrooms():
    pool = AsyncMock()
    pool.fetch.return_value = [
        {"channel_id": 8, "title": "Свежие новости", "topic": "", "project_info": "",
         "brief": {}, "last_error": None, "enabled": True, "review": False,
         "empty_plan": True},
        {"channel_id": 9, "title": "Магазин", "topic": "товары", "project_info": "",
         "brief": {}, "last_error": None, "enabled": True, "review": False,
         "empty_plan": True},
    ]

    actions = await va_control.network_actions(pool, 7)

    assert [item["channel_id"] for item in actions] == ["9"]
    assert actions[0]["reason"] == "Нет будущего плана"


@pytest.mark.asyncio
async def test_eligible_accounts_excludes_shared_health_quarantine(monkeypatch):
    pool = AsyncMock()
    pool.fetch.return_value = [{"id": 11}, {"id": 12}]
    monkeypatch.setattr(infra_memory, "is_account_quarantined", AsyncMock(side_effect=[False, True]))
    assert await va_control.eligible_accounts(pool, 7, 8) == [11]
    sql, owner, channel = pool.fetch.call_args.args
    assert "a.owner_id=mc.owner_id" in sql and "a.is_active=TRUE" in sql
    assert (owner, channel) == (7, 8)


@pytest.mark.asyncio
async def test_publish_uses_only_accounts_approved_by_shared_health(monkeypatch):
    pool = AsyncMock()
    allowed = AsyncMock(return_value=[11])
    submit = AsyncMock(return_value=44)
    monkeypatch.setattr(va_control, "eligible_accounts", allowed)
    monkeypatch.setattr(ca, "channel_row", AsyncMock(return_value={"title": "Канал"}))
    monkeypatch.setattr(operation_bus, "submit", submit)

    assert await ca.publish(pool, 7, 8, "текст", "Польза") == 44

    allowed.assert_awaited_once_with(pool, 7, 8)
    payload = submit.await_args.args[3]
    assert payload["account_ids"] == [11]
    assert payload["channel_ids"] == [8]


@pytest.mark.asyncio
async def test_publish_stops_before_queue_when_no_healthy_account(monkeypatch):
    pool = AsyncMock()
    monkeypatch.setattr(va_control, "eligible_accounts", AsyncMock(return_value=[]))
    submit = AsyncMock()
    monkeypatch.setattr(operation_bus, "submit", submit)

    with pytest.raises(ca.ChannelAdminError, match="Нет доступного аккаунта"):
        await ca.publish(pool, 7, 8, "текст", "Польза")
    submit.assert_not_awaited()


@pytest.mark.asyncio
async def test_network_actions_prioritize_errors_then_owner_review():
    pool = AsyncMock()
    pool.fetch.return_value = [
        {"channel_id": 1, "title": "Первый", "last_error": "нет прав", "review": False,
         "enabled": True, "empty_plan": False},
        {"channel_id": 2, "title": "Второй", "last_error": None, "review": True,
         "enabled": True, "empty_plan": False},
        {"channel_id": 3, "title": "Третий", "last_error": None, "review": False,
         "enabled": True, "empty_plan": True},
    ]
    actions = await va_control.network_actions(pool, 77)
    assert [a["reason"] for a in actions] == [
        "Ошибка работы: нет прав", "Черновики ждут решения", "Нет будущего плана",
    ]
    assert pool.fetch.call_args.args[1:] == (77,)


def test_interface_connects_control_cards_network_actions_and_safe_rollback():
    root = Path(__file__).resolve().parents[1]
    js = (root / "mini_app/screens/va_admin.js").read_text(encoding="utf-8")
    api = (root / "services/mini_app_api.py").read_text(encoding="utf-8")
    network = js[js.index("function _vaNetworkHtml"):js.index("let _vaDraftCtx")]
    control = js[js.index("function _vaControlHtml"):js.index("function _vaBriefHtml")]

    assert "n.next_actions" in network
    assert "n.next_actions" not in control
    assert "openHealth()" in control and "openOps()" in control
    assert "openEditorialRules(_vaCid)" in control and "openVaStrategy()" in control
    assert "vaRollbackLearning" in control
    assert "control.summary" in control and "Проверки без предупреждений" in control
    assert "/learning/rollback" in js and "/learning/rollback" in api
