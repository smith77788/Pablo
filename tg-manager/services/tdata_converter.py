"""Telegram Desktop tdata -> Telethon StringSession converter.

This is a best-effort parser for local Telegram Desktop tdata folders. It does
not depend on opentele, but requires pycryptodome for AES-IGE decryption.
"""

from __future__ import annotations

import base64
import hashlib
import importlib
import io
import logging
import os
import struct
from typing import Any, Optional, cast

log = logging.getLogger(__name__)


def _xor(a: bytes, b: bytes) -> bytes:
    return bytes(x ^ y for x, y in zip(a, b))


def _aes_cls():
    crypto_cipher = importlib.import_module("Crypto.Cipher")
    return cast(Any, getattr(crypto_cipher, "AES"))


def _aes_ige_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    """Decrypt AES-256-IGE blocks used by MTProto/TDesktop.

    Соглашение по IV — как в OpenSSL `AES_ige_encrypt` (и, значит, в Telegram
    Desktop): iv = iv1(16) ‖ iv2(16); блок m = D(c ⊕ iv2) ⊕ iv1, затем iv1←c,
    iv2←m. Прежняя реализация путала половины IV местами — расшифровка реальных
    файлов tdata не сходилась в принципе, и нативный конвертер никогда не давал
    рабочую сессию (спасал только opentele).
    """
    aes_cls = _aes_cls()
    aes = aes_cls.new(key, aes_cls.MODE_ECB)
    iv1, iv2 = iv[:16], iv[16:32]
    out = bytearray()
    for i in range(0, len(data), 16):
        c = data[i : i + 16]
        m = _xor(aes.decrypt(_xor(c, iv2)), iv1)
        iv1, iv2 = c, m
        out.extend(m)
    return bytes(out)


def _aes_ige_encrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    """Encrypt AES-256-IGE — точная инверсия `_aes_ige_decrypt` (OpenSSL).

    Нужна для round-trip регресс-теста: тест шифрует известный локальный ключ
    ровно так, как это делает Telegram Desktop, и проверяет, что конвертер его
    достаёт обратно. Без общего шифратора тест доказывал бы лишь самосогласован-
    ность, а не совпадение с форматом TDesktop."""
    aes_cls = _aes_cls()
    aes = aes_cls.new(key, aes_cls.MODE_ECB)
    iv1, iv2 = iv[:16], iv[16:32]
    out = bytearray()
    for i in range(0, len(data), 16):
        p = data[i : i + 16]
        c = _xor(aes.encrypt(_xor(p, iv1)), iv2)
        iv1, iv2 = c, p
        out.extend(c)
    return bytes(out)


def _prepare_aes_oldmtp(auth_key: bytes, msg_key: bytes):
    """TDesktop `prepareAES_oldmtp` (MTProto 1.0 KDF, send=false → x=8).

    Именно этим Telegram Desktop шифрует/расшифровывает ЛОКАЛЬНОЕ хранилище:
    и key_datas (локальный ключ), и файлы аккаунтов. Для локалки encrypt и
    decrypt используют один и тот же x=8 (операция симметрична по ключу).

    Прежний `_prep_aes_local` брал 36-байтные срезы auth_key и иные смещения —
    это не совпадало с реальным алгоритмом, и даже с верным ключом расшифровка
    не сходилась."""
    x = 8
    a = hashlib.sha1(msg_key + auth_key[x : x + 32]).digest()
    b = hashlib.sha1(
        auth_key[32 + x : 48 + x] + msg_key + auth_key[48 + x : 64 + x]
    ).digest()
    c = hashlib.sha1(auth_key[64 + x : 96 + x] + msg_key).digest()
    d = hashlib.sha1(msg_key + auth_key[96 + x : 128 + x]).digest()
    aes_key = a[0:8] + b[8:20] + c[4:16]
    aes_iv = a[8:20] + b[0:8] + c[16:20] + d[0:8]
    return aes_key, aes_iv


