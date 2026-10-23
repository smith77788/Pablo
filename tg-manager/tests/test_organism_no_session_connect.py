"""Гарантия: организм НЕ коннектит сессии Telegram (иначе — AUTH_KEY_DUPLICATED).

Организм-раннер крутится в фоне раз в 15 мин по всем владельцам. Если хоть один
его путь (world/brain/runner/spine, fleet_governor, диагностика хранилища,
сенсор намерений) подключит живую сессию параллельно с операцией — вернётся тот
самый конфликт «сессия с двух IP», который сжигал флот. Эти модули обязаны быть
ТОЛЬКО про БД. Храповик по исходникам: любой коннект здесь уронит сборку.
"""
from __future__ import annotations

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Файлы, которые дёргает фоновый heartbeat организма (read-only контекст).
_DB_ONLY_FILES = [
    "services/organism/spine.py",
    "services/organism/world.py",
    "services/organism/brain.py",
    "services/organism/runner.py",
    "services/fleet_governor.py",
    "services/intent_sensor.py",
]

# Признаки живого коннекта к Telegram-сессии.
_CONNECT_MARKERS = (
    "connect_client",
    "_make_client",
    "check_account_status",
    "TelegramClient(",
    ".connect()",
    "send_dm(",
    "join_channel(",
)


def test_organism_paths_never_connect_sessions():
    offenders = []
    for rel in _DB_ONLY_FILES:
        src = open(os.path.join(ROOT, rel), encoding="utf-8").read()
        for m in _CONNECT_MARKERS:
            if m in src:
                offenders.append(f"{rel}: {m}")
    assert not offenders, (
        "Организм должен быть только про БД — найден коннект сессии "
        "(риск AUTH_KEY_DUPLICATED):\n  " + "\n  ".join(offenders))


def test_vault_diagnostics_is_db_only():
    # diagnostics зовётся из world.snapshot фоново — не должна коннектить.
    src = open(os.path.join(ROOT, "services", "vault_service.py"), encoding="utf-8").read()
    diag = src[src.index("async def diagnostics"):]
    diag = diag[:diag.index("\n\nasync def ") if "\n\nasync def " in diag else len(diag)]
    for m in ("connect_client", "_make_client", ".connect()", "TelegramClient("):
        assert m not in diag, f"vault.diagnostics коннектит сессию: {m}"
