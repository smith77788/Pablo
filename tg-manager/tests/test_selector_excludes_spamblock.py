"""Гейт: единая дверь выбора аккаунта НЕ отдаёт аккаунты под спам-блоком.

По аудиту (проактивная реабилитация): аккаунт в 'spamblock' ограничен Telegram и
не должен толкаться в массовые операции — иначе ограничение усугубляется вплоть до
бана. flood_engine сам ВЫСТАВЛЯЕТ spamblock, но при ВЫБОРЕ его надо исключать.
Оба селектора двери (bulk select_all_active и одиночный get_best_account) обязаны
фильтровать spamblock наряду с banned/deactivated/session_expired.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _statuses_excluded(src: str) -> bool:
    needle = ("COALESCE(a.acc_status, 'active') NOT IN "
              "('banned', 'deactivated', 'session_expired', 'spamblock')")
    return needle in src


def test_bulk_door_excludes_spamblock():
    src = (ROOT / "services" / "resource_selector.py").read_text(encoding="utf-8")
    assert _statuses_excluded(src), "select_all_active не исключает spamblock"


def test_single_door_excludes_spamblock():
    src = (ROOT / "services" / "flood_engine.py").read_text(encoding="utf-8")
    assert _statuses_excluded(src), "get_best_account не исключает spamblock"
