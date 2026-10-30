"""Регресс: @SpamBot-ответ о БАНЕ распознаётся как 'banned', а не теряется.

Жалоба владельца: «система не понимает, что аккаунты заблокированы/заморожены, и
продолжает брать их в работу». Причина: на забаненный аккаунт @SpamBot отвечает
«Your account was blocked for violations of the Telegram Terms of Service» — это
НЕ снимаемый спамблок, а терминальный бан. Такой ответ не матчил ни OK, ни LIMIT
и уходил в None → check_account_status_full отдавал 'unknown', acc_status не
менялся, и забаненный аккаунт оставался в ротации.

Теперь classify_reply возвращает 'banned' (а check_account_status_full — status
'banned', которое ∈ LOST_STATUSES и выводит аккаунт из работы).
"""
from __future__ import annotations

import inspect

from services import account_manager, account_status
from services.spambot_i18n import classify_reply


# ── распознавание бана ────────────────────────────────────────────────────────

def test_english_tos_ban_is_banned():
    # ровно тот текст из жалобы (Операция #721)
    t = ("Your account was blocked for violations of the Telegram Terms of "
         "Service https://telegram.org/tos")
    assert classify_reply(t) == "banned"


def test_russian_ban_is_banned():
    t = "Ваш аккаунт был заблокирован за нарушения условий использования Telegram."
    assert classify_reply(t) == "banned"


def test_ban_wins_over_limit_and_is_not_spamblock():
    # бан терминален — его нельзя понизить до снимаемого спамблока
    t = "Your account was blocked for violations of the Terms of Service."
    assert classify_reply(t) == "banned"


# ── не задели здоровые и спамблок-ветки ───────────────────────────────────────

def test_active_still_active():
    assert classify_reply("Good news, no limits are currently applied to your account.") == "active"


def test_temp_spamblock_not_mistaken_for_ban():
    t = "Some Telegram features may be unavailable to you until 25 Dec 2026."
    assert classify_reply(t) == "spamblock"


def test_perm_spamblock_still_spamblock_not_ban():
    t = "Your account is now limited. This limitation is not going to be lifted automatically."
    assert classify_reply(t) == "spamblock"


def test_empty_is_none():
    assert classify_reply("") is None
    assert classify_reply(None) is None


# ── вывод из работы: banned ∈ LOST и помечает аккаунт мёртвым ──────────────────

def test_banned_is_a_lost_dead_status():
    assert "banned" in account_status.LOST_STATUSES
    assert account_status.is_dead("banned")


def test_banned_persists_to_acc_status():
    # 'unknown' не персистился (и правильно), а 'banned' обязан перезаписать статус
    assert account_manager.should_persist_account_status("banned") is True
    assert account_manager.should_persist_account_status("unknown") is False


# ── проводка в check_account_status_full ──────────────────────────────────────

def test_status_check_returns_banned_for_ban_reply():
    src = inspect.getsource(account_manager.check_account_status_full)
    assert 'spambot_status == "banned"' in src
    assert '"status": "banned"' in src
