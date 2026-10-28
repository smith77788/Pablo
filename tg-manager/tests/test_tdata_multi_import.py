"""Архив tdata с многими аккаунтами импортируется ЦЕЛИКОМ, а не по одному.

Жалоба владельца: ZIP с 20 аккаунтами tdata не загрузился («TDF file key_datas
not found in …»). Причины:
  1) поиск брал ПЕРВУЮ папку tdata — импортировался один аккаунт из двадцати;
  2) одна битая/непарсящаяся папка роняла весь импорт — остальные 19 даже не
     пробовались.

Фикс: находим ВСЕ папки tdata, конвертируем и проверяем каждую независимо
(битая не рушит партию), возвращаем сводку accounts/errors/found.
"""
from __future__ import annotations

import io
import os
import zipfile

import pytest
from services import account_manager as am


def _zip_with(folders: list[str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for f in folders:
            z.writestr(f"{f}/tdata/key_datas", b"TDF$dummy-key")
            z.writestr(f"{f}/tdata/D877F783D5D3EF8C/maps", b"x")
    return buf.getvalue()


def test_find_all_tdata_roots(tmp_path):
    for name in ("79300000001__a", "79300000002__b", "79300000003__c"):
        d = tmp_path / name / "tdata"
        d.mkdir(parents=True)
        (d / "key_datas").write_bytes(b"x")
    roots = am._find_all_tdata_roots(str(tmp_path))
    assert len(roots) == 3, f"нашлись не все папки tdata: {roots}"
    assert all(r.endswith("tdata") for r in roots)


def _patch_converter(monkeypatch):
    async def _fake_import(root):
        label = os.path.basename(os.path.dirname(root))
        if label.endswith("__broken"):
            raise ValueError("key_datas not found")
        if label.endswith("__dead"):
            raise ValueError("сессия не авторизована")
        ss = "SS_" + label
        return ss, {"tg_user_id": 1, "phone": "", "username": ss}

    monkeypatch.setattr(am, "import_from_tdata", _fake_import)


@pytest.mark.asyncio
async def test_imports_all_accounts(monkeypatch):
    _patch_converter(monkeypatch)
    zb = _zip_with(["79301__one", "79302__two", "79303__three"])
    res = await am.import_tdata_accounts_from_zip_bytes(zb)
    assert res["found"] == 3
    assert len(res["accounts"]) == 3, "импортированы не все аккаунты"
    assert res["errors"] == []


@pytest.mark.asyncio
async def test_one_bad_folder_does_not_sink_the_batch(monkeypatch):
    _patch_converter(monkeypatch)
    # Одна папка не парсится, одна сессия мёртвая — остальные должны пройти.
    zb = _zip_with(["79301__one", "79302__broken", "79303__dead", "79304__four"])
    res = await am.import_tdata_accounts_from_zip_bytes(zb)
    assert res["found"] == 4, "found считает все обнаруженные папки tdata"
    oks = {info["username"] for _, info in res["accounts"]}
    assert oks == {"SS_79301__one", "SS_79304__four"}
    assert any("broken" in e for e in res["errors"])
    assert any("dead" in e for e in res["errors"])


@pytest.mark.asyncio
async def test_batch_uses_full_converter_instead_of_native_fallback(monkeypatch):
    calls = []

    async def _full(root):
        calls.append(root)
        return "SS", {"tg_user_id": 1, "username": "full"}

    monkeypatch.setattr(am, "import_from_tdata", _full)
    res = await am.import_tdata_accounts_from_zip_bytes(_zip_with(["79301__one"]))

    assert len(calls) == 1
    assert res["accounts"][0][1]["username"] == "full"


@pytest.mark.asyncio
async def test_single_wrapper_returns_first(monkeypatch):
    _patch_converter(monkeypatch)
    zb = _zip_with(["79301__one"])
    ss, info = await am.import_tdata_from_zip_bytes(zb)
    assert ss == "SS_79301__one" and info["tg_user_id"] == 1


@pytest.mark.asyncio
async def test_no_tdata_folder_raises(monkeypatch):
    _patch_converter(monkeypatch)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("readme.txt", b"no tdata here")
    with pytest.raises(ValueError):
        await am.import_tdata_accounts_from_zip_bytes(buf.getvalue())


def test_tdf_read_reports_corrupt_vs_missing(tmp_path):
    """key_datas есть, но битый → честная причина, а не «not found»."""
    from services import tdata_converter as tc
    # файл есть, но не TDF$ (битый/обрезан)
    (tmp_path / "key_datas").write_bytes(b"not a tdf file at all")
    try:
        tc._tdf_read(str(tmp_path), "key_datas")
        assert False, "должно было бросить"
    except FileNotFoundError:
        assert False, "битый файл не должен выглядеть как отсутствующий"
    except ValueError as e:
        assert "не читается" in str(e)

    # файла нет вовсе → FileNotFoundError
    import pytest as _pt
    with _pt.raises(FileNotFoundError):
        tc._tdf_read(str(tmp_path / "empty"), "key_datas")


@pytest.mark.asyncio
async def test_rar_archive_gives_clear_message():
    rar = b"Rar!\x1a\x07\x00" + b"\x00" * 50
    with pytest.raises(ValueError) as ei:
        await am.import_tdata_accounts_from_zip_bytes(rar)
    assert "RAR" in str(ei.value)


@pytest.mark.asyncio
async def test_7z_archive_gives_clear_message():
    sevenz = b"7z\xbc\xaf\x27\x1c" + b"\x00" * 50
    with pytest.raises(ValueError) as ei:
        await am.import_tdata_accounts_from_zip_bytes(sevenz)
    assert "7z" in str(ei.value)


@pytest.mark.asyncio
async def test_opentele_failure_falls_back_to_native(monkeypatch):
    """Сбой opentele (UnboundLocalError auth_session на проде) НЕ роняет импорт —
    управление доходит до нативного конвертера."""
    import importlib as _il
    import types as _types
    import services.tdata_converter as tc

    class _FakeTD:
        def __init__(self, path):
            pass

        def isLoaded(self):
            return True

        async def ToTelethon(self, **kw):
            raise UnboundLocalError(
                "cannot access local variable 'auth_session'")

    fake_td = _types.ModuleType("opentele.td")
    fake_td.TDesktop = _FakeTD
    fake_api = _types.ModuleType("opentele.api")
    fake_api.UseCurrentSession = object()
    real_import = _il.import_module

    def _imp(name, *a, **k):
        if name == "opentele.td":
            return fake_td
        if name == "opentele.api":
            return fake_api
        return real_import(name, *a, **k)

    monkeypatch.setattr(am.importlib, "import_module", _imp)
    monkeypatch.setattr(tc, "check_pycryptodome", lambda: True)

    def _native_marker(path, passphrase=""):
        raise ValueError("__NATIVE_REACHED__")

    monkeypatch.setattr(tc, "convert_tdata", _native_marker)

    # Если бы откат не работал, упали бы на opentele-ошибке «auth_session».
    with pytest.raises(ValueError, match="__NATIVE_REACHED__"):
        await am.import_from_tdata("/fake/tdata")
