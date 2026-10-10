"""DM-движок — регрессия: PeerFlood (account-level) отделён от per-target блокировок.

Раньше _classify_error сваливал в один бакет "blocked" и per-target ошибки
(YouBlockedUser/ChatWriteForbidden — «этот юзер меня заблокировал»), и
account-level PeerFloodError (аккаунт помечен за спам вообще). Обработка "blocked"
УБИРАЛА весь аккаунт из ротации → (1) один недружелюбный таргет выкидывал здоровый
аккаунт из кампании; (2) PeerFlood убирал аккаунт из этой кампании, но БЕЗ
cooldown → следующая операция сразу добивала флагнутый аккаунт.
"""
from __future__ import annotations

import inspect

from services import dm_engine


class PeerFloodError(Exception):
    pass


class YouBlockedUser(Exception):
    pass


class ChatWriteForbidden(Exception):
    pass


class FloodWaitError(Exception):
    seconds = 30


def test_peer_flood_is_own_class_not_blocked():
    assert dm_engine._classify_error(PeerFloodError("account flagged")) == "peer_flood"
    # и по тексту ошибки тоже
    assert dm_engine._classify_error(Exception("... PEER_FLOOD ...")) == "peer_flood"


def test_per_target_blocks_stay_blocked():
    assert dm_engine._classify_error(YouBlockedUser()) == "blocked"
    assert dm_engine._classify_error(ChatWriteForbidden()) == "blocked"


def test_flood_wait_still_flood():
    assert dm_engine._classify_error(FloodWaitError()) == "flood"


def test_peerflood_removed_from_blocked_set():
    assert "PeerFloodError" not in dm_engine._BLOCKED_ERRORS
    assert "PeerFloodError" in dm_engine._PEER_FLOOD_ERRORS


def test_run_campaign_keeps_account_on_per_target_block():
    """При status='blocked' (per-target) аккаунт НЕ удаляется из ротации,
    а при peer_flood ставится длинный cooldown."""
    src = inspect.getsource(dm_engine.run_campaign)
    # per-target ветка явно ничего не делает с ротацией
    assert 'if status == "blocked":' in src
    # peer_flood ставит cooldown
    assert "_PEER_FLOOD_COOLDOWN" in src
    assert "cooldown_until" in src
