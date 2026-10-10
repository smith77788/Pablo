"""Регрессия: глобальный поиск объясняет ошибку по-русски и не прячет флуд.

Текст ошибки из `search_public` уходит владельцу напрямую: бот печатает его
как «⚠️ {error}», мини-апп отдаёт телом ответа. Раньше оттуда уезжало
«FloodWait 300s» и сырой английский текст telethon — владелец английский не
читает.

Хуже другое: FloodWait при поиске не записывался в общий пульс здоровья
аккаунта. Для остальных подсистем аккаунт оставался «спокойным», выбор
аккаунта брал его под следующую операцию — и уводил под уже действующее
ограничение Telegram, где следующая пауза будет длиннее предыдущей.

Отдельно ловим флуд на фазе connect: он поднимается ДО внутреннего
`except FloodWaitError`, поэтому попадал в общий обработчик и не учитывался
вообще.
"""
from __future__ import annotations

import asyncio
import inspect
import re
from unittest.mock import AsyncMock, patch

import pytest

from services import global_search_engine as gse


class _FloodWaitError(Exception):
    def __init__(self, seconds: int):
        self.seconds = seconds
        super().__init__(f"A wait of {seconds} seconds is required (caused by SearchRequest)")


@pytest.fixture
def _telethon(monkeypatch):
    """Подменяет telethon-импорты внутри search_public на заглушки."""
    import sys
    import types

    mods = {}
    for name in ("telethon", "telethon.tl", "telethon.tl.functions",
                 "telethon.tl.functions.contacts", "telethon.errors"):
        mods[name] = sys.modules.get(name)

    if "telethon.errors" not in sys.modules or not hasattr(
        sys.modules.get("telethon.errors", object()), "FloodWaitError"
    ):
        tl = types.ModuleType("telethon")
        tl_tl = types.ModuleType("telethon.tl")
        tl_fn = types.ModuleType("telethon.tl.functions")
        tl_ct = types.ModuleType("telethon.tl.functions.contacts")
        tl_ct.SearchRequest = lambda **kw: ("SearchRequest", kw)
        tl_err = types.ModuleType("telethon.errors")
        tl_err.FloodWaitError = _FloodWaitError
        for name, mod in (("telethon", tl), ("telethon.tl", tl_tl),
                          ("telethon.tl.functions", tl_fn),
                          ("telethon.tl.functions.contacts", tl_ct),
                          ("telethon.errors", tl_err)):
            monkeypatch.setitem(sys.modules, name, mod)
    yield


class _RecordingPool:
    def __init__(self):
        self.calls: list[tuple] = []

    async def execute(self, sql, *args):
        self.calls.append((sql, args))
        return "OK"


def _client_that_floods(seconds: int, *, on_connect: bool = False):
    class _Client:
        connect = AsyncMock()
        disconnect = AsyncMock()

        def __call__(self, req):
            raise _FloodWaitError(seconds)

    if on_connect:
        _Client.connect = AsyncMock(side_effect=_FloodWaitError(seconds))
    return _Client()


def _run(coro):
    return asyncio.run(coro)


# Имена собственные в русском тексте — это норма, английской фразой они не
# являются. Без этого списка проверка ловит само слово «Telegram» и объявляет
# сломанным заведомо здоровое сообщение.
_PROPER_NOUNS = ("Telegram", "FloodWait", "Mini App", "SOCKS", "Infragram")


def _has_latin_words(text: str) -> bool:
    """Есть ли в тексте английские слова (а не имена собственные и коды)."""
    t = text or ""
    for word in _PROPER_NOUNS:
        t = t.replace(word, "")
    return bool(re.search(r"[A-Za-z]{4,}", t))


def _has_latin_words_self_check():
    """Проверяем измеритель на заведомо здоровом и заведомо больном примере."""
    assert not _has_latin_words("⏳ Telegram просит паузу 5 мин — отдохните.")
    assert _has_latin_words("FloodWaitError: A wait of 300 seconds is required")


_has_latin_words_self_check()


def _call_args(src: str, fn: str) -> list[str]:
    """Аргументы каждого вызова ``fn`` с учётом вложенных скобок.

    Наивное `\\(([^)]*)\\)` обрывается на первой же внутренней скобке
    (`dict(acc)`) и объявляет сломанным здоровый вызов.
    """
    out = []
    for m in re.finditer(re.escape(fn) + r"\(", src):
        i = m.end()
        depth = 1
        while i < len(src) and depth:
            if src[i] == "(":
                depth += 1
            elif src[i] == ")":
                depth -= 1
            i += 1
        out.append(src[m.end():i - 1])
    return out


# --- ошибка по-русски -----------------------------------------------------

