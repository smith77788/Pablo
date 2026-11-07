"""Легитимная жалоба по почте: валидация, сборка письма, честные статусы.

Ключевые инварианты: доказательства — только ССЫЛКИ/ID (сам материал отклоняется),
«отправлено» не выдаётся за «заблокировано», отправка идёт одним письмом.
"""
from __future__ import annotations

import pytest

from services import abuse_report as ar
from tests.test_executors import FakePool


def _ok_data(**over):
    d = {"target": "@baduser", "category": "scam",
         "evidence": ["https://t.me/baduser/123", "https://t.me/baduser/456"],
         "context": "Публикует мошеннические схемы и выманивает предоплату у людей."}
    d.update(over)
    return d


def test_validate_ok():
    clean, errors = ar.validate_report(_ok_data())
    assert errors == []
    assert clean["category"] == "scam" and clean["target"]
    assert len(clean["evidence"]) == 2


def test_validate_requires_fields():
    _, e1 = ar.validate_report({"target": "", "category": "scam",
                                "evidence": ["t.me/x/1"], "context": "x" * 30})
    assert any("цель" in x.lower() for x in e1)
    _, e2 = ar.validate_report(_ok_data(category="nope"))
    assert any("категори" in x.lower() for x in e2)
    _, e3 = ar.validate_report(_ok_data(evidence=[]))
    assert any("ссыл" in x.lower() for x in e3)
    _, e4 = ar.validate_report(_ok_data(context="коротко"))
    assert any("опишите" in x.lower() for x in e4)


def test_validate_rejects_the_material_itself():
    # data:-URI и файл-вложение — это САМ материал, а не ссылка: отклоняем.
    clean, errors = ar.validate_report(_ok_data(
        category="csam",
        evidence=["data:image/jpeg;base64,/9j/4AAQSkZJRg==", "evil.mp4"]))
    assert clean is None
    assert any("сам материал" in x.lower() or "контент" in x.lower() for x in errors)


def test_is_evidence_ref():
    assert ar._is_evidence_ref("https://t.me/x/5") is True
    assert ar._is_evidence_ref("@channel") is True
    assert ar._is_evidence_ref("data:image/png;base64,AAAA") is False
    assert ar._is_evidence_ref("just words no link") is False


def test_recipients_and_escalation():
    assert ar.recipients_for("scam") == ["abuse@telegram.org"]
    assert ar.recipients_for("csam") == ["abuse@telegram.org"]
    esc = ar.escalation_for("csam")
    assert any("NCMEC" in x for x in esc) and any("IWF" in x for x in esc)
    assert ar.escalation_for("scam") == []


def test_compose_email_has_links_not_material():
    clean, _ = ar.validate_report(_ok_data())
    subject, body = ar.compose_email(clean, ref_id="AR-TEST")
    assert "AR-TEST" in subject and clean["target"] in subject
    assert "https://t.me/baduser/123" in body
    assert "not attached" in body.lower()  # явно сказано: материал не приложен


def test_compose_csam_mentions_authorities():
    clean, _ = ar.validate_report(_ok_data(category="csam"))
    _, body = ar.compose_email(clean, ref_id="AR-X")
    assert "NCMEC" in body and "IWF" in body


@pytest.mark.asyncio
async def test_send_invalid_returns_invalid():
    res = await ar.send_report(FakePool(), 1, _ok_data(evidence=[]))
    assert res["status"] == "invalid" and res["errors"]


@pytest.mark.asyncio
async def test_send_no_mailbox():
    res = await ar.send_report(FakePool(fetchrow=None), 42, _ok_data())
    assert res["status"] == "no_mailbox"
    assert "подключ" in res["message"].lower()
    assert "abuse@telegram.org" in res["recipients"]


@pytest.mark.asyncio
async def test_send_sent_is_not_a_block_confirmation():
    mailbox = {"id": 1, "email": "me@example.com", "smtp_host": "smtp", "smtp_port": 587,
               "smtp_pass": "", "auth_type": "oauth", "oauth_provider": "gmail",
               "oauth_refresh_token": "", "oauth_access_token": "", "oauth_expires_at": None}
    calls = {}

    async def fake_send(mb, rcpts, subj, body):
        calls["to"] = rcpts
        return True, ""
    res = await ar.send_report(FakePool(fetchrow=mailbox), 42, _ok_data(), send_fn=fake_send)
    assert res["status"] == "sent"
    assert res["status"] not in ("blocked", "removed", "banned")
    assert "не подтверждение удаления" in res["note"].lower()
    assert calls["to"] == ["abuse@telegram.org"]  # одно письмо, один адрес-деск


@pytest.mark.asyncio
async def test_send_failed_is_honest():
    mailbox = {"id": 1, "email": "me@example.com", "smtp_host": "smtp", "smtp_port": 587,
               "smtp_pass": "", "auth_type": "oauth", "oauth_provider": "gmail",
               "oauth_refresh_token": "", "oauth_access_token": "", "oauth_expires_at": None}

    async def fake_send(mb, rcpts, subj, body):
        return False, "smtp down"
    res = await ar.send_report(FakePool(fetchrow=mailbox), 42, _ok_data(), send_fn=fake_send)
    assert res["status"] == "send_failed" and res["error"] == "smtp down"


def test_migration_registered():
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    assert os.path.exists(os.path.join(root, "schema_v303_abuse_reports.sql"))
    manifest = open(os.path.join(root, "schema_checksums.txt"), encoding="utf-8").read()
    assert "schema_v303_abuse_reports.sql" in manifest
