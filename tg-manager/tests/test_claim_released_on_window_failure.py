"""Захваченный аккаунт освобождается, если сбой случился в «окне» между
захватом и основным try (создание клиента / запрос в БД).

Жалоба владельца: «операция не запускается — все аккаунты заняты, хотя никаких
операций нет». Корень — НЕ операции, а фоновые модули (разбор аудитории,
прогрев чатов, активность, консоль, поиск): аккаунт захватывался
`op_worker.try_claim_account`, но `_make_client` и предшествующие запросы в БД
стояли ВНЕ того try, чей finally делает release. Исключение в этом окне (сбой
БД, битая сессия) уводило аккаунт мимо release — он залипал «занятым» в
`_accounts_in_use`, а `renew_leases` продлевал его аренду вечно; снять можно
было только рестартом. Накопление таких залипаний и давало «весь флот занят».

Эталон, по которому выправлены все модули — `account_warmer`: весь пост-захватный
путь (включая `_make_client`) под одним try с release в finally.

Проверяем ПОВЕДЕНИЕ (а не наличие обёртки): если в окне падает исключение,
аккаунт обязан освободиться.
"""
from __future__ import annotations

import asyncio

import pytest

import services.op_worker as ow


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """Одиночный процесс (без БД-арбитра) + чистый реестр захватов."""
    saved_pool = ow._db_pool
    saved_in_use = set(ow._accounts_in_use)
    ow._db_pool = None  # try_claim_account работает по локальному реестру
    ow._accounts_in_use.clear()
    try:
        yield
    finally:
        ow._db_pool = saved_pool
        ow._accounts_in_use.clear()
        ow._accounts_in_use.update(saved_in_use)


def test_parser_releases_account_when_db_create_run_fails(monkeypatch):
    """parser.parse_members: сбой _create_run (INSERT в БД) в окне захвата
    обязан освободить аккаунт, а не оставить его залипшим «занятым»."""
    from services import parser
    from services import flood_engine

    acc = {"id": 7771, "session_str": "s", "device_model": None}

    async def _best(pool, owner_id, **k):
        return dict(acc)
    monkeypatch.setattr(flood_engine, "get_best_account", _best)

    async def _boom(*a, **k):
        raise RuntimeError("БД недоступна")
    monkeypatch.setattr(parser, "_create_run", _boom)

    with pytest.raises(RuntimeError):
        asyncio.run(parser.parse_members(None, 1, "@src"))

    assert not ow.is_account_in_use(7771), (
        "аккаунт обязан освободиться при сбое в окне захвата — иначе он залипает "
        "«занятым» до рестарта процесса")


def test_parser_releases_account_when_make_client_fails(monkeypatch):
    """Тот же инвариант для сбоя _make_client (битая сессия)."""
    from services import parser
    from services import flood_engine
    from services import account_manager

    acc = {"id": 7772, "session_str": "s", "device_model": None}

    async def _best(pool, owner_id, **k):
        return dict(acc)
    monkeypatch.setattr(flood_engine, "get_best_account", _best)

    async def _run_ok(*a, **k):
        return 123  # run_id
    monkeypatch.setattr(parser, "_create_run", _run_ok)

    def _bad_client(*a, **k):
        raise RuntimeError("битая сессия")
    monkeypatch.setattr(account_manager, "_make_client", _bad_client)

    # _update_run может звать БД при неудаче — глушим, чтобы тест не зависел.
    async def _upd(*a, **k):
        return None
    monkeypatch.setattr(parser, "_update_run", _upd, raising=False)

    with pytest.raises(RuntimeError):
        asyncio.run(parser.parse_members(None, 1, "@src"))

    assert not ow.is_account_in_use(7772), (
        "аккаунт обязан освободиться при сбое _make_client в окне захвата")


def test_global_search_releases_account_when_make_client_fails(monkeypatch):
    """global_search_engine.search_public: сбой _make_client в окне захвата
    освобождает аккаунт (внутри функции _make_client импортируется из
    account_manager — патчим именно там)."""
    from services import global_search_engine as gse
    import services.account_manager as am

    def _bad_client(*a, **k):
        raise RuntimeError("битая сессия")
    monkeypatch.setattr(am, "_make_client", _bad_client, raising=False)

    acc = {"id": 7773}
    res = asyncio.run(gse.search_public("sess", "запрос", 20, acc))
    assert res.get("ok") is False  # сбой возвращается как ошибка, не как краш

    assert not ow.is_account_in_use(7773), (
        "аккаунт обязан освободиться при сбое _make_client в окне захвата")


def test_fixed_modules_make_client_inside_release_try():
    """Храповик: во всех выправленных модулях путь захвата инициализирует
    `client = None` ДО try и больше не создаёт клиента голой строкой перед ним.
    Грубая, но дешёвая страховка от повторного выноса _make_client за try."""
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for mod in ("activity_engine", "chat_warmup", "parser",
                "global_search_engine", "entity_analyzer", "account_console"):
        src = open(os.path.join(root, "services", f"{mod}.py"),
                   encoding="utf-8").read()
        assert "client = None" in src, (
            f"{mod}: нет инициализации client=None перед try — вероятен возврат "
            f"утечки захвата при сбое в окне")