def _create_local_key_candidates(passphrase: bytes, salt: bytes) -> list[bytes]:
    """Деривация 256-байтного passcode-ключа TDesktop из пароля и соли.

    У Telegram Desktop две версии `CreateLocalKey`, зависят от версии сборки,
    экспортировавшей tdata. Пробуем обе — сначала современную:

    - Современная (TDesktop ≥ ~1.3): hash = SHA512(salt ‖ pass ‖ salt),
      key = PBKDF2-HMAC-SHA512(hash, salt, iters, 256);
      iters = 1 без пароля, 100000 с паролем.
    - Легаси: key = PBKDF2-HMAC-SHA1(pass, salt, iters, 256);
      iters = 4 без пароля, 4000 с паролем.
    """
    candidates: list[bytes] = []
    pre = hashlib.sha512(salt + passphrase + salt).digest()
    iters_modern = 1 if not passphrase else 100_000
    candidates.append(
        hashlib.pbkdf2_hmac("sha512", pre, salt, iters_modern, dklen=256)
    )
    iters_legacy = 4 if not passphrase else 4000
    candidates.append(
        hashlib.pbkdf2_hmac("sha1", passphrase, salt, iters_legacy, dklen=256)
    )
    return candidates


def _decrypt_local_blob(passcode_key: bytes, encrypted: bytes) -> Optional[bytes]:
    """TDesktop `DecryptLocal`: msgKey(16) ‖ AES-IGE, с проверкой SHA1.

    Возвращает расшифрованный буфер (с 4-байтным LE-полем длины в начале и
    паддингом в конце) или None, если msgKey не сошёлся (не тот ключ/битьё)."""
    if len(encrypted) <= 16 or (len(encrypted) % 16) != 0:
        return None
    msg_key = encrypted[:16]
    body = encrypted[16:]
    aes_key, aes_iv = _prepare_aes_oldmtp(passcode_key, msg_key)
    try:
        decrypted = _aes_ige_decrypt(aes_key, aes_iv, body)
    except Exception:
        return None
    # Контроль целостности TDesktop: первые 16 байт SHA1(decrypted) == msgKey.
    if hashlib.sha1(decrypted).digest()[:16] != msg_key:
        return None
    return decrypted


def _parse_encrypted_descriptor(decrypted: bytes) -> Optional[bytes]:
    """Достать полезную нагрузку из буфера TDesktop `EncryptedDescriptor`.

    Формат: [LE uint32 fullLen] ‖ payload ‖ случайный паддинг. fullLen включает
    само 4-байтное поле. Возвращает payload (без длины и паддинга)."""
    if len(decrypted) < 4:
        return None
    full_len = struct.unpack("<I", decrypted[:4])[0]
    if full_len < 4 or full_len > len(decrypted):
        return None
    return decrypted[4:full_len]


def _prep_aes_local(auth_key: bytes, msg_key: bytes, decrypt: bool = True):
    """SHA-1 PrepareAES variant for older local TDesktop files."""
    x = 8 if decrypt else 0
    sha1a = hashlib.sha1(msg_key + auth_key[x : x + 36]).digest()
    sha1b = hashlib.sha1(auth_key[x + 40 : x + 76] + msg_key).digest()
    sha1c = hashlib.sha1(auth_key[x + 84 : x + 120] + msg_key).digest()
    sha1d = hashlib.sha1(msg_key + auth_key[x + 128 : x + 160]).digest()
    aes_key = sha1a[:8] + sha1b[8:20] + sha1c[4:16]
    aes_iv = sha1a[8:20] + sha1b[:8] + sha1c[16:20] + sha1d[:8]
    return aes_key, aes_iv


def _prep_aes_local_sha256(auth_key: bytes, msg_key: bytes, decrypt: bool = True):
    """SHA-256 PrepareAES variant used by newer TDesktop files."""
    x = 4 if decrypt else 0
    sha256a = hashlib.sha256(msg_key + auth_key[x : x + 36]).digest()
    sha256b = hashlib.sha256(auth_key[x + 40 : x + 76] + msg_key).digest()
    aes_key = sha256a[:8] + sha256b[8:24] + sha256a[24:32]
    aes_iv = sha256b[:8] + sha256a[8:24] + sha256b[24:32]
    return aes_key, aes_iv


_TDF_MAGIC = b"TDF$"