def test_flood_error_is_russian(_telethon):
    with patch("services.account_manager._make_client",
               return_value=_client_that_floods(300)):
        res = _run(gse.search_public("s", "доставка", 10, _acc={"id": 9}))

    assert res["ok"] is False
    assert not _has_latin_words(res["error"]), (
        f"владельцу уходит английский текст: {res['error']!r}"
    )
    assert res.get("error_code") == "flood"


def test_flood_error_says_how_long_to_wait(_telethon):
    with patch("services.account_manager._make_client",
               return_value=_client_that_floods(300)):
        res = _run(gse.search_public("s", "доставка", 10, _acc={"id": 9}))

    assert res.get("flood_wait") == 300
    assert "5 мин" in res["error"] or "300" in res["error"], res["error"]


def test_timeout_error_is_russian(_telethon):
    class _Client:
        connect = AsyncMock(side_effect=asyncio.TimeoutError())
        disconnect = AsyncMock()

    with patch("services.account_manager._make_client", return_value=_Client()):
        res = _run(gse.search_public("s", "x", 10, _acc={"id": 9}))

    assert res["ok"] is False
    assert not _has_latin_words(res["error"]), res["error"]


def test_dead_session_is_explained(_telethon):
    class _Client:
        connect = AsyncMock()
        disconnect = AsyncMock()

        def __call__(self, req):
            raise RuntimeError("AUTH_KEY_UNREGISTERED")

    with patch("services.account_manager._make_client", return_value=_Client()):
        res = _run(gse.search_public("s", "x", 10, _acc={"id": 9}))

    assert res.get("error_code") == "session_expired", res
    assert not _has_latin_words(res["error"]), res["error"]


# --- флуд попадает в пульс здоровья --------------------------------------

def test_flood_is_recorded_in_account_health(_telethon):
    seen: list[tuple] = []

    async def _rec(pool, account_id, wait_seconds, action_type="default",
                   operation_id=None):
        seen.append((account_id, wait_seconds, action_type))
        return float(wait_seconds)

    with patch("services.account_manager._make_client",
               return_value=_client_that_floods(300)), \
            patch("services.flood_engine.record_flood", _rec):
        _run(gse.search_public("s", "x", 10, _acc={"id": 77}, pool=object()))

    assert seen == [(77, 300, "search")], (
        f"флуд не записан в пульс здоровья аккаунта: {seen!r}"
    )


def test_flood_on_connect_is_recorded_too(_telethon):
    """Флуд на подключении поднимается ДО внутреннего перехвата."""
    seen: list[tuple] = []

    async def _rec(pool, account_id, wait_seconds, action_type="default",
                   operation_id=None):
        seen.append((account_id, wait_seconds))
        return float(wait_seconds)

    with patch("services.account_manager._make_client",
               return_value=_client_that_floods(420, on_connect=True)), \
            patch("services.flood_engine.record_flood", _rec):
        res = _run(gse.search_public("s", "x", 10, _acc={"id": 77}, pool=object()))

    assert seen == [(77, 420)], f"флуд на подключении потерян: {seen!r}"
    assert res.get("flood_wait") == 420
    assert not _has_latin_words(res["error"]), res["error"]


def test_flood_reaches_the_database_cooldown(_telethon):
    """Кулдаун обязан лечь в tg_accounts, иначе его не увидят другие процессы."""
    pool = _RecordingPool()

    with patch("services.account_manager._make_client",
               return_value=_client_that_floods(300)):
        _run(gse.search_public("s", "x", 10, _acc={"id": 77}, pool=pool))

    sqls = " ".join(sql for sql, _ in pool.calls)
    assert "cooldown_until" in sqls, (
        "кулдаун не записан в БД — соседний процесс возьмёт аккаунт снова"
    )


def test_search_accepts_a_pool():
    sig = inspect.signature(gse.search_public)
    assert "pool" in sig.parameters, (
        "поиску нечем записать флуд в БД: он не получает пул"
    )


# --- все вызывающие передают пул -----------------------------------------

def _read(rel: str) -> str:
    import os

    with open(os.path.join(os.path.dirname(__file__), "..", rel), encoding="utf-8") as f:
        return f.read()


@pytest.mark.parametrize("rel", [
    "bot/handlers/global_search.py",
    "services/mini_app_api.py",
    "services/op_worker.py",
])
def test_callers_pass_the_pool(rel):
    src = _read(rel)
    calls = [a for a in _call_args(src, "search_public") if "def " not in a]
    assert calls, f"{rel}: вызов поиска не найден — проверка ничего не проверяет"
    for args in calls:
        assert "pool=" in args, (
            f"{rel}: вызов поиска без пула — флуд не доедет до БД: {args}"
        )


def test_no_english_flood_label_left():
    src = _read("services/global_search_engine.py")
    assert 'f"FloodWait {e.seconds}s"' not in src
