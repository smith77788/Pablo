"""H1/H2 инвайта: бан САМОГО инвайтера ≠ провал цели; временные сбои не дедупим.

Жалоба владельца: «людям постоянно жалуются — не дозваться».
  • H1: бан/отзыв самого аккаунта-инвайтера (UserDeactivatedBan, UserBannedInChannel,
    AuthKeyUnregistered) раньше классифицировался как провал цели (FAIL_RESTRICTED/
    FAIL_BANNED) → цель списывалась в провал И в журнал приглашённых, и человек
    больше НИКОГДА не приглашался. Теперь это FAIL_SELF_BAN: аккаунт выводится из
    круга, цель возвращается в очередь.
  • H2: username-путь писал в дедуп ВСЕ попробованные цели (tried), включая
    временные сбои (флуд, таймаут, «прочее»). Теперь дедупим только реально
    решённых — добавленных (added) и перманентно мёртвых (dead).
"""
from __future__ import annotations

import pytest

from services import mass_inviter_engine as mie
from services import op_worker
from tests.test_invite_queue_scheduler import (  # noqa: F401 — фикстуры стенда
    _Pool,
    _reset_account_claims,
    _run,
    stand,
)

TARGETS = [f"u{i}" for i in range(1, 11)]


def _exc(name: str) -> Exception:
    """Исключение с нужным именем класса (classify смотрит на type().__name__)."""
    return type(name, (Exception,), {})("boom")


# ── H1: классификация бана инвайтера ─────────────────────────────────────────

def test_self_ban_errors_classified_as_self_ban_not_target_fault():
    for name in ("UserDeactivatedBanError", "UserBannedInChannelError",
                 "AuthKeyUnregisteredError", "AuthKeyDuplicatedError",
                 "SessionRevokedError", "UserDeactivatedError"):
        kind = mie.classify_invite_error(_exc(name))
        assert kind == mie.FAIL_SELF_BAN, f"{name} → {kind}, ожидался self_ban"
        # Критично: это НЕ вина цели — не должно быть провалом цели.
        assert kind not in (mie.FAIL_RESTRICTED, mie.FAIL_BANNED), name


def test_target_side_errors_stay_target_side():
    # Кик цели из чата и ограничение САМОЙ цели — по-прежнему про цель, не self_ban.
    assert mie.classify_invite_error(_exc("UserKickedError")) == mie.FAIL_BANNED
    assert mie.classify_invite_error(_exc("UserRestrictedError")) == mie.FAIL_RESTRICTED
    assert mie.classify_invite_error(_exc("UserPrivacyRestrictedError")) == mie.FAIL_PRIVACY


# ── H1: исполнитель не теряет цели забаненного инвайтера ──────────────────────

@pytest.fixture
def _capture_dedup(monkeypatch):
    """Перехватить, какие цели реально ушли в журнал дедупа (invite_target_log)."""
    seen: set = set()

    async def _cap(pool, owner_id, group_key, op_id, targets):
        seen.update(str(t) for t in targets if t is not None)
        return True

    monkeypatch.setattr(op_worker, "_record_invited_targets", _cap)
    return seen


def test_banned_inviter_returns_targets_to_queue_not_failed(stand, _capture_dedup):
    """Инвайтер забанен — его цели возвращаются в очередь (не провал, не дедуп).

    Работает только аккаунт 1 (у аккаунта 2 суточный лимит 0), и он сразу
    «забанен»: цели обязаны остаться в остатке, а не списаться."""
    def responder(acc_id, refs, dry=False):
        return {"ok": 0, "failed": 0, "errors": ["account error: инвайтер заблокирован"],
                "account_dead": True, "account_dead_reason": "banned",
                "untried": list(refs), "added": [], "dead": []}

    stand(responder, limits={2: 0})  # только аккаунт 1 в деле
    res = _run(_Pool(), TARGETS)

    assert res["failed"] == 0, "бан инвайтера не должен считаться провалом целей"
    assert res["left"] == len(TARGETS), "цели забаненного инвайтера обязаны остаться в очереди"
    assert not _capture_dedup, "цели забаненного инвайтера НЕ должны попасть в дедуп"


def test_banned_inviter_targets_rescued_by_healthy_account(stand, _capture_dedup):
    """Аккаунт 1 забанен, аккаунт 2 здоров — все цели забирает аккаунт 2."""
    def responder(acc_id, refs, dry=False):
        if acc_id == 1:
            return {"ok": 0, "failed": 0, "errors": ["account error: бан"],
                    "account_dead": True, "account_dead_reason": "session",
                    "untried": list(refs), "added": [], "dead": []}
        return {"ok": len(refs), "failed": 0, "errors": [], "added": list(refs), "dead": []}

    s = stand(responder)
    res = _run(_Pool(), TARGETS)
    assert res["left"] == 0, "здоровый аккаунт обязан подобрать цели забаненного"
    assert res["failed"] == 0


# ── H2: временные сбои не пишутся в дедуп ─────────────────────────────────────

def test_only_added_and_dead_are_deduped_not_temp_failures(stand, _capture_dedup):
    """Из 10 целей: 6 добавлено, 1 мёртвый username, 3 временный сбой (флуд/таймаут).

    В журнал приглашённых обязаны попасть ТОЛЬКО 6 добавленных + 1 мёртвый.
    Три временных сбоя не дедупятся — их дозовут следующим прогоном."""
    added = TARGETS[:6]
    dead = TARGETS[6:7]
    temp = TARGETS[7:]  # u8,u9,u10 — временные сбои (FAIL_OTHER/FLOOD)

    def responder(acc_id, refs, dry=False):
        _added = [r for r in refs if r in added]
        _dead = [r for r in refs if r in dead]
        _temp = [r for r in refs if r in temp]
        return {"ok": len(_added), "failed": len(_dead) + len(_temp),
                "errors": [f"{r}: таймаут" for r in _temp],
                "fail_kinds": {"dead": len(_dead), "other": len(_temp)},
                "added": _added, "dead": _dead, "untried": []}

    s = stand(responder)
    _run(_Pool(), TARGETS)

    assert _capture_dedup == set(added) | set(dead), (
        f"в дедуп должны попасть только добавленные+мёртвые, а попало {_capture_dedup}")
    for t in temp:
        assert t not in _capture_dedup, f"временный сбой {t} не должен дедупиться"
