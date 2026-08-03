"""AUTH_KEY_DUPLICATED — временный конфликт, а НЕ смерть аккаунта.

Жалоба пользователя: «аккаунты не должны падать на AUTH_KEY, даже если аккаунт
авторизован не только у нас, но и на другом устройстве».

Механика Telegram: многосессионность штатная — у каждого устройства свой auth_key,
и сессия на телефоне сама по себе конфликта не вызывает. AUTH_KEY_DUPLICATED («used
under two different IP addresses simultaneously») возникает, когда ОДНА И ТА ЖЕ
сессия секунду шла с двух IP (общая/импортированная сессия активна ещё где-то, либо
дёрнулся прокси). Это ВРЕМЕННО и обычно оживает — деактивировать аккаунт нельзя.

Был баг: AUTH_KEY_DUPLICATED классифицировался как 'dead'/'fatal' → аккаунт
деактивировался (is_active=FALSE) во всех исполнителях и в contact-sync. Один
кратковременный конфликт (телефон на миг онлайн / флап прокси) выключал сразу весь
флот. Фикс: отдельный класс _is_session_conflict_error — конфликт лечится как
транспортная проблема (кулдаун + повтор), НЕ как смерть.
"""
from __future__ import annotations

from services import op_worker
from services.contacts_hub.sync_service import classify_session_error


_DUP = "The authorization key (session file) was used under two different IP addresses simultaneously, and could have been used by an attacker"
_DUP2 = "AUTH_KEY_DUPLICATED"


def test_conflict_detected():
    assert op_worker._is_session_conflict_error(_DUP)
    assert op_worker._is_session_conflict_error(_DUP2)
    assert not op_worker._is_session_conflict_error("FLOOD_WAIT 30")


def test_conflict_is_not_dead_session():
    # Ключевое: НЕ деактивируем аккаунт из-за конфликта двух IP.
    assert op_worker._is_dead_session_error(_DUP) is False
    assert op_worker._is_dead_session_error(_DUP2) is False
    # но настоящая мёртвая сессия по-прежнему dead
    assert op_worker._is_dead_session_error("AuthKeyUnregistered") is True
    assert op_worker._is_dead_session_error("SESSION_REVOKED") is True


def test_conflict_is_cooldown_not_fatal():
    # op-уровень: не 'fatal' (не деактивируем), а 'retry' (остынет и повторит).
    assert op_worker._classify_op_error(RuntimeError(_DUP)) == "retry"
    assert op_worker._classify_op_error(RuntimeError(_DUP2)) == "retry"
    # настоящий фатал остаётся фаталом
    assert op_worker._classify_op_error(RuntimeError("USER_DEACTIVATED_BAN")) == "fatal"


def test_conflict_isolated_like_transport():
    # Конфликт лечится как сетевая/транспортная проблема — аккаунт остужается
    # (isolation), а не деактивируется.
    assert op_worker._is_network_or_proxy_error(_DUP) is True


def test_fatal_pattern_no_longer_contains_dup():
    # AUTH_KEY_DUPLICATED убран из фатальных паттернов.
    assert not op_worker._FATAL_MSG_PATTERNS.search("AUTH_KEY_DUPLICATED")


def test_contact_sync_does_not_deactivate_on_conflict():
    friendly, status = classify_session_error(_DUP)
    assert status != "dead", "конфликт двух IP не должен деактивировать аккаунт"
    assert status == "net", "лечим как временную транспортную проблему"
    assert "НЕ отключён" in friendly, "пользователю честно: аккаунт не потерян"
    # а настоящая смерть — по-прежнему dead
    _f2, s2 = classify_session_error("USER_DEACTIVATED account deactivated")
    assert s2 == "dead"