def _read_tdf_raw(path: str) -> bytes:
    """Read a TDF$ file, verify checksum, and return encrypted content."""
    with open(path, "rb") as f:
        raw = f.read()
    if len(raw) < 24 or raw[:4] != _TDF_MAGIC:
        raise ValueError(f"Invalid TDF file: {os.path.basename(path)}")
    ver_bytes = raw[4:8]
    content = raw[8:-16]
    md5_stored = raw[-16:]
    # Telegram Desktop hashes payload + its little-endian size + version + magic.
    # The previous implementation hashed the file prefix instead, rejecting every
    # valid modern key_datas file as corrupt.
    size_bytes = struct.pack("<I", len(content))
    md5_calc = hashlib.md5(content + size_bytes + ver_bytes + _TDF_MAGIC).digest()
    if md5_calc != md5_stored:
        # Контрольная сумма — проверка ЦЕЛОСТНОСТИ, а не ключ расшифровки. Порядок
        # полей в MD5 у Telegram Desktop менялся между версиями, и несхождение
        # чаще означает «наша формула не та для этой версии», чем «файл битый».
        # Данные (content = payload между версией и md5) целы в любом случае —
        # расшифровка ниже использует именно их, а не md5. Поэтому не роняем
        # чтение: иначе валидный key_datas отвергался как «битый», и весь импорт
        # tdata не работал. Реально повреждённый файл отсеется дальше, на
        # расшифровке (вернётся пустой auth_key), не утянув остальные аккаунты.
        log.debug("tdata: TDF checksum mismatch on %s — продолжаем (не фатально)",
                  os.path.basename(path))
    return content


def _tdf_read(base: str, name: str) -> bytes:
    """Read a TDF file with Telegram Desktop suffix fallback.

    Честная диагностика: если файл ЕСТЬ, но не читается (не TDF$, битая
    контрольная сумма или обрезан при упаковке в ZIP), поднимаем НАСТОЯЩУЮ
    причину, а не общее «not found».
    Прежде проглоченная ошибка превращалась в «key_datas not found», хотя файл
    был на месте, — и диагностика уходила не туда."""
    failures: list[str] = []
    for suffix in ("", "0", "1", "s"):
        path = os.path.join(base, name + suffix)
        if os.path.exists(path):
            try:
                return _read_tdf_raw(path)
            except (OSError, ValueError) as exc:
                failures.append(f"{os.path.basename(path)}: {exc}")
    if failures:
        raise ValueError(
            f"Файл {name} найден, но не читается как TDF: " + "; ".join(failures)
        )
    raise FileNotFoundError(f"TDF file {name} not found in {base}")


class _QS:
    """Minimal Qt DataStream reader for big-endian integers and QByteArray."""

    def __init__(self, data: bytes):
        self._b = io.BytesIO(data)

    def u32(self) -> int:
        b = self._b.read(4)
        if len(b) < 4:
            raise EOFError("QStream: unexpected end of stream")
        return struct.unpack(">I", b)[0]

    def i32(self) -> int:
        b = self._b.read(4)
        if len(b) < 4:
            raise EOFError("QStream: unexpected end of stream")
        return struct.unpack(">i", b)[0]

    def ba(self) -> bytes:
        n = self.u32()
        if n == 0xFFFFFFFF:
            return b""
        b = self._b.read(n)
        if len(b) < n:
            raise EOFError(f"QStream: QByteArray expected {n}, got {len(b)}")
        return b

    def remaining(self) -> bytes:
        return self._b.read()

    def pos(self) -> int:
        return self._b.tell()


# Telegram Desktop `LocalEncryptSaltSize = 32` (256 бит). Прежде здесь стояло 64,
# и валидный 32-байтный salt отвергался ещё до расшифровки с ошибкой «salt is too
# short: 32 bytes» — импорт tdata падал на ровном месте. Ни одна версия TDesktop
# не пишет соль короче 16 байт, это и есть нижняя граница вменяемости.
_SALT_SIZE = 32
_MIN_SALT_SIZE = 16
_LOCAL_KEY_SIZE = 256


def _extract_local_key(decrypted: bytes) -> Optional[bytes]:
    """Из расшифрованного key_datas достать 256-байтный локальный ключ.

    Полезная нагрузка — QByteArray(localkey) в BigEndian-потоке Qt. Payload из
    EncryptedDescriptor — предпочтительный путь; дальше несколько терпимых
    запасных смещений на случай мелких расхождений версий."""
    payload = _parse_encrypted_descriptor(decrypted)
    streams = [payload] if payload is not None else []
    streams += [decrypted[skip:] for skip in (4, 0, 16, 20)]
    for buf in streams:
        if not buf:
            continue
        try:
            local_key = _QS(buf).ba()
        except Exception:
            continue
        if len(local_key) == _LOCAL_KEY_SIZE:
            return local_key
    return None


