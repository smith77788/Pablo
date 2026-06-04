"""
�������� ��������� tdata > Telethon StringSession.
�� ������� opentele. ���������� pycryptodome ��� AES-IGE.

������������: Telegram Desktop 2.x/3.x/4.x, tdata ��� ������ (����������� ������).
"""

from __future__ import annotations

import base64
import hashlib
import io
import logging
import os
import struct
from typing import Optional

log = logging.getLogger(__name__)


# -- ������-������� ------------------------------------------------------------


def _xor(a: bytes, b: bytes) -> bytes:
    return bytes(x ^ y for x, y in zip(a, b))


def _aes_ige_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    """AES-256-IGE ����������� (����� MTProto/TDesktop)."""
    from Crypto.Cipher import AES as _AES  # type: ignore

    aes = _AES.new(key, _AES.MODE_ECB)
    m_prev, c_prev = iv[:16], iv[16:]
    out = bytearray()
    for i in range(0, len(data), 16):
        c = data[i : i + 16]
        m = _xor(aes.decrypt(_xor(c, m_prev)), c_prev)
        c_prev, m_prev = c, m
        out.extend(m)
    return bytes(out)


def _pass_key_legacy(passphrase: bytes, salt: bytes):
    """SHA-512 ����� AES ����� � IV �� ���� (legacy TDesktop �����)."""
    sha = hashlib.sha512(salt + hashlib.sha512(passphrase).digest()).digest()
    return sha[:32], sha[32:64]  # key=32B, iv=32B


def _prep_aes_local(auth_key: bytes, msg_key: bytes, decrypt: bool = True):
    """SHA-1 PrepareAES ��� ��������� ������ TDesktop (legacy ������)."""
    x = 8 if decrypt else 0
    sha1a = hashlib.sha1(msg_key + auth_key[x : x + 36]).digest()
    sha1b = hashlib.sha1(auth_key[x + 40 : x + 76] + msg_key).digest()
    sha1c = hashlib.sha1(auth_key[x + 84 : x + 120] + msg_key).digest()
    sha1d = hashlib.sha1(msg_key + auth_key[x + 128 : x + 160]).digest()
    aes_key = sha1a[:8] + sha1b[8:20] + sha1c[4:16]
    aes_iv = sha1a[8:20] + sha1b[:8] + sha1c[16:20] + sha1d[:8]
    return aes_key, aes_iv


def _prep_aes_local_sha256(auth_key: bytes, msg_key: bytes, decrypt: bool = True):
    """SHA-256 PrepareAES ��� ��������� ������ TDesktop (����� ������ 3.x+)."""
    x = 4 if decrypt else 0
    sha256a = hashlib.sha256(msg_key + auth_key[x : x + 36]).digest()
    sha256b = hashlib.sha256(auth_key[x + 40 : x + 76] + msg_key).digest()
    aes_key = sha256a[:8] + sha256b[8:24] + sha256a[24:32]
    aes_iv = sha256b[:8] + sha256a[8:24] + sha256b[24:32]
    return aes_key, aes_iv


# -- TDF �������� ������ -------------------------------------------------------

_TDF_MAGIC = b"TDF$"


def _read_tdf_raw(path: str) -> bytes:
    """������ TDF$ ����, ��������� MD5, ���������� content (��� ���������/��������)."""
    with open(path, "rb") as f:
        raw = f.read()
    if len(raw) < 24 or raw[:4] != _TDF_MAGIC:
        raise ValueError(f"�� TDF ����: {path}")
    ver_bytes = raw[4:8]
    content = raw[8:-16]
    md5_stored = raw[-16:]
    md5_calc = hashlib.md5(_TDF_MAGIC + ver_bytes + content).digest()
    if md5_calc != md5_stored:
        raise ValueError(f"TDF ����������� ����� �� ���������: {path}")
    return content


def _tdf_read(base: str, name: str) -> bytes:
    """TDF ���� � fallback �� �������� 1 � 0."""
    for suffix in ("", "1", "0"):
        p = os.path.join(base, name + suffix)
        if os.path.exists(p):
            try:
                return _read_tdf_raw(p)
            except Exception:
                continue
    raise FileNotFoundError(f"TDF ���� {name} �� ������ � {base}")


# -- Qt DataStream -------------------------------------------------------------


