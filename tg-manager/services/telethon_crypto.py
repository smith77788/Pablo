"""Нативное шифрование транспорта Telethon вместо чистого Python.

ЗАЧЕМ. Каждый клиент флота ходит в Telegram через ConnectionTcpObfuscated
(`account_manager._make_client`: «всегда обфускация»; релей Cloudflare —
та же обфускация поверх WebSocket). Обфускация — это AES-CTR над КАЖДЫМ
байтом в обе стороны, и Telethon 1.x делает его через `pyaes`, то есть на
чистом Python, без оглядки на cryptg и libssl (`telethon/crypto/aesctr.py`:
«TODO Use libssl if available»). Замер в контейнере агента 12.11.2026:

    pyaes CTR         0.4 МБ/с
    pycryptodome CTR  905 МБ/с   (в 2250 раз быстрее, поток байт-в-байт тот же)

Все клиенты всех аккаунтов живут в ОДНОМ цикле событий вместе с ботом,
мини-аппом и исполнителем операций. 0.4 МБ/с означает: часть загрузки
медиа в 512 КБ (рассылка с картинкой, аватар, пост в канал) держит цикл
~1.3 с — всё это время не отвечает никто, а у соседних клиентов истекают
пинги. Это был потолок пропускной способности флота по трафику, и стоял он
не в Telegram и не в сети, а в одной строке зависимости.

Второе место — AES-IGE самого MTProto. Его Telethon уже умеет брать из
cryptg (400 МБ/с) или libssl через ctypes (8 МБ/с в том же замере); без
обоих — тоже чистый Python. cryptg теперь в requirements.txt, а `backends()`
говорит, что реально выбрано, — это пишется в лог на старте.

КАК. `install()` подменяет класс AESModeCTR в модулях Telethon, которые его
используют (обфускация, MTProxy, расшифровка CDN). Замена держит тот же
интерфейс и тот же поток ключа: и `encrypt`, и `decrypt` продвигают ОДИН
счётчик, как у pyaes (у CTR это одна операция). Подмена идемпотентна и
fail-open: нет pycryptodome — остаётся pyaes и громкое предупреждение,
потому что медленный транспорт лучше никакого.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# Модули Telethon, которые берут AESModeCTR по имени из своего пространства
# имён (`from ...crypto import AESModeCTR`), поэтому менять надо в каждом, а не
# только в telethon.crypto.aesctr.
_CTR_USERS = (
    "telethon.crypto.aesctr",
    "telethon.crypto",
    "telethon.network.connection.tcpobfuscated",
    "telethon.network.connection.tcpmtproxy",
    "telethon.crypto.cdndecrypter",
)

class NativeAESModeCTR:
    """AES-CTR с полным 128-битным счётчиком из IV — как pyaes в Telethon.

    Один объект шифра на поток: `decrypt` тоже идёт через `encrypt`, иначе
    pycryptodome отказывается смешивать направления, а Telethon (и pyaes)
    считает их одной лентой ключа.
    """

    __slots__ = ("_cipher",)

    def __init__(self, key: bytes, iv: bytes):
        from Crypto.Cipher import AES

        assert isinstance(key, bytes)
        assert isinstance(iv, bytes)
        assert len(iv) == 16
        self._cipher = AES.new(key, AES.MODE_CTR, nonce=b"", initial_value=iv)

    def encrypt(self, data) -> bytes:
        return self._cipher.encrypt(bytes(data))

    def decrypt(self, data) -> bytes:
        return self._cipher.encrypt(bytes(data))


def install() -> bool:
    """Подменить AES-CTR Telethon на нативный. True — подмена стоит."""
    try:
        from Crypto.Cipher import AES  # noqa: F401
    except Exception:
        log.critical(
            "pycryptodome недоступен: шифрование трафика Telegram остаётся на "
            "чистом Python (~0.4 МБ/с на весь процесс). Каждая загрузка медиа "
            "будет замораживать бота и мини-апп. Установите pycryptodome.")
        return False
    import importlib
    import sys

    for name in _CTR_USERS:
        try:
            mod = sys.modules.get(name) or importlib.import_module(name)
        except Exception:
            # Такого модуля нет в этой версии Telethon — значит, и подменять
            # в нём нечего.
            continue
        if hasattr(mod, "AESModeCTR"):
            mod.AESModeCTR = NativeAESModeCTR
    return True


def backends() -> dict:
    """Что реально шифрует трафик: для лога старта и проверки готовности."""
    out = {"ctr": "pyaes", "ige": "python"}
    try:
        from telethon.network.connection import tcpobfuscated

        if getattr(tcpobfuscated, "AESModeCTR", None) is NativeAESModeCTR:
            out["ctr"] = "pycryptodome"
    except Exception:
        out["ctr"] = "unavailable"
    try:
        from telethon.crypto import aes as _aes

        if getattr(_aes, "cryptg", None):
            out["ige"] = "cryptg"
        else:
            from telethon.crypto import libssl

            if libssl.encrypt_ige and libssl.decrypt_ige:
                out["ige"] = "libssl"
    except Exception:
        out["ige"] = "unavailable"
    return out


def install_and_report() -> dict:
    """install() + одна строка в лог о выбранных реализациях."""
    install()
    b = backends()
    if b.get("ctr") != "pycryptodome" or b.get("ige") != "cryptg":
        log.warning(
            "Шифрование Telegram: CTR=%s, IGE=%s — ожидалось pycryptodome и "
            "cryptg; медленная реализация держит цикл событий на трафике флота",
            b.get("ctr"), b.get("ige"))
    else:
        log.info("Шифрование Telegram: CTR=%s, IGE=%s", b["ctr"], b["ige"])
    return b
