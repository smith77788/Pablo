"""Mass channel renames require an exact, short-lived approved target set."""
from pathlib import Path

import pytest
from services import channel_change_approval as approval

TARGETS = [
    {"channel_id": 2, "acc_id": 8, "title": "Two"},
    {"channel_id": 1, "acc_id": 7, "title": "One"},
]


def test_approval_is_order_independent_and_exact(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "123456:" + "x" * 40)
    token = approval.issue(9, "New", TARGETS, now=100)
    digest = approval.verify(token, 9, "New", list(reversed(TARGETS)), now=101)
    assert digest == approval.digest_targets(TARGETS)


@pytest.mark.parametrize("owner,value,targets", [
    (10, "New", TARGETS),
    (9, "Other", TARGETS),
    (9, "New", TARGETS[:1]),
    (9, "New", [{**TARGETS[0], "title": "Changed"}, TARGETS[1]]),
])
def test_approval_rejects_changed_scope(monkeypatch, owner, value, targets):
    monkeypatch.setenv("BOT_TOKEN", "123456:" + "x" * 40)
    token = approval.issue(9, "New", TARGETS, now=100)
    with pytest.raises(approval.ApprovalError):
        approval.verify(token, owner, value, targets, now=101)


def test_approval_expires_and_missing_secret_fails_closed(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "123456:" + "x" * 40)
    token = approval.issue(9, "New", TARGETS, now=100)
    with pytest.raises(approval.ApprovalError, match="устарело"):
        approval.verify(token, 9, "New", TARGETS, now=701)
    monkeypatch.delenv("BOT_TOKEN")
    monkeypatch.delenv("MANAGER_BOT_TOKEN", raising=False)
    with pytest.raises(approval.ApprovalError, match="ключ"):
        approval.issue(9, "New", TARGETS, now=100)


def test_signed_token_reaches_worker_instead_of_forgeable_digest():
    root = Path(__file__).resolve().parents[1]
    api = (root / "services" / "mini_app_api.py").read_text(encoding="utf-8")
    worker = (root / "services" / "op_worker.py").read_text(encoding="utf-8")

    assert '"approval_token": approval_token' in api
    assert 'params.get("approval_token")' in worker
    assert "channel_change_approval.verify(" in worker
    assert "approval_digest" not in api
    assert "approval_digest" not in worker
