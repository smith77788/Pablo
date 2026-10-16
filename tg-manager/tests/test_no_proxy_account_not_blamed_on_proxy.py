"""Регресс: аккаунт, которому прокси НЕ назначали, не должен получать в итоге
операции ложное «Прокси недоступен — исправьте прокси».

Баг (со скринов пользователя, Операция #117 bulk_join): у аккаунтов прокси не
назначены (должны идти напрямую по политике allow_direct). При сбое подключения
`join_channel` возвращает `proxy_error=True` (ловит OSError/ConnectionError), а
op_worker в bound-режиме (дефолт) писал ВСЕМ «❌ Прокси недоступен — смена IP
ломает auth key. Исправьте прокси» и изолировал аккаунт. Для аккаунта без
назначенного прокси это ложь: смена IP ему auth key не ломает (нет привязки к
IP), а совет «исправьте прокси» уводит чинить несуществующую проблему.

Фикс: причину выбираем по факту привязки аккаунта к прокси (_acc_has_bound_proxy).
Без фикса user-текст для безпроксёвого аккаунта содержал «Исправьте прокси» —
тест на это и падает.
"""
from __future__ import annotations

from services.op_worker import _acc_has_bound_proxy, _proxy_skip_reason


def test_has_bound_proxy_detection():
    assert _acc_has_bound_proxy({"proxy_id": 7}) is True
    assert _acc_has_bound_proxy({"proxy_url": "socks5://1.2.3.4:1080"}) is True
    assert _acc_has_bound_proxy({"proxy_id": 7, "proxy_url": None}) is True  # назначен, но неактивен — всё равно привязан к IP
    assert _acc_has_bound_proxy({}) is False
    assert _acc_has_bound_proxy({"proxy_id": None, "proxy_url": ""}) is False
    assert _acc_has_bound_proxy({"proxy_url": "   "}) is False


def test_no_proxy_account_gets_honest_reason_not_fix_proxy():
    _log, user = _proxy_skip_reason({"proxy_id": None, "proxy_url": None})
    assert "Исправьте прокси" not in user, (
        "аккаунту без назначенного прокси нельзя советовать «исправьте прокси»"
    )
    assert "auth key" not in user.lower(), (
        "нет привязки к IP — «смена IP ломает auth key» к нему не относится"
    )
    # Честная причина — про сессию/сеть.
    assert ("сесси" in user.lower()) or ("сет" in user.lower())


def test_bound_proxy_account_still_told_to_fix_proxy():
    # Для реально привязанного к прокси аккаунта сохраняем прежнюю верную причину.
    _log, user = _proxy_skip_reason({"proxy_id": 5})
    assert "прокси" in user.lower()
    assert "auth key" in user.lower()
