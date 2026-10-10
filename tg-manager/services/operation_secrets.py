"""Шифрование чувствительных параметров фоновых операций.

`operation_queue.params` живёт дольше HTTP-запроса и попадает в резервные копии,
поэтому пароли и токены не должны храниться там открытым текстом. Этот модуль
оставляет форму params прежней для исполнителей, но шифрует секреты перед INSERT
и раскрывает их только в памяти worker непосредственно перед выполнением.
"""
from __future__ import annotations

import json
from typing import Any

from services.secret_masking import SENSITIVE_FIELD_NAMES
from services.token_vault import (
    decrypt_token,
    encrypt_plaintext_token,
    keyed_fingerprint,
)

FINGERPRINT_FIELD = "_sealed_params_fp"


def _transform(value: Any, *, decrypt: bool) -> tuple[Any, bool]:
    if isinstance(value, dict):
        changed = False
        result = {}
        for key, item in value.items():
            if key == FINGERPRINT_FIELD:
                changed = True
                continue
            if isinstance(key, str) and key.lower() in SENSITIVE_FIELD_NAMES:
                if isinstance(item, str):
                    result[key] = (
                        decrypt_token(item) if decrypt else encrypt_plaintext_token(item)
                    )
                    changed = True
                else:
                    result[key] = item
                continue
            result[key], nested = _transform(item, decrypt=decrypt)
            changed = changed or nested
        return result, changed
    if isinstance(value, list):
        result = []
        changed = False
        for item in value:
            transformed, nested = _transform(item, decrypt=decrypt)
            result.append(transformed)
            changed = changed or nested
        return result, changed
    return value, False


def unseal_operation_params(params: dict[str, Any]) -> dict[str, Any]:
    """Вернуть plaintext-копию params и убрать внутреннюю метку дедупа."""
    plain, _ = _transform(params, decrypt=True)
    return plain


def seal_operation_params(
    params: dict[str, Any],
) -> tuple[dict[str, Any], str | None]:
    """Подготовить params для JSONB и вернуть HMAC для безопасного дедупа."""
    plain = unseal_operation_params(params)
    sealed, has_sensitive_fields = _transform(plain, decrypt=False)
    if not has_sensitive_fields:
        return sealed, None

    canonical = json.dumps(
        plain, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    fingerprint = keyed_fingerprint(canonical, namespace="operation-params-v1")
    sealed[FINGERPRINT_FIELD] = fingerprint
    return sealed, fingerprint
