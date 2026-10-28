"""Доинвайт целей, упавших из-за лимита участников канала.

Жалоба владельца (операция #240): сотни целей ушли в «Ошибка: The maximum number
of users has been exceeded». Это лимит УЧАСТНИКОВ канала (беда чата), а не вина
цели — такие цели нельзя списывать в окончательный провал, их нужно доинвайтить
в резервный канал / при повторе.

Корень: промоут-трюк (add_via_promote), в который уходят заблокированные
приватностью цели, упирался в полный канал (UsersTooMuch) и ловил это общим
except → failed по каждой цели. Плюс классификатор не распознавал текст
«maximum number of users». Теперь лимит классифицируется как FAIL_CHAT_FULL, и
трюк на нём прекращается, возвращая цели в still_blocked (не failed) — они не
пишутся в журнал приглашённых, поэтому повтор/продолжение доинвайтит их в канал
с местом (ротация резерва теперь по реальным подписчикам).
"""
from __future__ import annotations

import re
from pathlib import Path

from services import mass_inviter_engine as mie

ROOT = Path(__file__).resolve().parents[1]
ENGINE = (ROOT / "services" / "mass_inviter_engine.py").read_text(encoding="utf-8")


def test_maximum_users_text_classified_as_chat_full():
    # Реальный текст UsersTooMuchError из Telethon — даже если пришёл без своего
    # типа (обычным ValueError), классификатор обязан распознать лимит канала.
    for msg in (
        "The maximum number of users has been exceeded (to create a chat, for example)",
        "USERS_TOO_MUCH",
        "too many members in this channel",
    ):
        assert mie.classify_invite_error(Exception(msg)) == mie.FAIL_CHAT_FULL, msg


def test_promote_trick_returns_chat_full_targets_not_failed():
    # В add_via_promote лимит канала прекращает трюк (break) и НЕ пишет цель в
    # failed — иначе невиновные цели (канал был полон) терялись в «Ошибка».
    i = ENGINE.index("async def add_via_promote(")
    j = ENGINE.index("\nasync def ", i + 1)
    body = ENGINE[i:j]
    # есть явная ветка лимита до общего провала
    assert "classify_invite_error(e) == FAIL_CHAT_FULL" in body
    seg = body[body.index("classify_invite_error(e) == FAIL_CHAT_FULL"):]
    # она возвращает остаток в очередь (untried_from) и прекращает трюк, а не failed
    assert "untried_from = _cur" in seg[:400]
    assert "break" in seg[:400]
    # и в этой ветке нет failed += 1
    _branch = seg[:seg.index("log.warning(\"add_via_promote failed")]
    assert "failed += 1" not in _branch
