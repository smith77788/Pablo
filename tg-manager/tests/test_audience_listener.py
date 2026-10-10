"""Регресс: пассивный слушатель Update-потока (services/audience_listener.py).

Первопричина: весь проект узнаёт о входящих через periodic connect→запрос→
disconnect (poll), кроме Business Vault (событийно, но только для аккаунтов
через Telegram Business API — не для обычного флота tg_accounts). MTProto сам
пушит Updates в подключённую сессию — слушать это пассивно для чатов, где
аккаунт и так состоит, не эксплойт, а push вместо poll (меньше запросов →
меньше флуд-сигналов → меньше риска бана).

Opt-in по конструкции: держит соединение аккаунта открытым часами, поэтому
использует ТОТ ЖЕ единый арбитр (op_worker.try_claim_account/release_accounts),
что и ghost_engine/account_warmer — аккаунт, занятый обычной операцией,
слушатель не тронет (иначе AUTH_KEY_DUPLICATED — необратимая смерть сессии).
Продление аренды не пишется отдельно: op_worker.renew_leases() уже вызывается
в главном цикле воркера и продлевает ВСЕ аккаунты, арендованные этим процессом.
"""
from __future__ import annotations

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def test_schema_adds_opt_in_column():
    sql = _read("schema_v193_audience_listener.sql")
    assert "ADD COLUMN IF NOT EXISTS listener_enabled" in sql
    assert "DEFAULT FALSE" in sql  # выключено по умолчанию — не для всего флота


def test_uses_single_arbiter_not_own_locking():
    """Не должно быть собственного in-memory/DB замка мимо op_worker — иначе
    ровно тот класс бага, что op_worker.py явно предостерегает не повторять
    (см. комментарий про 'помечал занятыми БЕЗУСЛОВНО')."""
    src = _read("services/audience_listener.py")
    assert "try_claim_account" in src and "release_accounts" in src
    assert "from services import op_worker" in src


def test_no_separate_lease_renewal_reinvented():
    """renew_leases() уже продлевает всё, что арендовал этот процесс — если
    здесь появится собственный heartbeat/renew, это дублирование механизма,
    который уже есть и уже тестируется в op_worker."""
    src = _read("services/audience_listener.py")
    assert "renew_lease" not in src.replace("renew_leases() уже", "")


def test_reuses_get_account_for_telethon_not_hand_rolled_select():
    """database.db.get_account_for_telethon уже собирает device+proxy+decrypt
    контракт для _make_client — писать свой SELECT с нуля рискует разойтись
    с ним (например, забыть JOIN на user_proxies)."""
    src = _read("services/audience_listener.py")
    assert "get_account_for_telethon" in src
    assert "_make_client" in src


def test_incoming_only_filter_not_raw_all_updates():
    """v1 намеренно узкий — входящие ЛС (тот же охват, что и у intent_sensor
    для Business-аккаунтов), не весь сырой Update-поток (шумный, requires
    отдельного проектирования fan-out на группы/каналы)."""
    src = _read("services/audience_listener.py")
    assert "events.NewMessage(incoming=True)" in src


def test_feeds_existing_intent_sensor_not_duplicated_logic():
    src = _read("services/audience_listener.py")
    assert "intent_sensor.scan_incoming" in src


def test_peer_dict_shape_matches_vault_service_contract():
    """intent_sensor.scan_incoming ждёт dict с peer_user_id/peer_name/
    peer_username (см. services/vault_service.peer_of) — та же форма,
    независимо от источника (Business API или Telethon-событие)."""
    src = _read("services/audience_listener.py")
    for key in ("peer_user_id", "peer_name", "peer_username"):
        assert f'"{key}"' in src, f"нет ключа {key} в peer-словаре"


def test_registered_as_background_task_in_main():
    src = _read("main.py")
    assert "from services import audience_listener" in src
    assert 'asyncio.create_task(\n            _resilient("audience_listener", audience_listener.run, pool, bot)' in src \
        or '_resilient("audience_listener", audience_listener.run, pool, bot)' in src


def test_fail_soft_on_scan_error():
    """Сбой обработки одного входящего/тика не должен ронять весь цикл —
    тот же принцип fail-soft, что у immunity_engine/ghost_engine."""
    src = _read("services/audience_listener.py")
    assert src.count("log_exc_swallow") >= 4


def test_process_local_state_documented():
    """CLAUDE.md пункт 6: in-memory/process-local state — пометить явно."""
    src = _read("services/audience_listener.py")
    assert "Process-local" in src or "process-local" in src
    assert "не переживает рестарт" in src
