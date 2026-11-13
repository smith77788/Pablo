"""Единый модуль Жалоб: одна категория → оба вектора, мультиотправка писем.

Владелец просил единый модуль: выбрать один/несколько аккаунтов флота и
один/несколько почтовых ящиков, по одной категории уходит и in-app жалоба, и
письмо(а) в компетентный орган с автоподстановкой текста. Здесь — backend-
инварианты этого модуля (frontend-проводка проверяется статически отдельно).
"""
from __future__ import annotations

import pytest

from services import abuse_report as ar
from tests.test_executors import FakePool

_MAILBOX = {"id": 1, "email": "me@example.com", "smtp_host": "smtp", "smtp_port": 587,
            "smtp_pass": "", "auth_type": "oauth", "oauth_provider": "gmail",
            "oauth_refresh_token": "", "oauth_access_token": "", "oauth_expires_at": None}


def _ok_data(**over):
    d = {"target": "@baduser", "category": "scam",
         "evidence": ["https://t.me/baduser/123"],
         "context": "Публикует мошеннические схемы и выманивает предоплату у людей."}
    d.update(over)
    return d


# ── Единый список категорий ведёт оба вектора ──────────────────────────────

def test_every_category_maps_to_an_inapp_reason():
    """Каждая категория даёт повод MTProto-жалобы флотом из закрытого списка."""
    from services.reporter_engine import REPORT_REASONS
    for cat in ar.CATEGORIES:
        reason = ar.tg_reason_for(cat)
        assert reason in REPORT_REASONS, (cat, reason)


def test_csam_uses_the_child_reason_not_generic():
    """CSAM обязан подаваться по профильному поводу, а не 'other'/'spam'."""
    assert ar.tg_reason_for("csam") == "childabuse"
    from services.reporter_engine import _get_reason
    # повод резолвится в реальный (под заглушкой — непустой) InputReportReason
    assert _get_reason("childabuse") is not None


def test_tg_reason_falls_back_for_unknown_category():
    assert ar.tg_reason_for("no-such-category") == "other"


def test_categories_public_exposes_tg_reason():
    pub = ar.categories_public()
    assert pub and all("tg_reason" in c and "key" in c and "label" in c for c in pub)


# ── Мультиотправка писем: по одному письму с каждого выбранного ящика ───────

@pytest.mark.asyncio
async def test_multi_send_sends_from_each_selected_mailbox():
    calls = []

    async def fake_send(mb, rcpts, subj, body):
        calls.append(rcpts)
        return True, ""

    res = await ar.send_report_multi(
        FakePool(fetchrow=_MAILBOX), 42, _ok_data(), [1, 2, 3], send_fn=fake_send)
    assert res["status"] == "sent"
    assert res["sent"] == 3 and res["failed"] == 0
    assert len(calls) == 3  # ровно по письму на каждый ящик
    assert len(res["results"]) == 3


@pytest.mark.asyncio
async def test_multi_send_dedups_mailbox_ids():
    async def fake_send(mb, rcpts, subj, body):
        return True, ""
    res = await ar.send_report_multi(
        FakePool(fetchrow=_MAILBOX), 42, _ok_data(), [1, 1, 1], send_fn=fake_send)
    assert res["requested"] == 1 and res["sent"] == 1


@pytest.mark.asyncio
async def test_multi_send_is_honest_on_invalid_data_and_sends_nothing():
    calls = []

    async def fake_send(mb, rcpts, subj, body):
        calls.append(1)
        return True, ""
    # нет доказательной ссылки → письмо не собрать: не шлём НИ ОДНОГО
    res = await ar.send_report_multi(
        FakePool(fetchrow=_MAILBOX), 42, _ok_data(evidence=[]), [1, 2],
        send_fn=fake_send)
    assert res["status"] == "invalid" and res["sent"] == 0
    assert calls == []


@pytest.mark.asyncio
async def test_multi_send_reports_no_mailbox_without_false_success():
    async def fake_send(mb, rcpts, subj, body):
        return True, ""
    # ящик не найден (fetchrow=None) → no_mailbox, а не 'sent'
    res = await ar.send_report_multi(
        FakePool(fetchrow=None), 42, _ok_data(), [7], send_fn=fake_send)
    assert res["sent"] == 0 and res["no_mailbox"] == 1
    assert res["status"] != "sent"


@pytest.mark.asyncio
async def test_multi_send_requires_at_least_one_mailbox():
    res = await ar.send_report_multi(FakePool(fetchrow=_MAILBOX), 42, _ok_data(), [])
    assert res["status"] == "invalid" and res["requested"] == 0


# ── «1 аккаунт вместо среза» объясняется, а не молчит ──────────────────────

def test_mass_report_explains_why_fewer_accounts_ran():
    """Жалоба владельца: операция взяла 1 аккаунт из выбранного среза без
    объяснения. Теперь исход РАСКЛАДЫВАЕТ остаток по причинам (кулдаун/спамблок/
    риск-пульс/занятость), а не оставляет владельца гадать."""
    import inspect
    from services import op_worker
    src = inspect.getsource(op_worker._exec_mass_report)
    # явный выбор владельца (account_ids) против реально отработавших (used_ids)
    assert "used_ids = {int(a[\"id\"]) for a in accounts}" in src
    # раскладываем, почему из выбранного пошло меньше
    assert "_diagnose_unused_accounts(" in src
    assert '"unused_reasons"' in src
