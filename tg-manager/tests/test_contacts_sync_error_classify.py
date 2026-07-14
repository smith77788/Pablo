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


def test_duplicated_and_deactivated_are_dead():
    for msg in ("AUTH_KEY_DUPLICATED (406): used from two different IP",
                "The user has been deactivated"):
        _f, status = classify_session_error(msg)
        assert status == "dead", msg


def test_transient_errors_not_dead():
    f1, s1 = classify_session_error("A wait of 300 seconds is required (FloodWait)")
    assert s1 == "flood"
    f2, s2 = classify_session_error("proxy connect timeout")
    assert s2 == "net"
    # ничего не деактивируем на транзиентных
    assert s1 != "dead" and s2 != "dead"


def test_expired_marks_status_but_not_deactivate_in_sync():
    """sync_account на 'expired' ставит acc_status='session_expired', но НЕ
    трогает is_active (в отличие от 'dead')."""
    src_path = os.path.join(ROOT, "services/contacts_hub/sync_service.py")
    with open(src_path, encoding="utf-8") as f:
        src = f.read()
    seg = src[src.index("friendly, status = classify_session_error"):]
    seg = seg[:seg.index("return {'error': friendly")]
    # dead-ветка гасит is_active, expired-ветка — нет
    assert "if status == 'dead':" in seg and "is_active=FALSE" in seg
    exp = seg[seg.index("elif status == 'expired':"):]
    # expired-ветка не содержит деактивации (is_active=FALSE — только в dead)
    assert "is_active=FALSE" not in exp
    assert "acc_status='session_expired'" in exp
