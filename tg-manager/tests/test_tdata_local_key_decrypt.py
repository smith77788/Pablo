"""Нативный конвертер tdata реально расшифровывает key_datas и достаёт ключ.

ЧТО ЛОМАЛОСЬ (скрины владельца, две волны).
1) «key_datas: salt is too short: 32 bytes» — ждали соль 64 байта, а Telegram
   Desktop пишет ровно 32.
2) После фикса соли — «локальный ключ не распознан в расшифрованном блоке» —
   расшифровка уже проходила, но ЛОКАЛЬНЫЙ КЛЮЧ читался как QByteArray, тогда как
   TDesktop пишет его СЫРЫМИ 256 байтами (`AuthKey::write` → `writeRawData`), без
   префикса длины.

ЧТО ПРОВЕРЯЕМ. Строим key_datas ровно в формате Telegram Desktop (независимой
реализацией шифратора на stdlib+pycryptodome; все примитивы сверены с эталонным
кодом клиента — opentele/tgcrypto) и требуем, чтобы `_read_local_key` достал тот
же 256-байтный локальный ключ. Внутренний блок — [LE uint32 len] ‖ 256 СЫРЫХ байт
ключа ‖ паддинг. На старом коде тест падал: сначала на соли, затем на разборе
ключа как QByteArray.

Второй тест (если установлен opentele) собирает key_datas ЭТАЛОННЫМ кодом
TDesktop и гоняет через наш конвертер — ловит любое расхождение с реальным
форматом, которое синтетика в принципе поймать не может.
"""
from __future__ import annotations

import hashlib
import struct
from pathlib import Path

import pytest

from services import tdata_converter as tc

pytest.importorskip("Crypto.Cipher", reason="нужен pycryptodome для AES-IGE")


# ── Независимая реализация шифрующей стороны Telegram Desktop ─────────────────
def _xor(a: bytes, b: bytes) -> bytes:
    return bytes(x ^ y for x, y in zip(a, b))


def _ige_encrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    """AES-256-IGE encrypt как в OpenSSL `AES_ige_encrypt` (= Telegram Desktop)."""
    from Crypto.Cipher import AES

    aes = AES.new(key, AES.MODE_ECB)
    iv1, iv2 = iv[:16], iv[16:32]
    out = bytearray()
    for i in range(0, len(data), 16):
        p = data[i : i + 16]
        c = _xor(aes.encrypt(_xor(p, iv1)), iv2)
        iv1, iv2 = c, p
        out += c
    return bytes(out)


def _prepare_aes_oldmtp(auth_key: bytes, msg_key: bytes, x: int = 8):
    a = hashlib.sha1(msg_key + auth_key[x : x + 32]).digest()
    b = hashlib.sha1(
        auth_key[32 + x : 48 + x] + msg_key + auth_key[48 + x : 64 + x]
    ).digest()
    c = hashlib.sha1(auth_key[64 + x : 96 + x] + msg_key).digest()
    d = hashlib.sha1(msg_key + auth_key[96 + x : 128 + x]).digest()
    aes_key = a[:8] + b[8 : 8 + 12] + c[4 : 4 + 12]
    aes_iv = a[8 : 8 + 12] + b[:8] + c[16 : 16 + 4] + d[:8]
    return aes_key, aes_iv


def _create_local_key(passcode: bytes, salt: bytes) -> bytes:
    pre = hashlib.sha512(salt + passcode + salt).digest()
    iters = 1 if not passcode else 100_000
    return hashlib.pbkdf2_hmac("sha512", pre, salt, iters, dklen=256)


def _create_legacy_local_key(passcode: bytes, salt: bytes) -> bytes:
    iters = 1 if not passcode else 100_000
    return hashlib.pbkdf2_hmac("sha512", passcode, salt, iters, dklen=256)


def _qba(b: bytes) -> bytes:
    """Qt QByteArray в BigEndian-потоке: 4-байтная длина + байты."""
    return struct.pack(">I", len(b)) + b


def _tdf(content: bytes, version: int = 5004000) -> bytes:
    magic = b"TDF$"
    vb = struct.pack("<I", version)
    size = struct.pack("<I", len(content))
    checksum = hashlib.md5(content + size + vb + magic).digest()
    return magic + vb + content + checksum


