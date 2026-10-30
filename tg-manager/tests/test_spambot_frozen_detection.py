"""Регресс: @SpamBot-ответ о ЗАМОРОЗКЕ распознаётся как 'frozen', а не теряется.

Вторая половина жалобы владельца (первая — бан): «система не понимает, что
аккаунты заблокированы/ЗАМОРОЖЕНЫ, и продолжает брать их в работу». Заморозка —
средняя ступень Telegram (read-only, снимается только апелляцией через @SpamBot,
иначе аккаунт удаляется после названной даты). На неё @SpamBot отвечает НЕ
стандартным приветствием, а текстом про заморозку. Этот ответ не матчил ни OK,
ни BAN, ни LIMIT → уходил в None → check_account_status_full отдавал 'unknown',
acc_status не менялся, и замороженный аккаунт оставался в ротации.

Теперь classify_reply возвращает 'frozen' (а check_account_status_full — status
'frozen'); 'frozen' ∈ DEAD_STATUSES (из работы выводится), но ∈
RESTRICTED_STATUSES, НЕ LOST — снимается апелляцией, а не потерян навсегда.
"""
from __future__ import annotations

import inspect

from services import account_manager, account_status
from services.spambot_i18n import classify_reply


# ── распознавание заморозки ───────────────────────────────────────────────────

def test_english_frozen_is_frozen():
    # дословный текст Шага 1 @SpamBot при заморозке
    t = ("Your account has been frozen for a serious violation of Telegram's "
         "Terms and Conditions. Your account is now in read-only mode (since "
         "12 Oct 2025). You can appeal via @SpamBot if you believe this is a "
         "mistake. If you do not contact us before 26 Oct 2025, your account "
         "will be permanently deactivated and deleted.")
    assert classify_reply(t) == "frozen"


def test_russian_frozen_is_frozen():
    t = ("Ваш аккаунт был заморожен за серьезное нарушение Условий использования "
         "Telegram. Теперь ваш аккаунт находится в режиме «только чтение».")
    assert classify_reply(t) == "frozen"


def test_read_only_mode_alone_is_frozen():
    # @SpamBot часто отвечает по-английски даже не-EN аккаунту
    assert classify_reply("Your account is now in read-only mode.") == "frozen"


# ── заморозка НЕ путается с баном и спамблоком (и наоборот) ────────────────────

def test_frozen_not_mistaken_for_ban():
    # «serious violation» есть и в заморозке, и в бане — но бан говорит «blocked»
    assert classify_reply("Your account has been frozen for a serious violation.") == "frozen"


def test_ban_still_banned_not_frozen():
    t = "Your account was blocked for violations of the Telegram Terms of Service."
    assert classify_reply(t) == "banned"


def test_temp_spamblock_still_spamblock_not_frozen():
    assert classify_reply("Some Telegram features may be unavailable to you until 25 Dec 2026.") == "spamblock"


def test_active_still_active():
    assert classify_reply("Good news, no limits are currently applied to your account.") == "active"


# ── вывод из работы: frozen ∈ DEAD, но ∈ RESTRICTED (не LOST) ──────────────────

def test_frozen_is_dead_for_work():
    assert "frozen" in account_status.DEAD_STATUSES
    assert account_status.is_dead("frozen")


def test_frozen_is_restricted_not_lost():
    # заморозка снимается апелляцией — её нельзя советовать «очистить» как бан
    assert "frozen" in account_status.RESTRICTED_STATUSES
    assert "frozen" not in account_status.LOST_STATUSES


def test_frozen_persists_to_acc_status():
    # 'unknown' не персистился (и правильно), а 'frozen' обязан перезаписать статус
    assert account_manager.is_verified_account_restriction("frozen") is True
    assert account_manager.should_persist_account_status("frozen") is True
    assert account_manager.should_persist_account_status("unknown") is False


# ── проводка в check_account_status_full ──────────────────────────────────────

def test_status_check_returns_frozen_for_frozen_reply():
    src = inspect.getsource(account_manager.check_account_status_full)
    assert 'spambot_status == "frozen"' in src
    assert '"status": "frozen"' in src