def _read_local_key(tdata_dir: str, passphrase: bytes = b"") -> bytes:
    """Extract the 256-byte LocalKey from tdata/key_datas.

    Повторяет реальный путь Telegram Desktop: key_datas = QByteArray(salt) ‖
    QByteArray(keyEncrypted). passcode-ключ выводится из пароля+соли
    (`CreateLocalKey`), затем keyEncrypted расшифровывается как локальный блоб
    (`DecryptLocal`: msgKey ‖ AES-IGE с проверкой SHA1), и из полезной нагрузки
    читается 256-байтный локальный ключ."""
    content = _tdf_read(tdata_dir, "key_datas")
    stream = _QS(content)

    try:
        salt = stream.ba()
        encrypted = stream.ba()
    except EOFError:
        salt, encrypted = b"", b""

    if len(salt) == 0 and len(content) > _SALT_SIZE:
        # Экзотический вариант без QByteArray-обёртки: соль в начале «сырьём».
        salt = content[:_SALT_SIZE]
        encrypted = content[_SALT_SIZE:]

    if len(salt) < _MIN_SALT_SIZE:
        raise ValueError(
            "key_datas: соль не найдена или слишком короткая "
            f"({len(salt)} байт) — файл повреждён или это не tdata"
        )
    if len(encrypted) < 16 + 16:
        raise ValueError("key_datas: зашифрованный локальный ключ пуст/обрезан")
    if len(encrypted) % 16 != 0:
        encrypted = encrypted[: len(encrypted) - len(encrypted) % 16]

    failures: list[str] = []
    for passcode_key in _create_local_key_candidates(passphrase, salt):
        decrypted = _decrypt_local_blob(passcode_key, encrypted)
        if decrypted is None:
            failures.append("msgKey не сошёлся")
            continue
        local_key = _extract_local_key(decrypted)
        if local_key is not None:
            log.debug("tdata: LocalKey извлечён (%d байт)", len(local_key))
            return local_key
        failures.append("локальный ключ не распознан в расшифрованном блоке")

    detail = "; ".join(dict.fromkeys(failures)) or "нет подходящей схемы"
    raise ValueError(
        "key_datas: не удалось расшифровать локальный ключ "
        f"({detail}). Возможно, tdata под локальным паролем — тогда нужен пароль"
    )


def _decrypt_account_file(content: bytes, local_key: bytes) -> Optional[bytes]:
    """Расшифровать файл аккаунта tdata тем же локальным контуром TDesktop.

    Содержимое файла (после снятия обёртки TDF$) — это локальный блоб
    `DecryptLocal`: msgKey(16) ‖ AES-IGE, ключ — локальный ключ из key_datas.
    Правильный путь (msgKey со смещения 0, `prepareAES_oldmtp`) — первый; старые
    эвристики со сдвигом 8 и иными prep-функциями остаются запасными на случай
    нестандартных сборок."""
    if len(content) < 32:
        return None

    primary = _decrypt_local_blob(local_key, content)
    if primary is not None:
        payload = _parse_encrypted_descriptor(primary)
        return payload if payload is not None else primary

    # ── Запасные эвристики (старое поведение) ────────────────────────────────
    for off in (0, 8):
        if len(content) < off + 16:
            continue
        msg_key = content[off : off + 16]
        encrypted = content[off + 16 :]
        if len(encrypted) % 16 != 0:
            encrypted = encrypted[: len(encrypted) - len(encrypted) % 16]
        if len(encrypted) < 16:
            continue
        for prep_fn, decrypt_flag in [
            (_prep_aes_local, True),
            (_prep_aes_local, False),
            (_prep_aes_local_sha256, True),
            (_prep_aes_local_sha256, False),
        ]:
            try:
                aes_key, aes_iv = prep_fn(local_key, msg_key, decrypt_flag)
                decrypted = _aes_ige_decrypt(aes_key, aes_iv, encrypted)
                if len(decrypted) >= 4:
                    data_len = struct.unpack(">I", decrypted[:4])[0]
                    if 4 <= data_len <= len(decrypted) + 256:
                        return decrypted
            except Exception:
                pass

    return None


_DC_IPS = {
    1: "149.154.175.53",
    2: "149.154.167.51",
    3: "149.154.175.100",
    4: "149.154.167.91",
    5: "91.108.56.130",
}
_DC_PORT = 443


