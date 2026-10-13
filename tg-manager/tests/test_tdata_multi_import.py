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
    import services.tdata_converter as tc
    monkeypatch.setattr(tc, "check_pycryptodome", lambda: True)

    def _fake_convert(root):
        label = os.path.basename(os.path.dirname(root))
        if label.endswith("__broken"):
            raise ValueError("key_datas not found")
        return [{"session_str": "SS_" + label}]

    monkeypatch.setattr(tc, "convert_tdata", _fake_convert)

    async def _fake_verify(ss):
        if ss.endswith("__dead"):
            return None, "сессия не авторизована"
        return (ss, {"tg_user_id": 1, "phone": "", "username": ss}), ""

    monkeypatch.setattr(am, "_verify_tdata_session", _fake_verify)


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
    assert res["found"] == 3, "битая папка должна выпасть из found, остальные — нет"
    oks = {info["username"] for _, info in res["accounts"]}
    assert oks == {"SS_79301__one", "SS_79304__four"}
    assert any("broken" in e for e in res["errors"])
    assert any("dead" in e for e in res["errors"])


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
