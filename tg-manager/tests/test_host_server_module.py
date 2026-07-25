"""Модуль Host-Server: цена (редактируемая), доступ, маркетплейс, согласие.

Проверяем реальную логику сервиса на стейтовом фейк-пуле: покупка → доступ,
изменение цены в рантайме, публикация предложений (в т.ч. обязательное согласие
для device_compute), аренда чужого/запрет своего, переходы статусов аренды,
security-гейт исполнения на устройстве.
"""
from __future__ import annotations

import re

import pytest

from services import host_server as hs


# ── стейтовый фейк-пул: минимально моделирует нужные таблицы ──────────────────

class FakePool:
    def __init__(self):
        self.settings: dict[str, str] = {}
        self.access: set[int] = set()
        self.offerings: dict[int, dict] = {}
        self.rentals: dict[int, dict] = {}
        self.payments: list[dict] = []
        self._oid = 0
        self._rid = 0

    async def execute(self, query, *args):
        q = " ".join(query.split())
        if "INSERT INTO host_server_access" in q:
            self.access.add(args[0])
            return "INSERT 0 1"
        if q.startswith("INSERT INTO payments"):
            self.payments.append({"reference": args[-1], "plan": "host_server"})
            return "INSERT 0 1"
        if "UPDATE host_offerings SET is_active" in q:
            oid, owner, active = args
            o = self.offerings.get(oid)
            if o and o["owner_id"] == owner:
                o["is_active"] = active
                return "UPDATE 1"
            return "UPDATE 0"
        if "UPDATE host_rentals SET status" in q:
            rid = args[0]
            new = args[1]
            r = self.rentals.get(rid)
            if r:
                r["status"] = new
                if new == "active" and not r.get("started_at"):
                    r["started_at"] = "now"
            return "UPDATE 1"
        if "CREATE TABLE" in q or "platform_settings" in q:
            # set_platform_setting путь
            if "INSERT INTO platform_settings" in q:
                self.settings[args[0]] = args[1]
            return "OK"
        return "OK"

    async def fetchrow(self, query, *args):
        q = " ".join(query.split())
        if "FROM platform_settings" in q:
            key = args[0]
            return {"value": self.settings[key]} if key in self.settings else None
        if "FROM host_server_access" in q:
            return {"?column?": 1} if args[0] in self.access else None
        if q.startswith("SELECT id FROM payments"):
            return None
        if "INSERT INTO host_offerings" in q:
            self._oid += 1
            (owner, kind, title, desc, specs, region, price, period, adc) = args
            self.offerings[self._oid] = {
                "id": self._oid, "owner_id": owner, "kind": kind, "title": title,
                "description": desc, "specs": specs, "region": region,
                "price_usd": price, "period": period, "is_active": True,
                "allow_device_compute": adc, "created_at": "t",
            }
            return {"id": self._oid}
        if "FROM host_offerings WHERE id=$1" in q:
            return self.offerings.get(args[0])
        if "INSERT INTO host_rentals" in q:
            self._rid += 1
            (offering_id, tenant, provider, period, units, price) = args[:6]
            self.rentals[self._rid] = {
                "id": self._rid, "offering_id": offering_id, "tenant_id": tenant,
                "provider_id": provider, "status": "pending", "period": period,
                "units": units, "price_usd": price, "started_at": None,
                "ends_at": "future", "created_at": "t",
            }
            return {"id": self._rid, "ends_at": "future"}
        if "FROM host_rentals WHERE id=$1" in q:
            rid = args[0]
            scope_val = args[1]
            r = self.rentals.get(rid)
            if not r:
                return None
            # проверка скоупа (provider_id или tenant_id)
            if "provider_id=$2" in q and r["provider_id"] != scope_val:
                return None
            if "tenant_id=$2" in q and r["tenant_id"] != scope_val:
                return None
            return {"status": r["status"]}
        return None

    async def fetch(self, query, *args):
        q = " ".join(query.split())
        if "FROM host_offerings o" in q:  # list_market
            out = []
            for o in self.offerings.values():
                if not o["is_active"]:
                    continue
                out.append(o)
            # применим exclude viewer / kind по args грубо
            return [dict(o) for o in out]
        return []

    async def fetchval(self, query, *args):
        return None


@pytest.fixture()
def pool(monkeypatch):
    p = FakePool()
    # get/set_platform_setting идут через database.db — подменим на пул
    import database.db as db

    async def _get(_pool, key, default=""):
        return _pool.settings.get(key, default)

    async def _set(_pool, key, value):
        _pool.settings[key] = value

    monkeypatch.setattr(db, "get_platform_setting", _get, raising=False)
    monkeypatch.setattr(db, "set_platform_setting", _set, raising=False)
    # не пускаем в реальные admin/plan проверки
    import bot.utils.subscription as sub
    monkeypatch.setattr(sub, "is_platform_admin", lambda uid: False, raising=False)

    async def _plan(_pool, uid):
        return "free"
    monkeypatch.setattr(sub, "get_plan", _plan, raising=False)
    return p


# ── цена ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_price_default_and_editable(pool, monkeypatch):
    monkeypatch.delenv("PRICE_HOST_SERVER", raising=False)
    assert await hs.get_price(pool) == hs.DEFAULT_PRICE_USD
    applied = await hs.set_price(pool, 149)
    assert applied == 149
    assert await hs.get_price(pool) == 149