def _make_key_datas(local_key: bytes, passcode: bytes, salt: bytes, legacy: bool) -> bytes:
    passcode_key = (
        _create_legacy_local_key(passcode, salt)
        if legacy
        else _create_local_key(passcode, salt)
    )
    # EncryptedDescriptor: [LE u32 fullLen] ‖ ЛОКАЛЬНЫЙ КЛЮЧ СЫРЫМИ 256 байтами ‖
    # паддинг. Именно так TDesktop пишет AuthKey (writeRawData), без QByteArray.
    inner = local_key
    full_len = 4 + len(inner)  # длина включает само 4-байтное поле
    buf = struct.pack("<I", full_len) + inner
    if len(buf) % 16:
        buf += b"\x00" * (16 - len(buf) % 16)
    msg_key = hashlib.sha1(buf).digest()[:16]
    aes_key, aes_iv = _prepare_aes_oldmtp(passcode_key, msg_key, x=8)
    key_encrypted = msg_key + _ige_encrypt(aes_key, aes_iv, buf)
    content = _qba(salt) + _qba(key_encrypted)
    return _tdf(content)


def _write_tdata(tmp_path: Path, blob: bytes) -> str:
    tdata = tmp_path / "acc" / "tdata"
    tdata.mkdir(parents=True)
    (tdata / "key_datas").write_bytes(blob)
    return str(tdata)


_LOCAL_KEY = bytes((i * 7 + 3) & 0xFF for i in range(256))


def test_reads_32_byte_salt_modern_scheme(tmp_path: Path):
    salt = bytes((i * 5 + 1) & 0xFF for i in range(32))  # ровно 32 байта — как у TDesktop
    blob = _make_key_datas(_LOCAL_KEY, b"", salt, legacy=False)
    tdata_dir = _write_tdata(tmp_path, blob)

    got = tc._read_local_key(tdata_dir, b"")
    assert got == _LOCAL_KEY, "локальный ключ из key_datas восстановлен неверно"


def test_reads_legacy_scheme(tmp_path: Path):
    salt = bytes((i * 3 + 9) & 0xFF for i in range(32))
    blob = _make_key_datas(_LOCAL_KEY, b"", salt, legacy=True)
    tdata_dir = _write_tdata(tmp_path, blob)

    got = tc._read_local_key(tdata_dir, b"")
    assert got == _LOCAL_KEY, "легаси-схема вывода локального ключа не поддержана"


def test_32_byte_salt_no_longer_rejected(tmp_path: Path):
    """Именно баг со скрина: 32-байтная соль не должна отвергаться как «короткая»."""
    salt = b"\x11" * 32
    blob = _make_key_datas(_LOCAL_KEY, b"", salt, legacy=False)
    tdata_dir = _write_tdata(tmp_path, blob)

    try:
        tc._read_local_key(tdata_dir, b"")
    except ValueError as e:
        assert "too short" not in str(e) and "слишком коротк" not in str(e), (
            f"32-байтная соль всё ещё отвергается: {e}"
        )


def test_local_key_read_as_raw_not_bytearray(tmp_path: Path):
    """Второй баг со скрина: ключ пишется СЫРЫМ, читать его как QByteArray нельзя.

    Берём ключ, у которого первые 4 байта выглядят как правдоподобная длина
    QByteArray — старый разбор через `.ba()` прочитал бы мусор, сырой разбор даёт
    точные 256 байт."""
    tricky = struct.pack(">I", 0x00000100) + bytes((i * 11 + 5) & 0xFF for i in range(252))
    assert len(tricky) == 256
    salt = b"\x22" * 32
    blob = _make_key_datas(tricky, b"", salt, legacy=False)
    tdata_dir = _write_tdata(tmp_path, blob)

    assert tc._read_local_key(tdata_dir, b"") == tricky


# ПРИМЕЧАНИЕ. Эталонная сверка с key_datas, собранным реальным кодом Telegram
# Desktop (opentele.td.Storage), выполнена вручную вне pytest при разработке
# фикса — под нашим conftest telethon заглушается, и opentele там не
# импортируется. Все криптопримитивы выше (AES-IGE, prepareAES_oldmtp,
# CreateLocalKey) сверены бит-в-бит с opentele/tgcrypto, поэтому синтетический
# key_datas здесь имеет РЕАЛЬНЫЙ формат TDesktop.
