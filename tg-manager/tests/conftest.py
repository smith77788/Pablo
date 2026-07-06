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


class _AnyMeta(type):
    """Метакласс: атрибут, запрошенный на самом классе _Any (не на экземпляре),
    тоже отдаёт заглушку — иначе `SomeStubbedClass.some_attr` (частый паттерн
    monkey-patch / F.field / isinstance-констант в aiogram) падает с
    AttributeError, потому что обычный __getattr__ класса не перехватывает
    доступ к атрибутам самого класса."""

    def __getattr__(cls, name):
        # Дандеры (__signature__, __wrapped__, __mro_entries__, ...) обязаны
        # реально отсутствовать — иначе inspect/functools/pickle получают
        # мусорный объект вместо AttributeError и падают своей собственной
        # TypeError глубоко внутри stdlib (см. inspect.signature()).
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        return _Any()


class _Any(metaclass=_AnyMeta):
    """Заглушка любого telethon/aiogram-класса или функции."""

    def __init__(self, *a, **k):
        pass

    def __call__(self, *a, **k):
        return self

    def __getattr__(self, name):
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        return _Any()

    def __or__(self, other):
        return _Any()

    def __ror__(self, other):
        return _Any()

    def __and__(self, other):
        return _Any()

    def __rand__(self, other):
        return _Any()

    def __invert__(self):
        return _Any()

    def __eq__(self, other):
        return _Any()

    def __ne__(self, other):
        return _Any()

    __hash__ = object.__hash__


import importlib.abc
import importlib.util


class _TelethonFinder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """Meta-path finder (современный протокол find_spec/exec_module — работает на
    Python 3.11 И 3.12+, где легаси find_module/load_module удалён).

    Любой импорт telethon.* отдаёт лёгкую заглушку, поэтому тесты не зависят от
    полного набора подмодулей telethon (network, tl.*, …).
    """

    def find_spec(self, fullname, path=None, target=None):
        if fullname == "telethon" or fullname.startswith("telethon."):
            return importlib.util.spec_from_loader(fullname, self)
        return None

    def create_module(self, spec):
        m = _stub(spec.name)
        m.__getattr__ = lambda _n: _Any()  # type: ignore[attr-defined]
        m.TelegramClient = _Any  # type: ignore[attr-defined]
        m.StringSession = _Any  # type: ignore[attr-defined]
        return m

    def exec_module(self, module):
        return None


def _install_telethon_stubs() -> None:
    if "telethon" in sys.modules:
        return
    sys.meta_path.insert(0, _TelethonFinder())


_install_telethon_stubs()

# asyncpg C extension (protocol.protocol) не компилируется на Python 3.13.
# Стабим protocol-подмодуль чтобы import asyncpg проходил.
try:
    import asyncpg
    import asyncpg.protocol  # noqa: F401
except (ImportError, ModuleNotFoundError):
    _asyncpg = types.ModuleType("asyncpg")
    _asyncpg.__path__ = []
    _asyncpg.__version__ = "0.0.0-stub"
    _asyncpg.Pool = _Any
    _asyncpg.Record = dict
    _asyncpg.Connection = _Any
    _asyncpg.create_pool = _Any
    _asyncpg.connect = _Any
    sys.modules.setdefault("asyncpg", _asyncpg)
    _asyncpg_proto = types.ModuleType("asyncpg.protocol")
    _asyncpg_proto.__path__ = []
    _asyncpg_proto.Protocol = _Any
    _asyncpg_proto.NO_TIMEOUT = None
    _asyncpg_proto.BUILTIN_TYPE_NAME_MAP = {}
    sys.modules["asyncpg.protocol"] = _asyncpg_proto
    _asyncpg_proto_mod = types.ModuleType("asyncpg.protocol.protocol")
    _asyncpg_proto_mod.Protocol = _Any
    _asyncpg_proto_mod.NO_TIMEOUT = None
    _asyncpg_proto_mod.BUILTIN_TYPE_NAME_MAP = {}
    sys.modules["asyncpg.protocol.protocol"] = _asyncpg_proto_mod

# aiohttp / aiogram не установлены на Python 3.13 — стабим для импорта сервисов
_STUB_MODULES = {
    "aiohttp": {"ClientSession": _Any, "ClientTimeout": _Any, "TCPConnector": _Any, "ClientError": Exception},
    "aiohttp.web": {"Response": _Any, "Request": _Any, "Application": _Any},
    "aiohttp_socks": {},
    "aiogram": {"Bot": _Any, "Dispatcher": _Any, "Router": _Any, "F": _Any(), "BaseMiddleware": _Any},
    "aiogram.client": {"default": _Any},
    "aiogram.client.default": {"DefaultBotProperties": _Any},
    "aiogram.client.session": {},
    "aiogram.client.session.aiohttp": {"AiohttpSession": _Any},
    "aiogram.enums": {"ParseMode": _Any},
    "aiogram.filters": {"Command": _Any, "CommandStart": _Any, "StateFilter": _Any},
    "aiogram.filters.callback_data": {"CallbackData": type("CallbackData", (), {
        "__init_subclass__": lambda cls, **kw: None,
        "__init__": lambda self, **kw: self.__dict__.update(kw),
        "pack": lambda self: "",
        "unpack": classmethod(lambda cls, d: cls()),
        "filter": classmethod(lambda cls, *a, **kw: lambda c: True),
    })},
    "aiogram.filters.state": {"State": _Any, "StatesGroup": _Any},
    "aiogram.fsm.context": {"FSMContext": _Any},
    "aiogram.fsm.state": {"State": _Any, "StatesGroup": _Any},
    "aiogram.fsm.storage.base": {"BaseStorage": _Any, "StorageKey": _Any, "StateType": _Any},
    "aiogram.fsm.storage.memory": {"MemoryStorage": _Any},
    "aiogram.types": {
        "Message": _Any, "CallbackQuery": _Any, "InlineKeyboardButton": _Any, "InlineKeyboardMarkup": _Any,
        "KeyboardButton": _Any, "ReplyKeyboardMarkup": _Any, "BufferedInputFile": _Any, "ErrorEvent": _Any,
        "WebAppInfo": _Any, "PhotoSize": _Any, "TelegramObject": _Any,
        "MessageOriginChannel": _Any, "MessageOriginChat": _Any, "MessageOriginHiddenUser": _Any, "MessageOriginUser": _Any,
    },
    "aiogram.utils.keyboard": {"InlineKeyboardBuilder": _Any, "ReplyKeyboardBuilder": _Any},
}

for mod_name, attrs in _STUB_MODULES.items():
    if mod_name not in sys.modules:
        try:
            __import__(mod_name)
        except ImportError:
            m = types.ModuleType(mod_name)
            m.__path__ = []
            for k, v in attrs.items():
                setattr(m, k, v)
            sys.modules[mod_name] = m
            # Stub sub-modules
            parts = mod_name.split(".")
            for i in range(1, len(parts)):
                parent = ".".join(parts[:i])
                child = ".".join(parts[:i+1])
                if parent in sys.modules and child not in sys.modules:
                    cm = types.ModuleType(child)
                    cm.__path__ = []
                    sys.modules[child] = cm

# Termux site-packages для Python 3.13 (пакеты установлены через pip в termux)
_TERMUX_SITE = "/data/data/com.termux/files/usr/lib/python3.13/site-packages"
if os.path.isdir(_TERMUX_SITE) and _TERMUX_SITE not in sys.path:
    sys.path.insert(0, _TERMUX_SITE)

# Проект-корень в path (tests/ лежит внутри tg-manager/)
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
