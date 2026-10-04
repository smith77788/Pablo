"""Мёртвую сессию узнают по СООБЩЕНИЮ Telegram, а не только по коду ошибки.

Решение «сессия мертва» дорогое в обе стороны: True деактивирует аккаунт
(is_active=FALSE) и требует переимпорта, False оставляет его в ротации. Поэтому
детектор один на весь продукт — `op_errors.is_dead_session_text`, и
`account_manager.is_dead_session_error` ходит через него же.

Что было сломано. Оба детектора были написаны по КОДАМ ошибок
(AUTH_KEY_UNREGISTERED, SESSION_REVOKED, USER_DEACTIVATED), а исполнители на
обычном пути передают `str(exc)` — человеческое сообщение Telethon, в котором
кода нет вообще. Проверено на telethon 1.45.0: SESSION_REVOKED приходит как
«The authorization has been invalidated…», USER_DEACTIVATED — как «The user has
been deleted/deactivated», PHONE_NUMBER_BANNED — как «The used phone number has
been banned from Telegram…». Из восьми смертей сессии детектор узнавал ОДНУ
(AuthKeyUnregistered, и то потому, что его сообщение случайно похоже на код).

Цена: аккаунт с отозванным ключом или забаненным номером оставался активным.
Каждая следующая операция брала его как рабочий, коннектилась, падала и писала
владельцу «ошибка» вместо «переимпортируйте аккаунт»; пульс флота показывал
живым то, чего больше нет.

Вторая половина — ложное срабатывание. В паттернах стояло «authorization key»,
и под него попадал AuthKeyPermEmptyError («The method is unavailable for
temporary authorization key»): там сессия ЖИВА, просто метод недоступен
временному ключу, — а аккаунт за это деактивировался. Это тот же класс вреда,
что и у конфликта двух IP (tests/test_auth_key_duplicated_not_dead.py), который
уже чинили.

Сообщения ниже — дословные из telethon 1.45.0 (`str(err_cls(request=None))` без
хвоста «caused by»). Заглушка telethon в conftest настоящих текстов не знает,
поэтому они зафиксированы здесь строками: тест проверяет НАШ детектор на
реальных данных, а не библиотеку.
"""
from __future__ import annotations

import pytest

from services import op_errors
from services.account_manager import is_dead_session_error


# ── Мертва: ключ отозван, аккаунт удалён, номер забанен ─────────────────────
DEAD = {
    "AuthKeyUnregisteredError": "The key is not registered in the system",
    "AuthKeyInvalidError": "The key is invalid",
    "UserDeactivatedError": "The user has been deleted/deactivated",
    "UserDeactivatedBanError": "The user has been deleted/deactivated",
    "SessionRevokedError": (
        "The authorization has been invalidated, because of the user "
        "terminating all sessions"
    ),
    "SessionExpiredError": "The authorization has expired",
    "PhoneNumberBannedError": (
        "The used phone number has been banned from Telegram and cannot be "
        "used anymore. Maybe check https://www.telegram.org/faq_spam"
    ),
}

# ── Жива: аккаунт трогать нельзя ────────────────────────────────────────────
ALIVE = {
    # Конфликт двух IP: лечится кулдауном и повтором.
    "AuthKeyDuplicatedError": (
        "The authorization key (session file) was used under two different IP "
        "addresses simultaneously, and can no longer be used. Use the same "
        "session exclusively, or use different sessions"
    ),
    # Метод недоступен временному ключу — сессия при этом рабочая.
    "AuthKeyPermEmptyError": (
        "The method is unavailable for temporary authorization key, not bound "
        "to permanent"
    ),
    "FloodWaitError": "A wait of 300 seconds is required",
    "ChatAdminRequiredError": (
        "Chat admin privileges are required to do that in the specified chat"
    ),
    "ChannelPrivateError": (
        "The channel specified is private and you lack permission to access it"
    ),
    "UserPrivacyRestrictedError": (
        "The user's privacy settings do not allow you to do this"
    ),
    "PeerFloodError": (
        "Too many requests, try again later (caused by InviteToChannelRequest)"
    ),
}


@pytest.mark.parametrize("name", sorted(DEAD))
def test_dead_session_message_is_recognized(name):
    msg = DEAD[name]
    assert op_errors.is_dead_session_text(msg) is True, (
        f"{name}: сообщение Telegram о мёртвой сессии не распознано — аккаунт "
        "останется активным, и каждая операция будет в него бить"
    )
    assert is_dead_session_error(msg) is True, f"{name}: вторая дверь не согласна"


@pytest.mark.parametrize("name", sorted(ALIVE))
def test_live_account_is_not_buried(name):
    msg = ALIVE[name]
    assert op_errors.is_dead_session_text(msg) is False, (
        f"{name}: живой аккаунт объявлен мёртвым — его деактивируют (is_active="
        "FALSE) и выведут из флота без причины"
    )
    assert is_dead_session_error(msg) is False, f"{name}: вторая дверь не согласна"


def test_error_codes_still_work():
    """Текст, собранный нами самими, содержит код — он тоже должен ловиться."""
    for code in ("AUTH_KEY_UNREGISTERED", "SESSION_REVOKED", "SESSION_EXPIRED",
                 "USER_DEACTIVATED_BAN", "PHONE_NUMBER_BANNED"):
        assert op_errors.is_dead_session_text(code) is True, code
        assert is_dead_session_error(code) is True, code


def test_both_doors_agree_everywhere():
    """Две двери на одно решение — расхождение и было первопричиной."""
    for msg in list(DEAD.values()) + list(ALIVE.values()) + ["", "   ", "whatever"]:
        assert op_errors.is_dead_session_text(msg) == is_dead_session_error(msg), msg


def test_empty_is_not_a_death():
    assert op_errors.is_dead_session_text("") is False
    assert op_errors.is_dead_session_text(None) is False
    assert is_dead_session_error(None) is False


def test_expired_authorization_is_fatal_not_retried():
    """Истёкшая авторизация от повтора не оживает — иначе операция бьёт в неё
    до конца своих попыток, а это запросы мёртвой сессией."""
    assert "SessionExpiredError" in op_errors._FATAL_ERRORS
    assert "AuthKeyInvalidError" in op_errors._FATAL_ERRORS
    # А конфликт двух IP фаталом быть не должен — его чинит кулдаун.
    assert "AuthKeyDuplicatedError" not in op_errors._FATAL_ERRORS


# ── Канарейка жалоб видит мёртвую сессию как опасный исход ──────────────────

def test_strike_canary_counts_a_dead_session_as_danger():
    """Волна жалоб — самая баноопасная операция продукта, и тормоз у неё один:
    канарейка по исходам первых аккаунтов. Её список маркеров написан по кодам
    ошибок, поэтому отозванный ключ читался как безликий «failed» — волна шла
    дальше уже мёртвым флотом."""
    from services import strike_engine as se

    revoked = {"error": DEAD["SessionRevokedError"]}
    assert se._acc_result_fleet_danger(revoked) is True
    assert se._acc_outcome(revoked) == "banned"

    deleted = {"error": DEAD["UserDeactivatedError"]}
    assert se._acc_result_fleet_danger(deleted) is True
    assert se._acc_outcome(deleted) == "banned"

    # Живой аккаунт опасным исходом не становится: иначе канарейка остановит
    # работающую волну.
    ok = {"error": ALIVE["ChannelPrivateError"], "peer_reported": False}
    assert se._acc_result_fleet_danger(ok) is False
    assert se._acc_outcome(ok) == "failed"
