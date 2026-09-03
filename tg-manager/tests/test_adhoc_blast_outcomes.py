"""Разовая рассылка ЛС: разные исходы требуют разной реакции.

Что было сломано:
  - все исходы складывались в «ошибку»: флуд-вейт, флаг PeerFlood, мёртвая
    сессия и «у получателя закрыты ЛС» были неотличимы;
  - проверялся ключ result['banned'], которого account_manager.send_dm НИКОГДА
    не возвращает → мёртвые сессии не отлавливались вовсе;
  - PeerFlood (аккаунт помечен Telegram за спам) игнорировался, и аккаунт
    продолжал слать — быстрый путь его потерять;
  - выбор аккаунта шёл как `i % len(active)`: при выбывании аккаунта модуль по
    счётчику получателей перескакивал через оставшиеся;
  - дубли в списке получателей давали два одинаковых ЛС одному человеку;
  - не писалось ни одной строки аудита — кому не дошло, узнать было негде.
"""
from __future__ import annotations

import ast
import pathlib

from services.dm_engine import classify_send_result

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_OP_WORKER = _ROOT / "services" / "op_worker.py"


# ── Классификация исходов (реальные ответы account_manager.send_dm) ───────────

def test_success():
    assert classify_send_result({"ok": True}) == "sent"


def test_flood_wait_detected_by_key_and_by_text():
    assert classify_send_result({"error": "FloodWait: подождите 30с", "flood_wait": 30}) == "flood"
    assert classify_send_result({"error": "FLOOD_WAIT_42"}) == "flood"


def test_peer_flood_detected_by_key_and_by_text():
    assert classify_send_result(
        {"error": "PeerFlood: аккаунт временно ограничен", "peer_flood": True}
    ) == "peer_flood"
    assert classify_send_result({"error": "PEER_FLOOD"}) == "peer_flood"


def test_dead_session_is_auth_not_generic_error():
    for err in ("AUTH_KEY_UNREGISTERED", "SESSION_REVOKED", "Unauthorized"):
        assert classify_send_result({"error": err}) == "auth", err


def test_permanent_target_errors_are_skip_not_failure():
    """Аккаунт при этих ошибках ЗДОРОВ — они не должны выглядеть сбоем флота."""
    for err in (
        "приватность: пользователь запретил входящие",
        "заблокирован: вы в чёрном списке",
        "нет доступа к написанию",
        "аккаунт удалён",
        "username не существует",
    ):
        assert classify_send_result({"error": err}) == "skip", err


def test_unknown_error_is_retry():
    assert classify_send_result({"error": "сетевая беда"}) == "retry"


def test_malformed_result_does_not_crash():
    """Попытка сломать: не-словарь и пустой ответ не должны ронять рассылку."""
    assert classify_send_result(None) == "retry"
    assert classify_send_result({}) == "retry"
    assert classify_send_result("строка") == "retry"
    assert classify_send_result({"error": None}) == "retry"


def test_ok_wins_over_stale_error_key():
    assert classify_send_result({"ok": True, "error": ""}) == "sent"


# ── Подключение к разовой рассылке ────────────────────────────────────────────

def _adhoc_source() -> str:
    src = _OP_WORKER.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_exec_bulk_dm_adhoc":
            seg = ast.get_source_segment(src, node)
            assert seg is not None
            return seg
    raise AssertionError("_exec_bulk_dm_adhoc не найдена")


def test_adhoc_uses_classifier():
    assert "_classify_send_result" in _adhoc_source()


def test_adhoc_no_longer_checks_nonexistent_banned_key():
    """send_dm не возвращает 'banned' — проверка была мёртвым кодом."""
    src = _adhoc_source()
    assert 'result.get("banned")' not in src and "result.get('banned')" not in src


def test_adhoc_handles_peer_flood_and_flood_with_cooldown():
    src = _adhoc_source()
    assert "peer_flood" in src, "PeerFlood обязан обрабатываться отдельно"
    assert src.count("cooldown_until") >= 2, (
        "и flood, и peer_flood обязаны ставить аккаунту кулдаун"
    )


def test_adhoc_rotates_accounts_explicitly_not_by_recipient_index():
    src = _adhoc_source()
    assert "acc_idx" in src, "нужен явный вращающийся индекс аккаунта"
    assert "active_accounts[i % len(active_accounts)]" not in src, (
        "выбор по индексу получателя перескакивает через аккаунты при выбывании"
    )


def test_adhoc_deduplicates_recipients():
    src = _adhoc_source()
    assert "_seen_refs" in src, "дубли получателей обязаны схлопываться"


def test_adhoc_writes_audit_log():
    src = _adhoc_source()
    assert "operation_log" in src and "_log_step" in src, (
        "разовая рассылка обязана писать построчный аудит в лог операции"
    )


def test_adhoc_reports_skipped_separately_from_errors():
    src = _adhoc_source()
    assert "skip_count" in src and '"skipped"' in src, (
        "недоступные получатели не должны попадать в счётчик ошибок"
    )
