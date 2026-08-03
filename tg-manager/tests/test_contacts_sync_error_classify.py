"""Регресс: на экране «Синхронизация контактов — детали по аккаунтам» по каждому
аккаунту утекал СЫРОЙ английский текст исключения Telethon
(AuthKeyUnregisteredError: "The server claims it doesn't know about the
authorization key…"). sync_account ловил только AUTH_KEY_DUPLICATED, остальное
падало в `return {'error': emsg[:200]}`.

classify_session_error переводит все ключевые ошибки сессии в понятную русскую
причину и возвращает статус для acc_status. 'expired' (ключ не признан) НЕ
деактивирует аккаунт жёстко — при системном сбое (релей не на тот DC) все
аккаунты падают одинаково, глушить их все нельзя.
"""
from __future__ import annotations

import os

from services.contacts_hub.sync_service import classify_session_error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_screenshot_authkey_unregistered_is_translated():
    # дословный текст с экрана
    msg = ("The server claims it doesn't know about the authorization key "
           "(session file) currently being used. This might be because it "
           "either has never seen this authorization key")
    friendly, status = classify_session_error(msg)
    assert status == "expired"
    # никакого сырого английского не утекает пользователю
    low = friendly.lower()
    assert "server" not in low and "authorization key" not in low
    assert "релог" in low or "недействительна" in low


def test_deactivated_is_dead():
    # Только НЕОБРАТИМАЯ смерть (аккаунт удалён/деактивирован Telegram) = dead.
    _f, status = classify_session_error("The user has been deactivated")
    assert status == "dead"


def test_duplicated_is_transient_conflict_not_dead():
    """РАНЬШЕ AUTH_KEY_DUPLICATED = 'dead' → аккаунт деактивировался. Это неверно:
    конфликт двух IP временный (та же сессия секунду шла с двух адресов — аккаунт
    кратко онлайн на телефоне/другом устройстве или флап прокси). Многосессионность
    у Telegram штатная; телефон сам по себе не конфликтует. Теперь это 'net' —
    аккаунт НЕ деактивируется, только остужается и повторяет."""
    for msg in ("AUTH_KEY_DUPLICATED (406): used from two different IP",
                "The authorization key was used under two different IP addresses simultaneously"):
        _f, status = classify_session_error(msg)
        assert status == "net", msg
        assert status != "dead", msg


def test_transient_errors_not_dead():
    f1, s1 = classify_session_error("A wait of 300 seconds is required (FloodWait)")
    assert s1 == "flood"
    f2, s2 = classify_session_error("proxy connect timeout")
    assert s2 == "net"
    # ничего не деактивируем на транзиентных
    assert s1 != "dead" and s2 != "dead"


def test_expired_marks_status_but_not_deactivate_in_sync():
    """На 'expired' ставится acc_status='session_expired', но is_active НЕ гасится
    (в отличие от 'dead').

    Пометка перенесена из sync_account в sync_all_accounts: решение метить аккаунт
    принимается по КАРТИНЕ флота (см. `systemic`) — при системном сбое транспорта
    все аккаунты падают одинаково, и глушить их все нельзя. Поэтому пометка живёт
    в ветке `if not systemic:` общего прогона, а не в одиночном sync_account.
    """
    src_path = os.path.join(ROOT, "services/contacts_hub/sync_service.py")
    with open(src_path, encoding="utf-8") as f:
        src = f.read()
    # sync_account на ошибке НЕ метит аккаунт сам — только классифицирует и отдаёт статус.
    acc = src[src.index("async def sync_account"):src.index("async def sync_all_accounts")]
    assert "friendly, status = classify_session_error" in acc
    assert "is_active=FALSE" not in acc, "sync_account не должен деактивировать в одиночку"

    # Пометка — в sync_all_accounts, только когда сбой НЕ системный.
    allsrc = src[src.index("async def sync_all_accounts"):]
    guard = allsrc[allsrc.index("if not systemic:"):]
    assert "if st == 'dead':" in guard and "is_active=FALSE" in guard, \
        "dead-ветка должна гасить is_active в общем прогоне"
    exp = guard[guard.index("elif st == 'expired':"):]
    # expired-ветка не содержит деактивации (is_active=FALSE — только в dead)
    assert "is_active=FALSE" not in exp
    assert "acc_status='session_expired'" in exp
