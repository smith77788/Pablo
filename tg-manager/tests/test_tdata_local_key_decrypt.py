"""Нативный конвертер tdata реально расшифровывает key_datas, а не падает на соли.

ЧТО ЛОМАЛОСЬ (скрин владельца). Импорт аккаунта в формате tdata падал с ошибкой
«key_datas: salt is too short: 32 bytes». Нативный конвертер (запасной путь, когда
opentele не справился) ждал соль 64 байта, а Telegram Desktop пишет ровно 32
(`LocalEncryptSaltSize = 32`) — валидный файл отвергался ещё ДО расшифровки. Даже
если бы гейт убрали, расшифровка всё равно не сходилась: ключ выводился не по той
схеме (не `CreateLocalKey`), AES-IGE путал половины IV, а `prepareAES` брал не те
срезы ключа. То есть нативный путь не давал рабочую сессию в принципе.

ЧТО ПРОВЕРЯЕМ. Строим НАСТОЯЩИЙ key_datas ровно так, как его пишет Telegram
Desktop (здесь, независимой реализацией шифратора на stdlib+pycryptodome), и
требуем, чтобы `_read_local_key` достал обратно тот же 256-байтный локальный ключ —
для современной (PBKDF2-HMAC-SHA512) и легаси (PBKDF2-HMAC-SHA1) схем. На старом
коде тест падает на «salt is too short: 32 bytes».
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
    aes_key = a[0:8] + b[8:20] + c[4:16]
    aes_iv = a[8:20] + b[0:8] + c[16:20] + d[0:8]
    return aes_key, aes_iv


def _create_local_key_modern(passcode: bytes, salt: bytes) -> bytes:
    pre = hashlib.sha512(salt + passcode + salt).digest()
    iters = 1 if not passcode else 100_000
    return hashlib.pbkdf2_hmac("sha512", pre, salt, iters, dklen=256)


def _create_local_key_legacy(passcode: bytes, salt: bytes) -> bytes:
    iters = 4 if not passcode else 4000
    return hashlib.pbkdf2_hmac("sha1", passcode, salt, iters, dklen=256)


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
        _create_local_key_legacy(passcode, salt)
        if legacy
        else _create_local_key_modern(passcode, salt)
    )
    # EncryptedDescriptor: [LE u32 fullLen] ‖ QByteArray(local_key) ‖ паддинг.
    inner = _qba(local_key)
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


def test_reads_legacy_pbkdf2_sha1_scheme(tmp_path: Path):
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
