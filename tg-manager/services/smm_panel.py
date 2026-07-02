"""SMM panel API client — supports GlobalSMM API v2 format (standard SMM panel API).

API format: POST with form data: key, action, ...
Responses: JSON {order: id} or {status: ...} or {balance: ...}
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp

log = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = aiohttp.ClientTimeout(total=30)


class SmmPanelClient:
    def __init__(self, api_url: str, api_key: str) -> None:
        self.api_url = api_url.rstrip("/")
        self.api_key = api_key

    async def _post(self, session: aiohttp.ClientSession, data: dict) -> dict:
        data["key"] = self.api_key
        try:
            async with session.post(
                self.api_url, data=data, timeout=_DEFAULT_TIMEOUT, ssl=False
            ) as resp:
                resp.raise_for_status()
                text = await resp.text()
                try:
                    return await resp.json(content_type=None)
                except Exception:
                    return {"_raw": text, "error": "invalid json"}
        except asyncio.TimeoutError:
            return {"error": "timeout"}
        except Exception as exc:
            return {"error": str(exc)[:200]}

    async def get_balance(self) -> dict[str, Any]:
        async with aiohttp.ClientSession() as s:
            return await self._post(s, {"action": "balance"})

    async def get_services(self) -> list[dict]:
        async with aiohttp.ClientSession() as s:
            result = await self._post(s, {"action": "services"})
            if isinstance(result, list):
                return result
            return []

    async def add_order(
        self,
        service_id: str,
        link: str,
        quantity: int,
    ) -> dict[str, Any]:
        """Place a new order. Returns dict with 'order' key on success."""
        async with aiohttp.ClientSession() as s:
            return await self._post(
                s,
                {
                    "action": "add",
                    "service": service_id,
                    "link": link,
                    "quantity": quantity,
                },
            )

    async def get_order_status(self, order_id: str) -> dict[str, Any]:
        """Check status of a single order."""
        async with aiohttp.ClientSession() as s:
            return await self._post(s, {"action": "status", "order": order_id})

    async def get_multiple_statuses(self, order_ids: list[str]) -> dict[str, Any]:
        """Check status of multiple orders at once (comma-separated)."""
        async with aiohttp.ClientSession() as s:
            return await self._post(
                s,
                {"action": "status", "orders": ",".join(order_ids)},
            )


def make_client(api_url: str, api_key_enc: str) -> SmmPanelClient:
    """Create client from an at-rest-encrypted API key.

    decrypt_token прозрачно возвращает plaintext для старых (незашифрованных)
    записей, поэтому обратная совместимость сохраняется без миграции.
    """
    from services.token_vault import decrypt_token

    return SmmPanelClient(api_url, decrypt_token(api_key_enc))


# ── Status normalization ───────────────────────────────────────────────────────

_STATUS_MAP = {
    "Pending": "в очереди",
    "In progress": "выполняется",
    "Partial": "частично",
    "Completed": "выполнен",
    "Cancelled": "отменён",
    "Processing": "обработка",
    "Active": "активен",
}


def normalize_status(raw: str) -> str:
    return _STATUS_MAP.get(raw, raw)
