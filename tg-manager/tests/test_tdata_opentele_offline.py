"""Оффлайн-извлечение ключей из tdata через opentele — БЕЗ сети и без ToTelethon.

ЧТО ЛОМАЛОСЬ. Прод звал `td.ToTelethon(session=StringSession(), UseCurrentSession)`,
а внутри opentele эта ветка присваивает `auth_session` только когда session —
str или None. Передавали ОБЪЕКТ StringSession() → `UnboundLocalError: auth_session`,
и КАЖДЫЙ импорт tdata сваливался в нативный fallback (который тоже падал).

ЧТО ТЕПЕРЬ. У opentele берём только надёжную оффлайн-часть: расшифрованные
`authKey` + номер DC по каждому аккаунту, а StringSession собираем сами и
подключаемся своим клиентом. Здесь проверяем именно эту обвязку на фейковом
opentele (настоящий под нашим conftest не импортируется — telethon заглушён):
извлекаются ВСЕ аккаунты, главный идёт первым, а собранная строка несёт верные
DC и authKey.
"""
from __future__ import annotations

import base64
import struct
import types

from services import account_manager as am
from services.tdata_converter import _DC_IPS, _DC_PORT


def _decode_session(ss: str) -> tuple[int, bytes]:
    raw = base64.urlsafe_b64decode(ss[1:])
    dc_id = raw[0]
    auth_key = raw[1 + 4 + 2 :]  # dc(1) + ipv4(4) + port(2) + authKey(256)
    return dc_id, auth_key


def _install_fake_opentele(monkeypatch, accounts, main_index):
    class _Auth:
        def __init__(self, key):
            self.key = key

    class _Acc:
        def __init__(self, key, dc):
            self._key = key
            self._dc = dc

        @property
        def authKey(self):
            return _Auth(self._key)

        @property
        def MainDcId(self):
            return self._dc

        @property
        def _local(self):  # заставляем код взять IP из _DC_IPS
            raise RuntimeError("endpoints недоступны в тесте")

    accs = [_Acc(k, d) for (k, d) in accounts]

    class _TD:
        def __init__(self, path):
            self._accs = accs

        def isLoaded(self):
            return True

        @property
        def accountsCount(self):
            return len(self._accs)

        @property
        def accounts(self):
            return list(self._accs)

        @property
        def mainAccount(self):
            return self._accs[main_index]

    fake = types.ModuleType("opentele.td")
    fake.TDesktop = _TD
    real_import = am.importlib.import_module

    def _imp(name, *a, **k):
        if name == "opentele.td":
            return fake
        return real_import(name, *a, **k)

    monkeypatch.setattr(am.importlib, "import_module", _imp)


def test_extracts_all_accounts_main_first(monkeypatch):
    key_a = bytes((i * 3 + 1) & 0xFF for i in range(256))
    key_main = bytes((i * 5 + 2) & 0xFF for i in range(256))
    _install_fake_opentele(
        monkeypatch, accounts=[(key_a, 2), (key_main, 4)], main_index=1
    )

    sessions = am._extract_tdata_sessions_opentele("/fake/tdata")
    assert sessions is not None and len(sessions) == 2

    # Главный аккаунт (index=1) должен идти ПЕРВЫМ.
    dc0, k0 = _decode_session(sessions[0])
    assert dc0 == 4 and k0 == key_main
    dc1, k1 = _decode_session(sessions[1])
    assert dc1 == 2 and k1 == key_a


def test_skips_bad_accounts_but_keeps_good(monkeypatch):
    good = bytes((i * 7 + 9) & 0xFF for i in range(256))
    short = b"\x01" * 100  # битый authKey — пропустить
    _install_fake_opentele(
        monkeypatch, accounts=[(short, 2), (good, 1)], main_index=0
    )

    sessions = am._extract_tdata_sessions_opentele("/fake/tdata")
    assert sessions is not None and len(sessions) == 1  # битый пропущен, валидный остался
    dc, k = _decode_session(sessions[0])
    assert dc == 1 and k == good
    # IP в строке — дефолтный для DC1
    raw = base64.urlsafe_b64decode(sessions[0][1:])
    ip = ".".join(str(b) for b in raw[1:5])
    assert ip == _DC_IPS[1]
    assert _DC_PORT == 443


def test_returns_none_without_opentele(monkeypatch):
    real_import = am.importlib.import_module

    def _imp(name, *a, **k):
        if name == "opentele.td":
            raise ImportError("no opentele")
        return real_import(name, *a, **k)

    monkeypatch.setattr(am.importlib, "import_module", _imp)
    assert am._extract_tdata_sessions_opentele("/fake/tdata") is None
