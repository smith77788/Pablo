"""Тесты движка поиска потерянного контакта (services/contact_finder).

Чистые помощники + цикл hunt с мок-резолвером: проверяем матч имени, генерацию
кандидатов, ранний стоп при совпадении, отсутствие ложных матчей, обработку
FloodWait (кандидат не считается проверенным) и отмену.
"""
from __future__ import annotations

import asyncio

from services import contact_finder as cf


def test_name_matches():
    assert cf.name_matches("Oracle", "oracle") is True
    assert cf.name_matches("The Oracle 🔮", "Oracle") is True
    assert cf.name_matches("oracle", "The Oracle") is True   # вхождение в обе стороны
    assert cf.name_matches("Smile", "Oracle") is False
    assert cf.name_matches("", "Oracle") is False
    assert cf.name_matches("Oracle", "") is False            # пустой target — не матч


def test_valid_username():
    assert cf.valid_username("Smile001") is True
    assert cf.valid_username("@Smile99") is True
    assert cf.valid_username("Smi") is False                 # короче 5
    assert cf.valid_username("1Smile") is False              # начинается с цифры


def test_iter_candidates_padding_and_cap():
    c2 = list(cf.iter_candidates("Smile", 2))
    assert c2[0] == "Smile00" and c2[-1] == "Smile99" and len(c2) == 100
    # cap режет
    capped = list(cf.iter_candidates("Smile", 3, cap=10))
    assert len(capped) == 10 and capped[0] == "Smile000" and capped[9] == "Smile009"
    # невалидный префикс/digits → пусто
    assert list(cf.iter_candidates("", 2)) == []
    assert list(cf.iter_candidates("Smile", 0)) == []


def _resolver_from(existing: dict[str, str]):
    """existing: username(lower) → display name. Всё остальное — не существует."""
    async def _resolve(uname):
        name = existing.get(uname.lower())
        if name is None:
            return {"username": uname, "exists": False, "user_id": None, "name": "",
                    "premium": False, "flood_wait": 0, "error": None}
        return {"username": uname, "exists": True, "user_id": 111, "name": name,
                "premium": False, "flood_wait": 0, "error": None}
    return _resolve


def test_hunt_finds_and_stops_early():
    cands = list(cf.iter_candidates("Smile", 2))          # Smile00..Smile99
    resolver = _resolver_from({"smile42": "Oracle", "smile07": "Someone Else"})
    out = asyncio.run(cf.hunt(candidates=cands, target_name="Oracle",
                              resolve=resolver, pace_s=0))
    assert out["matches"], "должен найтись Smile42 = Oracle"
    assert out["matches"][0]["username"].lower() == "smile42"
    assert out["stopped_early"] is True
    # Ранняя остановка: не должны были дойти до конца (Smile42 — 43-й)
    assert out["checked"] <= 43
    # Smile07 существует, но имя не Oracle → в found, не в matches
    assert any(f["username"].lower() == "smile07" for f in out["found"])


def test_hunt_no_match_scans_all():
    cands = list(cf.iter_candidates("Smile", 2))
    resolver = _resolver_from({"smile07": "Someone Else"})   # никого с Oracle
    out = asyncio.run(cf.hunt(candidates=cands, target_name="Oracle",
                              resolve=resolver, pace_s=0))
    assert out["matches"] == []
    assert out["stopped_early"] is False
    assert out["checked"] == 100                              # прошли всех


def test_hunt_floodwait_not_counted_as_checked():
    calls = {"n": 0}
    backoffs = {"n": 0}

    async def resolver(uname):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"username": uname, "exists": False, "flood_wait": 30, "name": "",
                    "user_id": None, "premium": False, "error": "flood_wait"}
        return {"username": uname, "exists": False, "flood_wait": 0, "name": "",
                "user_id": None, "premium": False, "error": None}

    async def backoff(sec):
        backoffs["n"] += 1

    cands = ["Smile00", "Smile01", "Smile02"]
    out = asyncio.run(cf.hunt(candidates=cands, target_name="Oracle", resolve=resolver,
                              pace_s=0, flood_backoff_cb=backoff))
    assert out["flood_waits"] == 1
    assert backoffs["n"] == 1
    assert out["checked"] == 2            # floodwait-кандидат не засчитан проверенным


def test_hunt_cancellation():
    async def is_cancelled():
        return True
    out = asyncio.run(cf.hunt(candidates=["Smile00", "Smile01"], target_name="Oracle",
                              resolve=_resolver_from({}), pace_s=0,
                              is_cancelled=is_cancelled))
    assert out["cancelled"] is True
    assert out["checked"] == 0