class _QS:
    def __init__(self, data: bytes):
        self._b = io.BytesIO(data)

    def u32(self) -> int:
        b = self._b.read(4)
        if len(b) < 4:
            raise EOFError("QStream: ������������ ������")
        return struct.unpack(">I", b)[0]

    def i32(self) -> int:
        b = self._b.read(4)
        if len(b) < 4:
            raise EOFError
        return struct.unpack(">i", b)[0]

    def ba(self) -> bytes:
        """������ QByteArray: uint32 length + data."""
        n = self.u32()
        if n == 0xFFFFFFFF:
            return b""
        b = self._b.read(n)
        if len(b) < n:
            raise EOFError(f"QStream: QByteArray ������� {n}, �������� {len(b)}")
        return b

    def remaining(self) -> bytes:
        return self._b.read()

    def pos(self) -> int:
        return self._b.tell()


# -- ������ LocalKey �� key_datas ---------------------------------------------

_SALT_SIZE = 64
_LOCAL_KEY_SIZE = 256


def _read_local_key(tdata_dir: str, passphrase: bytes = b"") -> bytes:
    """
    ��������� 256-������� LocalKey �� tdata/key_datas.
    LocalKey � ������-���� ��� ����������� ���� ������ ��������.
    """
    content = _tdf_read(tdata_dir, "key_datas")
    s = _QS(content)

    # ��� QByteArray: salt (64 �����) � ������������� ����
    salt = s.ba()
    encrypted = s.ba()

    if len(salt) == 0:
        # Fallback: ��������� ������ ����� salt ��� ������ 64 ����� ����� ������
        if len(content) > _SALT_SIZE:
            salt = content[:_SALT_SIZE]
            encrypted = content[_SALT_SIZE:]
        else:
            raise ValueError("key_datas: �� ������� ����")

    if len(salt) < _SALT_SIZE:
        raise ValueError(f"key_datas: ���� ������� ��������: {len(salt)} ����")

    # ��������� ��� AES-IGE �������� (��������� 16)
    if len(encrypted) == 0:
        raise ValueError("key_datas: ��� ������������� ������")
    # ����������� �� 16 ����
    if len(encrypted) % 16 != 0:
        encrypted = encrypted[: len(encrypted) - len(encrypted) % 16]

    aes_key, aes_iv = _pass_key_legacy(passphrase, salt)
    try:
        decrypted = _aes_ige_decrypt(aes_key, aes_iv, encrypted)
    except Exception as e:
        raise ValueError(f"key_datas: ������ ����������� AES-IGE: {e}")

    # �������������� ����: [SHA1 check (16-20 bytes)] + QByteArray(LocalKey)
    # ������� ������ ��������
    for skip in (0, 4, 16, 20):
        try:
            s2 = _QS(decrypted[skip:])
            lk = s2.ba()
            if len(lk) == _LOCAL_KEY_SIZE:
                log.debug("tdata: LocalKey ������ (skip=%d)", skip)
                return lk
        except Exception:
            pass

    # Fallback: ���� ������ 256 ���� ��������
    if len(decrypted) >= _LOCAL_KEY_SIZE:
        log.debug("tdata: LocalKey fallback (raw bytes)")
        return decrypted[:_LOCAL_KEY_SIZE]

    raise ValueError(
        f"tdata: �� ������� ������� LocalKey (decrypted={len(decrypted)}B)"
    )


# -- ����������� ������ �������� -----------------------------------------------


def _decrypt_account_file(content: bytes, local_key: bytes) -> Optional[bytes]:
    """
    �������������� ���������� ����� �������� TDesktop.
    ������� SHA-1 � SHA-256 PrepareAES.
    """
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
            # ������� ��������: ������ 4 ����� � �����, ������ ���� ��������
            if len(decrypted) >= 4:
                data_len = struct.unpack(">I", decrypted[:4])[0]
                if 4 <= data_len <= len(decrypted) + 256:
                    return decrypted
        except Exception:
            pass

    return None


# -- ���������� auth_key � DC --------------------------------------------------

# ����������� DCs Telegram (IPv4)
_DC_IPS = {
    1: "149.154.175.53",
    2: "149.154.167.51",
    3: "149.154.175.100",
    4: "149.154.167.91",
    5: "91.108.56.130",
}
_DC_PORT = 443


def _scan_for_auth_key(data: bytes) -> list[bytes]:
    """���� 256-������� �����, ������� ����� ���� auth_key."""
    candidates = []
    s = io.BytesIO(data)
    while True:
        pos = s.tell()
        chunk = s.read(4)
        if len(chunk) < 4:
            break
        try:
            length = struct.unpack(">I", chunk)[0]
        except Exception:
            s.seek(pos + 1)
            continue
        if length == _LOCAL_KEY_SIZE:
            auth_key = s.read(256)
            if len(auth_key) == 256:
                # ��������� ��� ��� �� ������� ����
                if any(b != 0 for b in auth_key):
                    candidates.append(auth_key)
            s.seek(pos + 1)
        else:
            s.seek(pos + 1)
    return candidates


