"""Регресс: фоновые мониторы, ПОДКЛЮЧАЮЩИЕ аккаунты, не трогают занятые операцией.

Корень «свежий флот не стартует, AUTH_KEY_DUPLICATED»: только что добавленные
аккаунты (last_real_check_at IS NULL) мониторы берут ПЕРВЫМИ и коннектят ровно
тогда, когда оператор запускает операцию → одна сессия с двух коннектов = бан
auth key. Каждый такой монитор обязан фильтровать in_operation И сверять
op_worker.is_account_in_use прямо перед коннектом.
"""
from __future__ import annotations

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


# Модули, которые РЕАЛЬНО подключают аккаунты в фоне (check_account_status_full /
# connect_client / _make_client), и потому обязаны уважать занятость.
_CONNECTING_MONITORS = [
    "services/account_monitor.py",
    "services/account_health.py",
    "services/account_manager.py",   # run_session_health_monitor
    "services/account_warmer.py",
    "services/ghost_engine.py",      # фоновый «шум» — тоже живая сессия
    # Список был неполным: main.py запускает как create_task ещё несколько
    # независимых циклов, коннектящих аккаунты в обход мьютекса connect_client
    # (_make_client()+client.connect() напрямую) — найдено при разборе жалобы
    # «живой аккаунт не берётся в работу, показывает сессия/сеть» (мас-инвайт
    # ловил SessionBusyError от гонки с этими же циклами за ту же сессию).
    "services/showcase_layer.py",    # release_wave/_post_wave — волны витрины
    "services/drift_detector.py",    # get_full_channel_info — дрейф карточки канала
    "services/narrative_engine.py",  # post_to_channel — публикация нарратив-постов
    "services/ranking_checker.py",   # search_in_telegram — проверка позиций в поиске
]

# Захват перед коннектом: is_account_in_use (снимок) ИЛИ атомарный try_claim_account
# (сильнее — check-and-set под локом, без TOCTOU). Любой закрывает окно гонки.
_GUARD_TOKENS = ("try_claim_account", "try_claim_accounts", "is_account_in_use")


def _has_guard(src: str) -> bool:
    return any(tok in src for tok in _GUARD_TOKENS)


def test_connecting_monitors_filter_in_operation_in_sql():
    for rel in _CONNECTING_MONITORS:
        src = _read(rel)
        assert "COALESCE(a.in_operation, FALSE) = FALSE" in src \
            or "COALESCE(in_operation, FALSE) = FALSE" in src, \
            f"{rel}: выборка аккаунтов для проверки/прогрева не фильтрует in_operation"


def test_connecting_monitors_guard_before_connect():
    # АТОМАРНЫЙ захват прямо перед коннектом. Снимок is_account_in_use недостаточен:
    # операция может захватить аккаунт между проверкой и коннектом (TOCTOU) → одна
    # сессия с двух IP = AUTH_KEY_DUPLICATED (сжигание свежего флота). Только
    # try_claim_account (check-and-set под _accounts_lock) закрывает окно.
    for rel in _CONNECTING_MONITORS:
        src = _read(rel)
        assert "try_claim_account" in src, \
            f"{rel}: коннектящий монитор обязан АТОМАРНО захватывать сессию " \
            f"(try_claim_account), снимок is_account_in_use допускает гонку"


# Прочие фоновые циклы (main.py _resilient), которые тоже коннектят аккаунты по
# своей выборке — обязаны сверять занятость перед коннектом (не по фиксированной
# SQL-колонке, а по ин-мемори реестру, т.к. выбирают конкретные аккаунты).
_OTHER_BG_CONNECTORS = [
    "services/activity_engine.py",
    "services/content_mesh.py",
    "services/keyword_watcher.py",
]


def test_other_background_connectors_guard():
    for rel in _OTHER_BG_CONNECTORS:
        src = _read(rel)
        assert _has_guard(src), \
            f"{rel}: фоновый коннект аккаунта без захвата (try_claim/is_account_in_use)"


def test_shadowban_monitor_does_not_connect():
    # Контроль: shadowban_monitor только читает БД (flood_count), НЕ коннектит —
    # значит правило in_operation к нему не применяется (и не требуется).
    src = _read("services/shadowban_monitor.py")
    assert "check_account_status_full" not in src
    assert "connect_client" not in src
