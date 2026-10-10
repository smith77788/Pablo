"""Short-lived, owner-scoped approvals for destructive channel metadata changes."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time


class ApprovalError(ValueError):
    pass


def digest_targets(targets: list[dict]) -> str:
    canonical = sorted(
        (int(item["channel_id"]), int(item["acc_id"]), str(item.get("title") or ""))
        for item in targets
    )
    raw = json.dumps(canonical, ensure_ascii=False, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def _secret() -> bytes:
    value = os.getenv("BOT_TOKEN") or os.getenv("MANAGER_BOT_TOKEN") or ""
    if len(value) < 20:
        raise ApprovalError("На сервере не настроен ключ подтверждения")
    return value.encode()


def issue(owner_id: int, value: str, targets: list[dict], *, now: int | None = None) -> str:
    payload = {
        "owner": int(owner_id), "value": hashlib.sha256(value.encode()).hexdigest(),
        "targets": digest_targets(targets), "count": len(targets),
        "expires": int(now or time.time()) + 600,
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    body = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    signature = hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{signature}"


def verify(token: str, owner_id: int, value: str, targets: list[dict], *,
           now: int | None = None) -> str:
    try:
        body, signature = token.split(".", 1)
        expected = hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ApprovalError("Подтверждение недействительно")
        padded = body + "=" * (-len(body) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
    except ApprovalError:
        raise
    except Exception as exc:
        raise ApprovalError("Подтверждение повреждено") from exc
    if int(payload.get("expires") or 0) < int(now or time.time()):
        raise ApprovalError("Подтверждение устарело, проверьте список ещё раз")
    expected_payload = {
        "owner": int(owner_id), "value": hashlib.sha256(value.encode()).hexdigest(),
        "targets": digest_targets(targets), "count": len(targets),
    }
    if any(payload.get(key) != expected for key, expected in expected_payload.items()):
        raise ApprovalError("Список каналов изменился; нужно новое подтверждение")
    return expected_payload["targets"]