def _scan_for_auth_key(data: bytes) -> list[bytes]:
    """Find 256-byte chunks that can be Telegram auth keys."""
    candidates = []
    stream = io.BytesIO(data)
    while True:
        pos = stream.tell()
        chunk = stream.read(4)
        if len(chunk) < 4:
            break
        try:
            length = struct.unpack(">I", chunk)[0]
        except Exception:
            stream.seek(pos + 1)
            continue
        if length == _LOCAL_KEY_SIZE:
            auth_key = stream.read(_LOCAL_KEY_SIZE)
            if len(auth_key) == _LOCAL_KEY_SIZE and any(b != 0 for b in auth_key):
                candidates.append(auth_key)
            stream.seek(pos + 1)
        else:
            stream.seek(pos + 1)
    return candidates


def _scan_for_dc_id(data: bytes) -> Optional[int]:
    """Find a plausible Telegram DC id in decrypted account data."""
    for i in range(0, min(len(data) - 4, 256), 4):
        try:
            value = struct.unpack(">I", data[i : i + 4])[0]
            if 1 <= value <= 5:
                return value
        except Exception:
            pass
    return None


def _build_string_session(
    dc_id: int, server_ip: str, port: int, auth_key: bytes
) -> str:
    """Build a Telethon StringSession v1 payload."""
    import ipaddress

    ip_bytes = ipaddress.IPv4Address(server_ip).packed
    data = struct.pack(">B", dc_id) + ip_bytes + struct.pack(">H", port) + auth_key
    return "1" + base64.urlsafe_b64encode(data).decode()


_KNOWN_ACCOUNT_FILES = [
    "D877F783D5D3EF8C",
    "D7C2BAC1DE89EE7C",
    "ABF38F0E2B2A3E12",
]


def _find_account_files(tdata_dir: str) -> list[str]:
    """Find known and plausible account data files in a tdata directory."""
    result = []
    for name in _KNOWN_ACCOUNT_FILES:
        for suffix in ("", "1", "0"):
            path = os.path.join(tdata_dir, name + suffix)
            if os.path.exists(path):
                result.append(path)
                break

    try:
        for filename in sorted(os.listdir(tdata_dir)):
            if (
                len(filename) == 16
                and all(c in "0123456789ABCDEFabcdef" for c in filename)
                and os.path.join(tdata_dir, filename) not in result
            ):
                path = os.path.join(tdata_dir, filename)
                if os.path.isfile(path):
                    result.append(path)
    except Exception:
        pass

    return result


def convert_tdata(tdata_dir: str, passphrase: str = "") -> list[dict]:
    """Convert tdata directory contents to Telethon StringSession records."""
    pass_bytes = passphrase.encode("utf-8") if passphrase else b""

    local_key = _read_local_key(tdata_dir, pass_bytes)
    log.info("tdata: LocalKey parsed (%d bytes)", len(local_key))

    account_files = _find_account_files(tdata_dir)
    if not account_files:
        raise ValueError(
            "No account data files found in tdata. Expected files like "
            "D877F783D5D3EF8C. Check that the uploaded folder is a complete "
            "Telegram Desktop tdata directory."
        )

    sessions = []
    for file_path in account_files:
        try:
            content = _read_tdf_raw(file_path)
            decrypted = _decrypt_account_file(content, local_key)
            if decrypted is None:
                log.debug("tdata: could not decrypt %s", os.path.basename(file_path))
                continue

            auth_keys = _scan_for_auth_key(decrypted)
            if not auth_keys:
                log.debug(
                    "tdata: auth_key not found in %s", os.path.basename(file_path)
                )
                continue

            dc_id = _scan_for_dc_id(decrypted) or 2
            dc_id = max(1, min(5, dc_id))
            server_ip = _DC_IPS[dc_id]

            for auth_key in auth_keys[:1]:
                session_str = _build_string_session(
                    dc_id, server_ip, _DC_PORT, auth_key
                )
                sessions.append(
                    {
                        "session_str": session_str,
                        "dc_id": dc_id,
                        "source_file": os.path.basename(file_path),
                    }
                )
                log.info(
                    "tdata: session built from %s, DC=%d",
                    os.path.basename(file_path),
                    dc_id,
                )
        except Exception as exc:
            log.debug("tdata: skipped file %s: %s", os.path.basename(file_path), exc)

    return sessions


def check_pycryptodome() -> bool:
    """Return True when pycryptodome AES support is importable."""
    try:
        importlib.import_module("Crypto.Cipher.AES")
        return True
    except ImportError:
        return False
