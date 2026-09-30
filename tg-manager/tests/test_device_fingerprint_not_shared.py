"""Аккаунты без сохранённого профиля не должны делить один device fingerprint.

Аудит следов автоматизации (F11): _normalize_device_profile ставил ЖЁСТКО
'Samsung SM-S911B'/'Android 14' всем аккаунтам без device — байт-в-байт один
fingerprint = тривиальная кластеризация фермы. Теперь дефолт выбирается из пула
детерминированно по id аккаунта: один аккаунт всегда одно устройство (смена
device между коннектами сама палит), но разные аккаунты — разные устройства.
"""
from __future__ import annotations

from services import account_manager as am


def test_same_account_gets_stable_device():
    a = am._normalize_device_profile({"id": 101})
    b = am._normalize_device_profile({"id": 101})
    assert a["device_model"] == b["device_model"]
    assert a["system_version"] == b["system_version"]
    assert a["app_version"] == b["app_version"]


def test_different_accounts_spread_across_pool():
    models = {am._normalize_device_profile({"id": i})["device_model"] for i in range(1, 30)}
    assert len(models) > 1, "флот без профиля не должен делить один device"
    # и это не жёсткий старый дефолт на всех
    assert models != {"Samsung SM-S911B"}


def test_explicit_device_is_respected():
    d = am._normalize_device_profile(
        {"id": 5, "device_model": "Custom X", "system_version": "Android 99"})
    assert d["device_model"] == "Custom X"
    assert d["system_version"] == "Android 99"


def test_missing_id_does_not_crash():
    d = am._normalize_device_profile({})
    assert d.get("device_model") and d.get("system_version")
