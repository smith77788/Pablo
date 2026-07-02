"""Тестовый каркас: стабы тяжёлых зависимостей + окружение.

Позволяет тестировать чистую логику (настройки, классификаторы, хелперы) без
реального Postgres/Telethon. Тяжёлые модули (telethon, Crypto опционально)
подменяются лёгкими заглушками, чтобы импорт сервисов не падал.
"""
from __future__ import annotations

import os
import sys
import types

# ── Окружение по умолчанию (не секреты) ──────────────────────────────────────
os.environ.setdefault("MANAGER_BOT_TOKEN", "1:test")
os.environ.setdefault("DATABASE_URL", "postgres://test")
os.environ.setdefault("TG_API_ID", "1")
os.environ.setdefault("TG_API_HASH", "x")
os.environ.setdefault("TOKEN_ENCRYPTION_KEY", "test-encryption-secret")


def _stub(name: str) -> types.ModuleType:
    mod = types.ModuleType(name)
    mod.__path__ = []  # type: ignore[attr-defined]
    return mod


class _Any:
    """Заглушка любого telethon-класса/функции."""

    def __init__(self, *a, **k):
        pass

    def __call__(self, *a, **k):
        return self

    def __getattr__(self, _n):
        return _Any()


class _TelethonFinder:
    """Meta-path finder: любой импорт telethon.* отдаёт лёгкую заглушку.

    Так тесты не зависят от полного набора подмодулей telethon (network, tl.*, …).
    """

    def find_module(self, fullname, path=None):
        if fullname == "telethon" or fullname.startswith("telethon."):
            return self
        return None

    def load_module(self, fullname):
        if fullname in sys.modules:
            return sys.modules[fullname]
        m = _stub(fullname)
        m.__getattr__ = lambda _n: _Any()  # type: ignore[attr-defined]
        m.TelegramClient = _Any  # type: ignore[attr-defined]
        m.StringSession = _Any  # type: ignore[attr-defined]
        sys.modules[fullname] = m
        return m


def _install_telethon_stubs() -> None:
    if "telethon" in sys.modules:
        return
    sys.meta_path.insert(0, _TelethonFinder())
    import telethon  # noqa: F401  (форсируем создание корня через finder)


_install_telethon_stubs()

# Проект-корень в path (tests/ лежит внутри tg-manager/)
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
