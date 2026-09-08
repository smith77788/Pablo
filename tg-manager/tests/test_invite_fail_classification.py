"""Инвайт: «прочее» перестало прятать диагностируемые причины.

На живом прогоне из 177 отказов 58 попали в «❓ прочее» — крупная корзина без
имени и без смысла: оператор не понимал, чинить ему аудиторию, права или чат.
Часть из них вообще не отказы («цель уже в чате»), часть — конец работы с этим
чатом («достигнут предел участников»).

Отдельно здесь стережётся решение владельца: рассылку ссылок в ЛС незнакомцам
продукт больше НЕ советует — она собирает жалобы и уничтожает флот.
"""
from __future__ import annotations

import pathlib

from services import mass_inviter_engine as E

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_UI = (_ROOT / "mini_app" / "index.html").read_text(encoding="utf-8")


class _Err(Exception):
    """Ошибка с подменённым именем класса — как отдаёт их Telethon."""


def _exc(name: str, text: str = ""):
    return type(name, (_Err,), {})(text)


# ── Новые корзины ──────────────────────────────────────────────────────────

def test_already_in_chat_is_not_a_failure_bucket_of_its_own():
    """Раньше «уже в чате» падало в «прочее» и считалось отказом."""
    assert E.classify_invite_error(_exc("UserAlreadyParticipantError")) == E.FAIL_ALREADY_IN


def test_chat_full_is_recognised():
    assert E.classify_invite_error(_exc("UsersTooMuchError")) == E.FAIL_CHAT_FULL


def test_restricted_target_is_recognised():
    assert E.classify_invite_error(_exc("UserRestrictedError")) == E.FAIL_RESTRICTED


def test_bot_target_is_recognised():
    assert E.classify_invite_error(_exc("BotGroupsBlockedError")) == E.FAIL_IS_BOT


def test_missing_rights_is_perm_not_other():
    assert E.classify_invite_error(_exc("ChatAdminRequiredError")) == E.FAIL_PERM


def test_plain_valueerror_variants_are_classified_by_text():
    """Часть случаев Telethon отдаёт обычным ValueError без своего типа."""
    assert E.classify_invite_error(ValueError("User already participant")) == E.FAIL_ALREADY_IN
    assert E.classify_invite_error(ValueError("USERS_TOO_MUCH")) == E.FAIL_CHAT_FULL
    assert E.classify_invite_error(ValueError("Chat admin privileges are required")) == E.FAIL_PERM


def test_privacy_still_wins_over_restricted_wording():
    """«privacy restricted» — это приватность, а не ограничение аккаунта."""
    assert E.classify_invite_error(ValueError("user privacy restricted")) == E.FAIL_PRIVACY


def test_known_buckets_did_not_regress():
    assert E.classify_invite_error(_exc("UserPrivacyRestrictedError")) == E.FAIL_PRIVACY
    assert E.classify_invite_error(_exc("UserNotMutualContactError")) == E.FAIL_NOT_MUTUAL
    assert E.classify_invite_error(_exc("FloodWaitError")) == E.FAIL_FLOOD
    assert E.classify_invite_error(_exc("UserChannelsTooMuchError")) == E.FAIL_TOO_MANY_CHATS


def test_genuinely_unknown_still_falls_back_to_other():
    """Корзина «прочее» должна остаться — врать о причине хуже, чем признать
    незнание."""
    assert E.classify_invite_error(ValueError("совершенно новая ошибка")) == E.FAIL_OTHER


# ── Каждая корзина названа и объяснена ─────────────────────────────────────

def test_every_bucket_has_a_label():
    for name in dir(E):
        if not name.startswith("FAIL_") or name in ("FAIL_LABELS", "FAIL_ADVICE"):
            continue
        val = getattr(E, name)
        if isinstance(val, str):
            assert val in E.FAIL_LABELS, f"{name} без человеческой подписи"


def test_new_buckets_carry_advice():
    for b in (E.FAIL_ALREADY_IN, E.FAIL_CHAT_FULL, E.FAIL_RESTRICTED, E.FAIL_IS_BOT):
        assert E.FAIL_ADVICE.get(b), f"{b} без подсказки «что делать»"


# ── Запрет владельца: не советовать рассылку ссылок в ЛС ───────────────────

def test_privacy_advice_no_longer_recommends_dm_link():
    """Рассылка ссылок в ЛС незнакомцам уничтожает флот — продукт не должен её
    предлагать как решение приватности."""
    advice = E.FAIL_ADVICE[E.FAIL_PRIVACY]
    assert "ЛС" not in advice
    assert "обойти нельзя" in advice


def test_not_mutual_advice_no_longer_recommends_dm_link():
    assert "ЛС" not in E.FAIL_ADVICE[E.FAIL_NOT_MUTUAL]


def test_no_advice_text_recommends_dm_blasting():
    for bucket, text in E.FAIL_ADVICE.items():
        assert "ссылка в ЛС" not in text and "ссылку в ЛС" not in text, bucket


def test_ui_warns_about_the_dm_link_method():
    """Метод оставлен, но это не нейтральный выбор — предупреждаем у выбора,
    а не после потери флота."""
    assert "риск для флота" in _UI
    assert "убивает аккаунты" in _UI
