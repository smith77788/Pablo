"""Регресс: КАЖДЫЙ писатель подписки синхронизирует оба хранилища + сбрасывает кеш.

Источник правды — таблица `subscriptions` (её читают get_plan и бот). Но
`platform_users.current_plan/plan_expires_at` — второе денормализованное
хранилище, которое читают админ-списки и часть мини-аппа. Рантайм — ОДИН
процесс (`web: python main.py`; mini_app_api смонтирован внутри payment_webhook),
значит `_plan_cache` общий, и `invalidate_plan_cache` полноценно чистит его для
всех читателей.

Баг (репорт «видят подписку в боте, но не в мини-апп»): `check_and_grant_rewards`
(реф-награды) и `give_welcome_bonus` писали ТОЛЬКО subscriptions → бот подписку
видел, а platform_users оставался free → мини-апп/админка показывали free.
`grant_plan_to_user`/`revoke_plan_from_user` писали обе таблицы, но не сбрасывали
кеш → до 60с get_plan (в т.ч. в мини-аппе) отдавал старый план.

Проверяем по исходнику функций (юнит без БД): раз строка subscriptions
записана — рядом обязан быть sync platform_users и сброс кеша.
"""
from __future__ import annotations

import inspect
import re

from database import db


def _body(func_name: str) -> str:
    src = inspect.getsource(getattr(db, func_name))
    return src


def test_helpers_exist():
    assert hasattr(db, "_sync_platform_user_plan"), "нет единого хелпера синхронизации"
    assert hasattr(db, "_invalidate_plan_cache"), "нет хелпера сброса кеша"
    # хелпер синхронизации пишет platform_users и чистит кеш
    h = _body("_sync_platform_user_plan")
    assert "UPDATE platform_users SET current_plan" in h
    assert "_invalidate_plan_cache" in h
    inv = _body("_invalidate_plan_cache")
    assert "invalidate_plan_cache" in inv


def test_referral_rewards_syncs_platform_users():
    b = _body("check_and_grant_rewards")
    assert "INSERT INTO subscriptions" in b, "функция должна писать subscriptions"
    assert "_sync_platform_user_plan" in b, (
        "реф-награда обязана синхронизировать platform_users (иначе бот видит план, "
        "а мини-апп/админка — free)"
    )
    # апсерт должен возвращать plan/expires_at для синхронизации
    assert "RETURNING plan, expires_at" in b


def test_welcome_bonus_syncs_platform_users():
    b = _body("give_welcome_bonus")
    assert "INSERT INTO subscriptions" in b
    assert "_sync_platform_user_plan" in b and "RETURNING plan, expires_at" in b


def test_grant_plan_syncs_both_and_invalidates():
    b = _body("grant_plan_to_user")
    assert "INSERT INTO subscriptions" in b
    # platform_users синхронизируется от RETURNING подписки, не отдельным CASE
    assert "RETURNING plan, expires_at" in b
    assert "UPDATE platform_users SET current_plan=$2, plan_expires_at=$3" in b
    assert "_invalidate_plan_cache" in b, "grant без сброса кеша → get_plan отдаёт старое до 60с"


def test_revoke_plan_invalidates_cache():
    b = _body("revoke_plan_from_user")
    assert "UPDATE subscriptions SET is_active=false" in b
    assert "current_plan='free'" in b
    assert "_invalidate_plan_cache" in b, "revoke без сброса кеша → юзер до 60с ещё платный"


def test_no_subscriptions_writer_forgets_platform_users():
    """Свип: любая функция db, делающая INSERT INTO subscriptions, должна рядом
    синхронизировать platform_users (через хелпер или напрямую). Ловит будущие
    регрессии — новый писатель, забывший второе хранилище."""
    import database.db as m

    src = inspect.getsource(m)
    # разбиваем на функции верхнего уровня по 'async def '/'def '
    funcs = re.split(r"\n(?=async def |def )", src)
    offenders = []
    for f in funcs:
        if "INSERT INTO subscriptions" not in f:
            continue
        name = re.match(r"(?:async )?def (\w+)", f)
        name = name.group(1) if name else "?"
        syncs = (
            "_sync_platform_user_plan" in f
            or "UPDATE platform_users SET current_plan" in f
            or "platform_users" in f and "current_plan" in f
        )
        if not syncs:
            offenders.append(name)
    assert not offenders, (
        f"писатели subscriptions без синхронизации platform_users: {offenders}"
    )