@pytest.mark.asyncio
async def test_price_validation(pool):
    with pytest.raises(ValueError):
        await hs.set_price(pool, 0)
    with pytest.raises(ValueError):
        await hs.set_price(pool, 10 ** 9)


@pytest.mark.asyncio
async def test_env_price_fallback(pool, monkeypatch):
    monkeypatch.setenv("PRICE_HOST_SERVER", "199")
    assert await hs.get_price(pool) == 199  # env перекрывает дефолт, если нет setting


# ── доступ / покупка ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_purchase_then_grant_gives_access(pool):
    uid = 100
    assert await hs.has_access(pool, uid) is False
    res = await hs.create_purchase(pool, uid)
    assert res["already"] is False and res["reference"].startswith("HST-")
    assert pool.payments and pool.payments[0]["plan"] == "host_server"
    # активация как в payment_checker
    await hs.grant_access(pool, uid, res["reference"])
    assert await hs.has_access(pool, uid) is True
    # повторная покупка при наличии доступа — no-op
    assert (await hs.create_purchase(pool, uid))["already"] is True


# ── предложения / согласие ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_offering_requires_access(pool):
    with pytest.raises(PermissionError):
        await hs.create_offering(pool, 7, kind="server", title="VPS", price_usd=10)


@pytest.mark.asyncio
async def test_device_compute_requires_consent(pool):
    await hs.grant_access(pool, 7)
    with pytest.raises(ValueError):
        await hs.create_offering(
            pool, 7, kind="device_compute", title="Мой ПК", price_usd=5,
            allow_device_compute=False,
        )
    ok = await hs.create_offering(
        pool, 7, kind="device_compute", title="Мой ПК", price_usd=5,
        allow_device_compute=True,
    )
    assert ok["kind"] == "device_compute"


# ── аренда ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_rent_flow_and_cannot_rent_own(pool):
    provider, tenant = 1, 2
    await hs.grant_access(pool, provider)
    off = await hs.create_offering(pool, provider, kind="server", title="VPS", price_usd=10, period="month")
    # свой — нельзя
    with pytest.raises(ValueError):
        await hs.rent_offering(pool, provider, off["id"])
    # чужой — ок, снапшот цены × units
    r = await hs.rent_offering(pool, tenant, off["id"], units=3)
    assert r["status"] == "pending" and r["price_usd"] == 30 and r["provider_id"] == provider


@pytest.mark.asyncio
async def test_rental_status_transitions(pool):
    provider, tenant = 1, 2
    await hs.grant_access(pool, provider)
    off = await hs.create_offering(pool, provider, kind="server", title="VPS", price_usd=10)
    r = await hs.rent_offering(pool, tenant, off["id"])
    rid = r["id"]
    # чужой (не провайдер) не может активировать
    with pytest.raises(PermissionError):
        await hs.set_rental_status(pool, 999, rid, "active", as_provider=True)
    # провайдер активирует pending→active
    res = await hs.set_rental_status(pool, provider, rid, "active", as_provider=True)
    assert res["status"] == "active"
    # недопустимый переход active→rejected
    with pytest.raises(ValueError):
        await hs.set_rental_status(pool, provider, rid, "rejected", as_provider=True)
    # провайдер завершает active→ended
    assert (await hs.set_rental_status(pool, provider, rid, "ended", as_provider=True))["status"] == "ended"


@pytest.mark.asyncio
async def test_rent_inactive_offering_rejected(pool):
    provider, tenant = 1, 2
    await hs.grant_access(pool, provider)
    off = await hs.create_offering(pool, provider, kind="server", title="VPS", price_usd=10)
    await hs.set_offering_active(pool, provider, off["id"], False)
    with pytest.raises(ValueError):
        await hs.rent_offering(pool, tenant, off["id"])


# ── security-гейт устройства ─────────────────────────────────────────────────

def test_device_exec_gated_off_by_default(monkeypatch):
    monkeypatch.delenv("HOST_SERVER_DEVICE_EXEC", raising=False)
    assert hs.device_compute_execution_enabled() is False
    monkeypatch.setenv("HOST_SERVER_DEVICE_EXEC", "1")
    assert hs.device_compute_execution_enabled() is True


# ── интеграция: роуты API и оплата ───────────────────────────────────────────

def test_all_api_routes_registered():
    import pathlib
    src = (pathlib.Path(__file__).resolve().parents[1] / "services" / "mini_app_api.py").read_text("utf-8")
    for route in (
        '/api/miniapp/host_server/status',
        '/api/miniapp/host_server/buy',
        '/api/miniapp/host_server/price',
        '/api/miniapp/host_server/market',
        '/api/miniapp/host_server/offering',
        '/api/miniapp/host_server/offering/{offering_id}/toggle',
        '/api/miniapp/host_server/my',
        '/api/miniapp/host_server/rent',
        '/api/miniapp/host_server/rental/{rental_id}/status',
    ):
        assert route in src, f"роут не зарегистрирован: {route}"
    # админ-гейт на смену цены
    assert "host_server_set_price" in src and "_is_admin(uid)" in src


def test_payment_checker_activates_host_server_as_module_not_subscription():
    """plan='host_server' → grant_access, и НЕ уходит в _activate_subscription."""
    import pathlib
    src = (pathlib.Path(__file__).resolve().parents[1] / "services" / "payment_checker.py").read_text("utf-8")
    assert "host_server.grant_access" in src, "нет выдачи доступа при оплате host_server"
    # разовые модули исключены из активации подписки
    assert '("strike", "host_server")' in src, "host_server не исключён из активации подписки"
