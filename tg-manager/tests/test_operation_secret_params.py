"""Секреты операций шифруются at-rest без поломки исполнения и дедупа."""
from __future__ import annotations

import json
from pathlib import Path

from services.operation_secrets import (
    FINGERPRINT_FIELD,
    seal_operation_params,
    unseal_operation_params,
)


def test_sensitive_params_are_encrypted_and_roundtrip(monkeypatch):
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", "operation-secret-test-key")
    params = {
        "op": "2fa",
        "account_ids": [10, 11],
        "new_password": "new private password",
        "current_password": "old private password",
        "nested": {"bot_token": "1234567890:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"},
    }

    stored, fingerprint = seal_operation_params(params)
    serialized = json.dumps(stored)

    assert fingerprint and stored[FINGERPRINT_FIELD] == fingerprint
    assert "new private password" not in serialized
    assert "old private password" not in serialized
    assert "AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw" not in serialized
    assert stored["new_password"].startswith("ENC:")
    assert unseal_operation_params(stored) == params


def test_sensitive_fingerprint_is_stable_but_ciphertext_is_not(monkeypatch):
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", "operation-secret-test-key")
    params = {"op": "2fa", "new_password": "same password"}

    first, first_fp = seal_operation_params(params)
    second, second_fp = seal_operation_params(params)

    assert first_fp == second_fp
    assert first["new_password"] != second["new_password"]


def test_plaintext_password_may_start_with_encryption_marker(monkeypatch):
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", "operation-secret-test-key")
    params = {"new_password": "ENC:this-is-a-real-user-password"}

    stored, _ = seal_operation_params(params)

    assert stored["new_password"] != params["new_password"]
    assert unseal_operation_params(stored) == params


def test_non_sensitive_params_keep_legacy_shape():
    params = {"channel": "@example", "account_ids": [1, 2]}

    stored, fingerprint = seal_operation_params(params)

    assert stored == params
    assert fingerprint is None
    assert FINGERPRINT_FIELD not in stored


def test_worker_unseals_params_before_executor_dispatch():
    source = (Path(__file__).resolve().parents[1] / "services" / "op_worker.py").read_text(
        encoding="utf-8"
    )
    run_task = source[source.index("async def _run_op_task"):]
    parse_at = run_task.index('row["params"]')
    unseal_at = run_task.index("params = unseal_operation_params(params)")
    dispatch_at = run_task.index("_handler = handler_for(op_type)")
    assert parse_at < unseal_at < dispatch_at
