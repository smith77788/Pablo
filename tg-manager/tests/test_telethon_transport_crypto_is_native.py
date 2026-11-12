"""Трафик флота шифруется нативно, а не на чистом Python.

ЧТО БЫЛО. Все клиенты Telethon ходят через ConnectionTcpObfuscated, а его
AES-CTR в Telethon 1.x — это `pyaes`, чистый Python: 0.4 МБ/с на весь
процесс (замер 12.11.2026), тогда как pycryptodome даёт ~900 МБ/с. Часть
загрузки медиа в 512 КБ держала общий цикл событий ~1.3 с: бот, мини-апп и
все остальные клиенты флота в это время стояли. AES-IGE самого MTProto без
cryptg шёл через libssl по ctypes (8 МБ/с) или тоже на чистом Python.

ЧТО ПРОВЕРЯЕМ.
  * замена даёт ТОТ ЖЕ поток байт, что pyaes, на любой нарезке и на переходе
    счётчика через 2**128 (иначе соединение с Telegram просто не поднимется);
  * НАСТОЯЩИЙ путь создания клиента (`account_manager._make_client`) ставит
    подмену — проверяется в отдельном процессе с настоящим telethon, потому что
    в процессе pytest telethon подменён заглушкой из conftest;
  * через подменённую обфускацию мегабайт шифруется быстрее, чем pyaes
    успевает зашифровать его десятую часть;
  * cryptg есть в requirements.txt.
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _subprocess_can_import(mod: str) -> bool:
    try:
        return subprocess.run([sys.executable, "-c", f"import {mod}"],
                              capture_output=True, timeout=60).returncode == 0
    except Exception:
        return False


def test_native_ctr_is_byte_identical_to_pyaes():
    pyaes = pytest.importorskip("pyaes")
    from services.telethon_crypto import NativeAESModeCTR

    for iv in (os.urandom(16), b"\xff" * 16, b"\x00" * 15 + b"\xfe"):
        key = os.urandom(32)
        ref = pyaes.AESModeOfOperationCTR(key)
        ref._counter._counter = list(iv)
        fast = NativeAESModeCTR(key, iv)
        # Нарезка неровная нарочно: поток обязан продолжаться через границы
        # блоков, а encrypt и decrypt — двигать один и тот же счётчик.
        for i, n in enumerate((1, 15, 16, 17, 64, 1000, 3, 4096)):
            chunk = os.urandom(n)
            if i % 2:
                assert fast.decrypt(chunk) == ref.decrypt(chunk)
            else:
                assert fast.encrypt(chunk) == ref.encrypt(chunk)


def test_cryptg_is_a_dependency():
    with open(os.path.join(ROOT, "requirements.txt"), encoding="utf-8") as f:
        lines = [ln.split("#")[0].strip() for ln in f]
    assert any(ln.startswith("cryptg") for ln in lines), (
        "cryptg — единственная быстрая реализация AES-IGE для MTProto")


_PROBE = textwrap.dedent(r"""
    import os, sys, time, json
    sys.path.insert(0, %(root)r)
    os.environ.setdefault("TOKEN_ENCRYPTION_KEY", "t")
    from services import account_manager
    try:
        account_manager._make_client("", None, low_risk=True)
    except Exception:
        pass   # сам клиент здесь не нужен: проверяем, что путь поставил подмену
    from telethon.network.connection import tcpobfuscated
    from services import telethon_crypto

    class _Conn:
        packet_codec = type("C", (), {"obfuscate_tag": b"\xef" * 4})
        _reader = None
        _writer = None

    io = tcpobfuscated.ObfuscatedIO(_Conn())
    data = os.urandom(1024 * 1024)
    t = time.perf_counter()
    io._encrypt.encrypt(data)
    dt = time.perf_counter() - t
    print(json.dumps({
        "cls": tcpobfuscated.AESModeCTR.__module__ + "." + tcpobfuscated.AESModeCTR.__name__,
        "mb_s": len(data) / dt / 1e6,
        "backends": telethon_crypto.backends(),
    }))
""")


def test_real_client_path_installs_native_obfuscation():
    if not _subprocess_can_import("telethon"):
        pytest.skip("нужен настоящий telethon в отдельном процессе")
    import json

    r = subprocess.run([sys.executable, "-c", _PROBE % {"root": ROOT}],
                       capture_output=True, text=True, timeout=120, cwd=ROOT)
    assert r.returncode == 0, r.stderr[-3000:]
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["cls"] == "services.telethon_crypto.NativeAESModeCTR", out
    assert out["backends"]["ctr"] == "pycryptodome", out
    # pyaes делает ~0.4 МБ/с; порог в 25 раз выше него и в сотни раз ниже
    # нативного — тест не зависит от скорости машины CI.
    assert out["mb_s"] > 10, out
