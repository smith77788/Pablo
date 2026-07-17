"""Регресс-тесты ядра «применить в один клик» (Copilot apply).

Проверяем _retry_failed_ops_core: mass_publish пропускается (иначе дубли постов),
остальные упавшие ре-сабмятся ЧЕРЕЗ operation_bus (не прямым INSERT), JSON-params
корректно парсятся.
"""

import services.operation_bus as obus
from services.mini_app_api import _retry_failed_ops_core


class _Pool:
    def __init__(self, rows):
        self._rows = rows

    async def fetch(self, q, *a):
        return self._rows


async def test_retry_skips_mass_publish_and_submits_rest(monkeypatch):
    calls = []

    async def fake_submit(pool, owner_id, op_type, params, **kw):
        calls.append({"op_type": op_type, "params": params,
                      "total_items": kw.get("total_items"), "label": kw.get("label")})
        return 999

    monkeypatch.setattr(obus, "submit", fake_submit)

    rows = [
        {"op_type": "mass_invite", "params": {"x": 1}, "label": "Инвайт", "total_items": 10},
        {"op_type": "mass_publish", "params": {"y": 2}, "label": "Публикация", "total_items": 5},
        {"op_type": "run_broadcast", "params": '{"z": 3}', "label": "Рассылка", "total_items": 7},
    ]
    res = await _retry_failed_ops_core(_Pool(rows), uid=42)

    assert res["retried"] == 2          # mass_invite + run_broadcast
    assert res["skipped"] == 1          # mass_publish
    op_types = [c["op_type"] for c in calls]
    assert "mass_publish" not in op_types
    assert set(op_types) == {"mass_invite", "run_broadcast"}
    # JSON-строка params распарсилась в dict
    br = next(c for c in calls if c["op_type"] == "run_broadcast")
    assert br["params"] == {"z": 3}
    assert br["total_items"] == 7


async def test_retry_handles_broken_params(monkeypatch):
    async def fake_submit(pool, owner_id, op_type, params, **kw):
        assert isinstance(params, dict)   # никогда не отдаём не-dict в шину
        return 1

    monkeypatch.setattr(obus, "submit", fake_submit)
    rows = [
        {"op_type": "bulk_join", "params": "not-json", "label": None, "total_items": None},
        {"op_type": "bulk_leave", "params": None, "label": None, "total_items": 0},
    ]
    res = await _retry_failed_ops_core(_Pool(rows), uid=1)
    assert res["retried"] == 2


async def test_retry_empty(monkeypatch):
    async def fake_submit(*a, **k):
        raise AssertionError("submit не должен вызываться при пустом списке")

    monkeypatch.setattr(obus, "submit", fake_submit)
    res = await _retry_failed_ops_core(_Pool([]), uid=1)
    assert res == {"ok": True, "retried": 0, "skipped": 0}


async def test_build_ecosystem_core_creates_and_adds_channels(monkeypatch):
    import services.ecosystem_brain as eb
    from services.mini_app_api import _build_ecosystem_core

    created = {}
    added = []

    async def fake_create(pool, owner_id, name, **kw):
        created["name"] = name
        created["owner"] = owner_id
        return 77

    async def fake_add(pool, eco_id, owner_id, object_type, object_id, role="member"):
        added.append((eco_id, object_type, object_id))
        return True

    monkeypatch.setattr(eb, "create_ecosystem", fake_create)
    monkeypatch.setattr(eb, "add_member", fake_add)

    class P:
        async def fetch(self, q, *a):
            return [{"channel_id": 10}, {"channel_id": 20}, {"channel_id": 30}]

    res = await _build_ecosystem_core(P(), uid=5)
    assert res["ecosystem_id"] == 77
    assert res["channels"] == 3
    assert created["owner"] == 5
    assert added == [(77, "channel", 10), (77, "channel", 20), (77, "channel", 30)]


async def test_build_ecosystem_core_no_channels(monkeypatch):
    import services.ecosystem_brain as eb
    from services.mini_app_api import _build_ecosystem_core

    async def fake_create(pool, owner_id, name, **kw):
        return 1

    async def fake_add(*a, **k):
        raise AssertionError("add_member не должен вызываться без каналов")

    monkeypatch.setattr(eb, "create_ecosystem", fake_create)
    monkeypatch.setattr(eb, "add_member", fake_add)

    class P:
        async def fetch(self, q, *a):
            return []

    res = await _build_ecosystem_core(P(), uid=5)
    assert res["channels"] == 0
    assert res["ecosystem_id"] == 1


async def test_assign_proxies_reuses_apply_rotation(monkeypatch):
    """assign_proxies должен вызывать вылизанный proxy_rotation.apply_rotation с
    id аккаунтов без прокси, а не писать логику изоляции сам."""
    import services.proxy_rotation as pr
    from services.mini_app_api import _apply_next_action

    # apply_rotation вызывается с account_ids неназначенных аккаунтов
    seen = {}

    async def fake_apply(pool, owner_id, account_ids=None, proxy_ids=None):
        seen["account_ids"] = account_ids
        seen["owner"] = owner_id
        return {"ok": True, "rotated": len(account_ids or []), "skipped_no_proxy": 0}

    monkeypatch.setattr(pr, "apply_rotation", fake_apply)

    class P:
        async def fetch(self, q, *a):
            return [{"id": 1}, {"id": 2}]

    res = await _apply_next_action(P(), uid=9, action_id="assign_proxies")
    assert res.get("ok")
    assert seen["account_ids"] == [1, 2]
    assert seen["owner"] == 9
    assert "2" in res["message"]


async def test_apply_unknown_action_returns_error():
    from services.mini_app_api import _apply_next_action

    class P:
        async def fetch(self, q, *a):
            return []

    res = await _apply_next_action(P(), uid=1, action_id="nope_not_real")
    assert res.get("error")


async def test_apply_assign_proxies_no_unassigned():
    from services.mini_app_api import _apply_next_action

    class P:
        async def fetch(self, q, *a):
            return []   # нет аккаунтов без прокси

    res = await _apply_next_action(P(), uid=1, action_id="assign_proxies")
    assert res["ok"] and "уже с прокси" in res["message"]
