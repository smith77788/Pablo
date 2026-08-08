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


def _aes_ige_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    """Decrypt AES-256-IGE blocks used by MTProto/TDesktop."""
    crypto_cipher = importlib.import_module("Crypto.Cipher")
    aes_cls = cast(Any, getattr(crypto_cipher, "AES"))

    aes = aes_cls.new(key, aes_cls.MODE_ECB)
    m_prev, c_prev = iv[:16], iv[16:]
    out = bytearray()
    for i in range(0, len(data), 16):
        c = data[i : i + 16]
        m = _xor(aes.decrypt(_xor(c, m_prev)), c_prev)
        c_prev, m_prev = c, m
        out.extend(m)
    return bytes(out)


def _pass_key_legacy(passphrase: bytes, salt: bytes):
    """Build AES key/IV from passphrase and salt for legacy TDesktop files."""
    sha = hashlib.sha512(salt + hashlib.sha512(passphrase).digest()).digest()
    return sha[:32], sha[32:64]


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
        raise ValueError(f"Invalid TDF file: {path}")
    ver_bytes = raw[4:8]
    content = raw[8:-16]
    md5_stored = raw[-16:]
    md5_calc = hashlib.md5(_TDF_MAGIC + ver_bytes + content).digest()
    if md5_calc != md5_stored:
        raise ValueError(f"TDF checksum mismatch: {path}")
    return content


def _tdf_read(base: str, name: str) -> bytes:
    """Read a TDF file with Telegram Desktop suffix fallback."""
    for suffix in ("", "1", "0"):
        path = os.path.join(base, name + suffix)
        if os.path.exists(path):
            try:
                return _read_tdf_raw(path)
            except Exception:
                continue
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


_SALT_SIZE = 64
_LOCAL_KEY_SIZE = 256


def _read_local_key(tdata_dir: str, passphrase: bytes = b"") -> bytes:
    """Extract the 256-byte LocalKey from tdata/key_datas."""
    content = _tdf_read(tdata_dir, "key_datas")
    stream = _QS(content)

    salt = stream.ba()
    encrypted = stream.ba()

    if len(salt) == 0:
        if len(content) > _SALT_SIZE:
            salt = content[:_SALT_SIZE]
            encrypted = content[_SALT_SIZE:]
        else:
            raise ValueError("key_datas: salt not found")

    if len(salt) < _SALT_SIZE:
        raise ValueError(f"key_datas: salt is too short: {len(salt)} bytes")

    if len(encrypted) == 0:
        raise ValueError("key_datas: encrypted payload is empty")
    if len(encrypted) % 16 != 0:
        encrypted = encrypted[: len(encrypted) - len(encrypted) % 16]

    aes_key, aes_iv = _pass_key_legacy(passphrase, salt)
    try:
        decrypted = _aes_ige_decrypt(aes_key, aes_iv, encrypted)
    except Exception as exc:
        raise ValueError(f"key_datas: AES-IGE decrypt failed: {exc}") from exc

    for skip in (0, 4, 16, 20):
        try:
            stream2 = _QS(decrypted[skip:])
            local_key = stream2.ba()
            if len(local_key) == _LOCAL_KEY_SIZE:
                log.debug("tdata: LocalKey parsed (skip=%d)", skip)
                return local_key
        except Exception:
            pass

    if len(decrypted) >= _LOCAL_KEY_SIZE:
        log.debug("tdata: LocalKey fallback via raw bytes")
        return decrypted[:_LOCAL_KEY_SIZE]

    raise ValueError(f"tdata: LocalKey not found (decrypted={len(decrypted)}B)")


def _decrypt_account_file(content: bytes, local_key: bytes) -> Optional[bytes]:
    """Try known TDesktop local-account decrypt variants."""
    if len(content) < 24:
        return None

    msg_key = content[8:24]
    encrypted = content[24:]
    if len(encrypted) % 16 != 0:
        encrypted = encrypted[: len(encrypted) - len(encrypted) % 16]

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
