"""Virtual Administrator control center explains decisions without changing state."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from services import infra_memory, va_control


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