def _scan_for_dc_id(data: bytes) -> Optional[int]:
    """���� DC ID � �������������� ������."""
    # DC ID �������� ��� int32 ��� uint32 � ��������� 1-5
    for i in range(0, min(len(data) - 4, 256), 4):
        try:
            v = struct.unpack(">I", data[i : i + 4])[0]
            if 1 <= v <= 5:
                # ��������� �� �������� ������ � ����� ������ ���� �������� �������
                return v
        except Exception:
            pass
    return None


# -- ������ Telethon StringSession --------------------------------------------


def _build_string_session(
    dc_id: int, server_ip: str, port: int, auth_key: bytes
) -> str:
    """�������� Telethon StringSession v1 �� �����������."""
    import ipaddress

    ip_bytes = ipaddress.IPv4Address(server_ip).packed  # 4 bytes
    data = struct.pack(">B", dc_id) + ip_bytes + struct.pack(">H", port) + auth_key
    return "1" + base64.urlsafe_b64encode(data).decode()


# -- ���������� ������ �������� ------------------------------------------------

_KNOWN_ACCOUNT_FILES = [
    "D877F783D5D3EF8C",
    "D7C2BAC1DE89EE7C",
    "ABF38F0E2B2A3E12",
]


def _find_account_files(tdata_dir: str) -> list[str]:
    """������� ������������� ����� ������ �������� � tdata ����������."""
    result = []
    # ��������� �����
    for name in _KNOWN_ACCOUNT_FILES:
        for suffix in ("", "1", "0"):
            p = os.path.join(tdata_dir, name + suffix)
            if os.path.exists(p):
                result.append(p)
                break

    # ����� �� hex-����� �� 16 ��������
    try:
        for fname in sorted(os.listdir(tdata_dir)):
            if (
                len(fname) == 16
                and all(c in "0123456789ABCDEFabcdef" for c in fname)
                and os.path.join(tdata_dir, fname) not in result
            ):
                p = os.path.join(tdata_dir, fname)
                if os.path.isfile(p):
                    result.append(p)
    except Exception:
        pass

    return result


# -- ������� ����� ����� -------------------------------------------------------


def convert_tdata(tdata_dir: str, passphrase: str = "") -> list[dict]:
    """
    ������������ tdata ���������� � ������ Telethon StringSession.

    ���������� ������ dicts:
        [{"session_str": "1ABC...", "dc_id": 2, "source_file": "..."}]

    ����� ������� ������ ������ ���� �� ���� ���� �� �������������.
    Raises ValueError ���� key_datas �� ��������.
    """
    pass_bytes = passphrase.encode("utf-8") if passphrase else b""

    # 1. ������ LocalKey
    local_key = _read_local_key(tdata_dir, pass_bytes)
    log.info("tdata: LocalKey ������� (%d ����)", len(local_key))

    # 2. ���� ����� ��������
    account_files = _find_account_files(tdata_dir)
    if not account_files:
        raise ValueError(
            "� tdata �� ������� ����� �������� (D877F783D5D3EF8C � �������). "
            "��������� ��� ����� �������� ������ ����� tdata."
        )

    sessions = []
    for fpath in account_files:
        try:
            content = _read_tdf_raw(fpath)
            decrypted = _decrypt_account_file(content, local_key)
            if decrypted is None:
                log.debug("tdata: �� ������� ������������ %s", os.path.basename(fpath))
                continue

            # ���� auth_key
            auth_keys = _scan_for_auth_key(decrypted)
            if not auth_keys:
                log.debug("tdata: auth_key �� ������ � %s", os.path.basename(fpath))
                continue

            # ���� DC ID
            dc_id = _scan_for_dc_id(decrypted) or 2  # �� ��������� DC2 (��������)
            dc_id = max(1, min(5, dc_id))
            server_ip = _DC_IPS[dc_id]

            for auth_key in auth_keys[:1]:  # ���� ������ ��������
                session_str = _build_string_session(
                    dc_id, server_ip, _DC_PORT, auth_key
                )
                sessions.append(
                    {
                        "session_str": session_str,
                        "dc_id": dc_id,
                        "source_file": os.path.basename(fpath),
                    }
                )
                log.info(
                    "tdata: ������ ������� �� %s, DC=%d",
                    os.path.basename(fpath),
                    dc_id,
                )
        except Exception as e:
            log.debug("tdata: ������ ����� %s: %s", os.path.basename(fpath), e)

    return sessions


def check_pycryptodome() -> bool:
    """��������� ������� pycryptodome (AES)."""
    try:
        from Crypto.Cipher import AES  # type: ignore  # noqa

        return True
    except ImportError:
        return False

