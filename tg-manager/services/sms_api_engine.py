"""SMS API Engine — виртуальные номера для авторегистрации Telegram аккаунтов.

Поддерживаемые сервисы:
  - 5sim.net    (api_key в platform_settings["sms_api_5sim_key"])
  - sms-activate.org (api_key в platform_settings["sms_api_smsa_key"])

Выбор сервиса — platform_settings["sms_api_service"] = "5sim" | "smsactivate"
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp

log = logging.getLogger(__name__)

_TIMEOUT = aiohttp.ClientTimeout(total=30)

# Коды продукта (Telegram) для каждого сервиса
_5SIM_PRODUCT = "telegram"
_SMSA_SERVICE = "tg"

# Страна номера: мини-апп шлёт ISO-код («RU»), а сервисы кодируют страну
# по-своему — 5sim словом («russia»), sms-activate числом («0»). Без перевода
# заказ «RU» уходил как есть, и каждый пакет из мини-аппа падал на первой
# покупке номера («нет такой страны»): из бота же код приходил уже в формате
# сервиса (список стран берётся у самого сервиса) и проходит насквозь.
_ISO_TO_5SIM = {
    "RU": "russia", "UA": "ukraine", "KZ": "kazakhstan", "US": "usa",
    "GB": "england", "DE": "germany", "PL": "poland", "IN": "india",
    "ID": "indonesia", "BR": "brazil", "PH": "philippines",
}
_ISO_TO_SMSA = {
    "RU": "0", "UA": "1", "KZ": "2", "US": "187", "GB": "16", "DE": "43",
    "PL": "15", "IN": "22", "ID": "6", "BR": "73", "PH": "4",
}


def service_country(service: str, country: str | None) -> str:
    """Страна в формате сервиса. Уже «родной» код (russia / 0) не трогаем."""
    raw = str(country or "").strip()
    table = _ISO_TO_5SIM if service == "5sim" else _ISO_TO_SMSA
    return table.get(raw.upper(), raw) if len(raw) == 2 and raw.isalpha() else raw


# ── 5sim.net ──────────────────────────────────────────────────────────────────


class FiveSimClient:
    BASE = "https://5sim.net/v1"

    def __init__(self, api_key: str) -> None:
        self._key = api_key

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._key}",
            "Accept": "application/json",
        }

    async def get_countries(self) -> list[dict]:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as s:
            async with s.get(f"{self.BASE}/guest/countries", headers=self._headers()) as r:
                try:
                    data = await r.json(content_type=None)
                except Exception:
                    data = {}
                return [{"code": k, "name": k.replace("-", " ").title()} for k in data.keys()]

    async def buy_number(self, country: str) -> dict:
        """Заказать номер. Возвращает {"id": str, "phone": str}."""
        country = service_country("5sim", country)
        url = f"{self.BASE}/user/buy/activation/{country}/any/{_5SIM_PRODUCT}"
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as s:
            async with s.get(url, headers=self._headers()) as r:
                if r.status != 200:
                    text = await r.text()
                    raise RuntimeError(f"5sim buy_number: {r.status} {text[:200]}")
                data = await r.json()
                return {"id": str(data["id"]), "phone": f"+{data['phone']}"}

    async def get_sms(self, order_id: str, timeout_sec: int = 120) -> str | None:
        """Ждёт SMS с кодом. Возвращает код или None если таймаут."""
        url = f"{self.BASE}/user/check/{order_id}"
        deadline = asyncio.get_running_loop().time() + timeout_sec
        while asyncio.get_running_loop().time() < deadline:
            async with aiohttp.ClientSession(timeout=_TIMEOUT) as s:
                async with s.get(url, headers=self._headers()) as r:
                    if r.status != 200:
                        await asyncio.sleep(5)
                        continue
                    data = await r.json()
                    status = data.get("status", "")
                    if status in ("RECEIVED", "FINISHED"):
                        sms_list = data.get("sms", [])
                        if sms_list:
                            text = sms_list[-1].get("text", "")
                            import re
                            m = re.search(r"\d{5,6}", text)
                            if m:
                                return m.group(0)
                    if status in ("CANCELED", "TIMEOUT", "BANNED"):
                        return None
            await asyncio.sleep(6)
        return None

    async def _order_action(self, action: str, order_id: str) -> None:
        url = f"{self.BASE}/user/{action}/{order_id}"
        try:
            async with aiohttp.ClientSession(timeout=_TIMEOUT) as s:
                await s.get(url, headers=self._headers())
        except Exception as exc:
            log.warning("5sim %s %s: %s", action, order_id, exc)

    async def cancel_order(self, order_id: str) -> None:
        await self._order_action("cancel", order_id)

    async def finish_order(self, order_id: str) -> None:
        """Номер отработал — закрыть заказ, чтобы сервис не держал его открытым."""
        await self._order_action("finish", order_id)

    async def report_banned(self, order_id: str) -> None:
        """Номер заблокирован в Telegram: «ban» у 5sim возвращает деньги за
        него и убирает номер из выдачи, а «cancel» — нет."""
        await self._order_action("ban", order_id)

    async def get_balance(self) -> float:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as s:
            async with s.get(f"{self.BASE}/user/profile", headers=self._headers()) as r:
                data = await r.json()
                return float(data.get("balance", 0.0))


# ── sms-activate.org ─────────────────────────────────────────────────────────


class SmsActivateClient:
    BASE = "https://api.sms-activate.org/stubs/handler_api.php"

    def __init__(self, api_key: str) -> None:
        self._key = api_key

    async def _get(self, params: dict) -> str:
        params["api_key"] = self._key
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as s:
            async with s.get(self.BASE, params=params) as r:
                return await r.text()

    async def get_countries(self) -> list[dict]:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as s:
            async with s.get(
                "https://api.sms-activate.org/stubs/handler_api.php",
                params={"api_key": self._key, "action": "getCountries"},
            ) as r:
                data = await r.json(content_type=None)
                return [
                    {"code": str(v["id"]), "name": v.get("rus", v.get("eng", str(v["id"])))}
                    for v in data.values()
                    if isinstance(v, dict)
                ]

    async def buy_number(self, country: str) -> dict:
        country = service_country("smsactivate", country)
        text = await self._get({"action": "getNumber", "service": _SMSA_SERVICE, "country": country})
        if not text.startswith("ACCESS_NUMBER"):
            raise RuntimeError(f"sms-activate buy_number: {text[:200]}")
        _, order_id, phone = text.strip().split(":")
        return {"id": order_id, "phone": f"+{phone}"}

    async def get_sms(self, order_id: str, timeout_sec: int = 120) -> str | None:
        import re
        deadline = asyncio.get_running_loop().time() + timeout_sec
        while asyncio.get_running_loop().time() < deadline:
            text = await self._get({"action": "getStatus", "id": order_id})
            text = text.strip()
            if text.startswith("STATUS_OK"):
                code = text.split(":")[1]
                m = re.search(r"\d{5,6}", code)
                return m.group(0) if m else code
            if text in ("STATUS_CANCEL", "STATUS_TIMEOUT", "NO_ACTIVATION"):
                return None
            await asyncio.sleep(6)
        return None

    async def _set_status(self, order_id: str, status: str) -> None:
        try:
            await self._get({"action": "setStatus", "id": order_id, "status": status})
        except Exception as exc:
            log.warning("sms-activate setStatus=%s %s: %s", status, order_id, exc)

    async def cancel_order(self, order_id: str) -> None:
        await self._set_status(order_id, "8")

    async def finish_order(self, order_id: str) -> None:
        """Активация завершена (статус 6)."""
        await self._set_status(order_id, "6")

    async def report_banned(self, order_id: str) -> None:
        """У sms-activate отдельного «номер забанен» нет: отмена (8) и есть
        возврат средств за номер, которым не воспользовались."""
        await self._set_status(order_id, "8")

    async def get_balance(self) -> float:
        text = await self._get({"action": "getBalance"})
        try:
            return float(text.replace("ACCESS_BALANCE:", "").strip())
        except (TypeError, ValueError):
            return 0.0


# ── Factory ───────────────────────────────────────────────────────────────────


def get_sms_client(service: str, api_key: str) -> FiveSimClient | SmsActivateClient:
    if service == "5sim":
        return FiveSimClient(api_key)
    return SmsActivateClient(api_key)
