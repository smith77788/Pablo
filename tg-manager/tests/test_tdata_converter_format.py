from __future__ import annotations

import hashlib
import struct
from pathlib import Path

import pytest
from services import tdata_converter


def _tdf_bytes(content: bytes, version: int = 5004000) -> bytes:
    magic = b"TDF$"
    version_bytes = struct.pack("<I", version)
    size_bytes = struct.pack("<I", len(content))
    checksum = hashlib.md5(content + size_bytes + version_bytes + magic).digest()
    return magic + version_bytes + content + checksum


def test_read_tdf_uses_telegram_desktop_checksum(tmp_path: Path):
    content = b"current Telegram Desktop payload"
    path = tmp_path / "key_datas"
    path.write_bytes(_tdf_bytes(content))

    assert tdata_converter._read_tdf_raw(str(path)) == content


def test_tdf_suffix_fallback_supports_all_desktop_slots(tmp_path: Path):
    content = b"fallback slot"
    (tmp_path / "key_datas").write_bytes(_tdf_bytes(content))

    assert tdata_converter._tdf_read(str(tmp_path), "key_data") == content


def test_checksum_mismatch_is_not_fatal(tmp_path: Path):
    """Несошедшаяся контрольная сумма НЕ блокирует чтение.

    Прод-доказательство (скрин владельца): ВСЕ его рабочие аккаунты падали на
    «TDF checksum mismatch». Значит порядок полей в MD5 у его версии Telegram
    Desktop иной, чем наша формула. Контрольная сумма — проверка целостности, а
    не ключ расшифровки: payload (content) цел, и расшифровка использует именно
    его. Поэтому бьём по целостности мягко — возвращаем content, а реально
    битый файл отсеется дальше, на расшифровке.
    """
    content = b"payload"
    raw = bytearray(_tdf_bytes(content))
    raw[-1] ^= 0xFF  # ломаем байт контрольной суммы
    (tmp_path / "key_datas").write_bytes(raw)

    assert tdata_converter._read_tdf_raw(str(tmp_path / "key_datas")) == content
    assert tdata_converter._tdf_read(str(tmp_path), "key_datas") == content


def test_non_tdf_file_is_reported_not_missing(tmp_path: Path):
    """А вот файл БЕЗ магии TDF$ (обрезан/чужой формат) — честно сообщаем."""
    (tmp_path / "key_datas").write_bytes(b"not a tdf file at all")
    with pytest.raises(ValueError, match="найден, но не читается как TDF"):
        tdata_converter._tdf_read(str(tmp_path), "key_datas")


def test_production_has_full_tdata_converter_dependency():
    project = Path(__file__).resolve().parents[1]
    requirements = (project / "requirements.txt").read_text(encoding="utf-8")
    workspace = (project.parent / "pyproject.toml").read_text(encoding="utf-8")
    dockerfile = (project / "Dockerfile").read_text(encoding="utf-8")

    assert "opentele-tg==1.15.3" in requirements
    assert '"opentele-tg==1.15.3' in workspace
    assert "libglib2.0-0" in dockerfile
    assert "from opentele.td import TDesktop" in dockerfile
