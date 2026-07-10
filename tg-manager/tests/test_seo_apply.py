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


def _helper_src() -> str:
    from services import seo_apply as m
    return inspect.getsource(m.apply_seo_to_channel)


def test_seo_apply_scoped_by_owner():
    # логика уехала в общий хелпер (DRY) — проверяем его скоуп по owner
    body = _helper_src()
    assert "seo_ai_suggestions" in body and "owner_id=$1 AND chan_id=$2" in body, (
        "apply_seo_to_channel должен читать предложение со скоупом по владельцу"
    )
    assert "FROM managed_channels WHERE owner_id=$1 AND channel_id=$2" in body, (
        "управляющий аккаунт канала должен резолвиться со скоупом по владельцу"
    )


def test_seo_apply_reuses_existing_edit_functions():
    body = _helper_src()
    for fn in ("edit_channel_title", "edit_channel_about", "set_channel_username"):
        assert fn in body, f"apply_seo_to_channel должен переиспользовать account_manager.{fn}"


def test_seo_apply_has_timeout():
    body = _helper_src()
    assert "asyncio.wait_for" in body and "timeout=" in body, (
        "правки канала должны иметь таймаут (иначе зависший коннект → 502)"
    )


def test_seo_apply_functions_exist_in_account_manager():
    from services import account_manager
    for fn in ("edit_channel_title", "edit_channel_about", "set_channel_username"):
        assert hasattr(account_manager, fn), f"account_manager.{fn} должен существовать"


def test_shared_helper_exists_and_endpoint_reuses_it():
    """DRY: одиночный эндпоинт и bulk-исполнитель используют ОДНУ реализацию."""
    from services import seo_apply as seo_apply_mod
    assert hasattr(seo_apply_mod, "apply_seo_to_channel"), (
        "общий хелпер services.seo_apply.apply_seo_to_channel должен существовать"
    )
    body = _handler_src()
    assert "apply_seo_to_channel" in body, (
        "эндпоинт seo_apply должен переиспользовать общий хелпер (без дублей)"
    )


def test_seo_apply_all_route_registered():
    src = inspect.getsource(mini_app_api)
    assert re.search(
        r'add_post\(\s*["\']/api/miniapp/seo/apply_all["\']\s*,\s*seo_apply_all', src
    ), "маршрут POST /api/miniapp/seo/apply_all должен быть зарегистрирован"


def test_bulk_seo_apply_registered_and_dispatched():
    # op_type должен быть в реестре И обрабатываться воркером (иначе зависнет pending)
    from services import operation_bus, op_worker
    assert "bulk_seo_apply" in operation_bus.OP_REGISTRY, (
        "bulk_seo_apply должен быть в OP_REGISTRY"
    )
    wsrc = inspect.getsource(op_worker)
    assert 'op_type == "bulk_seo_apply"' in wsrc, (
        "op_worker должен диспетчеризовать bulk_seo_apply"
    )
    assert "async def _exec_bulk_seo_apply" in wsrc


def test_bulk_seo_executor_reuses_shared_helper():
    from services import op_worker
    src = inspect.getsource(op_worker)
    m = re.search(r"async def _exec_bulk_seo_apply\(.*?\n(.*?)\n\nasync def ", src, re.DOTALL)
    assert m, "_exec_bulk_seo_apply not found"
    assert "apply_seo_to_channel" in m.group(1), (
        "bulk-исполнитель должен переиспользовать общий хелпер (без дублей)"
    )
