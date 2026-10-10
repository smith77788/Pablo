"""Регрессия: mini-app эндпоинты НЕ должны возвращать 502/503 для бизнес-ошибок.

Фронт (`api()`) показывает «Сервис временно недоступен — попробуйте позже» для
любого ответа 502/503/520+. Поэтому бизнес-ошибка с таким статусом (напр.
account_check_restriction возвращал 502 при мёртвой сессии) выглядит как «весь
сервис лежит» — пользователь не видит реальную причину и думает, что ничего не
исполняется. Бизнес-ошибки → 4xx.

Плюс: инлайн-эндпоинты, коннектящиеся к Telegram в обработчике, должны иметь
таймаут (иначе зависание → edge 502/520 для ВСЕХ).
"""
from __future__ import annotations

import inspect
import re

from services import mini_app_api


def test_no_gateway_status_for_business_errors():
    src = inspect.getsource(mini_app_api)
    offenders = re.findall(r"_err\([^\n]*,\s*(50[23]|52\d)\)", src)
    assert not offenders, (
        f"_err(..., {set(offenders)}) — бизнес-ошибка с gateway-статусом → фронт "
        "покажет «Сервис временно недоступен» вместо реальной причины. Используйте 4xx."
    )


def test_inline_account_telethon_has_timeout():
    src = inspect.getsource(mini_app_api)
    for fn in ("account_check_restriction", "account_login_code"):
        m = re.search(rf"async def {fn}\(.*?\n(.*?)(?=\n    async def |\Z)", src, re.DOTALL)
        assert m, f"{fn} не найден"
        body = m.group(1)
        assert "wait_for" in body, (
            f"{fn}: инлайн-коннект к Telegram без asyncio.wait_for — зависание даст 502/520"
        )
