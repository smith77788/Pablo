"""Фича (роадмап P1): «Применить SEO-предложение» — замыкает петлю
анализ→рекомендация→ПРИМЕНЕНИЕ. Раньше seo_ai_suggestions (title/about/username
на канал) только показывались; применять надо было вручную.

Эндпоинт переиспследует существующее: seo_ai_suggestions (хранимая
рекомендация), managed_channels.acc_id → управляющий аккаунт,
account_manager.edit_channel_title/about + set_channel_username.
"""
from __future__ import annotations

import inspect
import re

from services import mini_app_api


def _handler_src() -> str:
    src = inspect.getsource(mini_app_api)
    m = re.search(r"async def seo_apply\(.*?\n(.*?)\n    async def ", src, re.DOTALL)
    assert m, "seo_apply handler not found"
    return m.group(1)


def test_seo_apply_route_registered():
    src = inspect.getsource(mini_app_api)
    assert re.search(
        r'add_post\(\s*["\']/api/miniapp/seo/apply["\']\s*,\s*seo_apply', src
    ), "маршрут POST /api/miniapp/seo/apply должен быть зарегистрирован"


def test_seo_apply_scoped_by_owner():
    body = _handler_src()
    # предложение и канал читаются со скоупом по owner (uid=$1)
    assert "seo_ai_suggestions" in body and "owner_id=$1 AND chan_id=$2" in body, (
        "seo_apply должен читать предложение канала со скоупом по владельцу"
    )
    assert "FROM managed_channels WHERE owner_id=$1 AND channel_id=$2" in body, (
        "управляющий аккаунт канала должен резолвиться со скоупом по владельцу"
    )


def test_seo_apply_reuses_existing_edit_functions():
    body = _handler_src()
    for fn in ("edit_channel_title", "edit_channel_about", "set_channel_username"):
        assert fn in body, f"seo_apply должен переиспользовать account_manager.{fn}"


def test_seo_apply_has_timeout():
    body = _handler_src()
    assert "asyncio.wait_for" in body and "timeout=45" in body, (
        "инлайн-правки канала должны иметь таймаут (иначе зависший коннект → 502)"
    )


def test_seo_apply_functions_exist_in_account_manager():
    from services import account_manager
    for fn in ("edit_channel_title", "edit_channel_about", "set_channel_username"):
        assert hasattr(account_manager, fn), f"account_manager.{fn} должен существовать"
