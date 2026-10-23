"""Полное покрытие классификатора ошибок операций (services/op_errors.py).

Модуль чистый и не имеет состояния — значит, обязан быть покрыт до всех веток:
нормализация результата (алиасы/статус/сводка), классификация retry/flood/
peer_flood/fatal/skip, сеть-прокси, мёртвая сессия vs временный конфликт двух IP.
"""
from __future__ import annotations

from services import op_errors as oe


# ── _normalize_result: все ветки ───────────────────────────────────────────────
def test_normalize_non_dict_wraps_into_summary():
    r = oe._normalize_result("готово", "x", 1.234)
    assert r["status"] == "done"
    assert r["summary"] == "готово"
    assert r["ok"] == 0 and r["failed"] == 0 and r["total"] == 0
    assert r["duration_s"] == 1.2 and r["op_type"] == "x"


def test_normalize_ok_alias_resolution():
    # каждый алиас поднимается в каноничный ok
    for alias in ("sent", "created", "waves_completed", "left", "deleted",
                  "joined", "invited", "published"):
        r = oe._normalize_result({alias: 7}, "op", 0.0)
        assert r["ok"] == 7, alias


def test_normalize_ok_defaults_to_zero_when_no_alias():
    r = oe._normalize_result({"status": "done"}, "op", 0.0)
    assert r["ok"] == 0


def test_normalize_fail_alias_and_total_and_summary():
    r = oe._normalize_result({"ok": 5, "fail": 3}, "op", 2.0)
    assert r["failed"] == 3          # fail → failed
    assert r["total"] == 8           # ok+failed
    assert "5 успешно" in r["summary"] and "3 ошибок" in r["summary"]


def test_normalize_keeps_explicit_failed_over_fail():
    r = oe._normalize_result({"ok": 1, "failed": 7, "fail": 999}, "op", 1.0)
    assert r["failed"] == 7          # явный failed приоритетнее алиаса fail


def test_normalize_preserves_existing_summary_and_total():
    r = oe._normalize_result({"ok": 1, "failed": 0, "total": 42, "summary": "своё"}, "op", 0.0)
    assert r["total"] == 42 and r["summary"] == "своё"


# ── _classify_op_error: каждая ветка ───────────────────────────────────────────
def test_classify_session_conflict_is_retry_not_fatal():
    assert oe._classify_op_error(RuntimeError("AUTH_KEY_DUPLICATED")) == "retry"
    assert oe._classify_op_error(RuntimeError("used under two different IP")) == "retry"


def test_classify_fatal_by_name_and_message():
    assert oe._classify_op_error(RuntimeError("USER_DEACTIVATED_BAN")) == "fatal"
    assert oe._classify_op_error(RuntimeError("SESSION_REVOKED")) == "fatal"

    class ChannelBannedError(Exception):
        pass
    assert oe._classify_op_error(ChannelBannedError("x")) == "fatal"


def test_classify_peer_flood_before_plain_flood():
    assert oe._classify_op_error(RuntimeError("PEER_FLOOD detected")) == "peer_flood"


def test_classify_plain_flood():
    assert oe._classify_op_error(RuntimeError("FLOOD_WAIT 30")) == "flood"

    class FloodWaitError(Exception):
        pass
    assert oe._classify_op_error(FloodWaitError("wait")) == "flood"


def test_classify_retry_by_name_and_keywords():
    assert oe._classify_op_error(TimeoutError("boom")) == "retry"
    assert oe._classify_op_error(RuntimeError("connection reset")) == "retry"
    assert oe._classify_op_error(RuntimeError("Read timeout")) == "retry"


def test_classify_skip_on_private_or_admin_required():
    assert oe._classify_op_error(RuntimeError("CHANNEL_PRIVATE")) == "skip"
    assert oe._classify_op_error(RuntimeError("CHAT_ADMIN_REQUIRED")) == "skip"

    class ChatAdminRequired(Exception):
        pass
    assert oe._classify_op_error(ChatAdminRequired("x")) == "skip"


def test_classify_unknown_defaults_to_retry():
    assert oe._classify_op_error(RuntimeError("нечто неведомое")) == "retry"


# ── сеть/прокси и мёртвая сессия ───────────────────────────────────────────────
def test_network_or_proxy_matches_patterns_and_conflict():
    assert oe._is_network_or_proxy_error("general socks server failure") is True
    assert oe._is_network_or_proxy_error("no route to host") is True
    assert oe._is_network_or_proxy_error("AUTH_KEY_DUPLICATED") is True  # конфликт → тоже сеть
    assert oe._is_network_or_proxy_error("совершенно обычный текст") is False


def test_dead_session_true_paths_and_conflict_excluded():
    # реальные признаки мёртвой сессии
    assert oe._is_dead_session_error("AuthKeyUnregistered") is True
    assert oe._is_dead_session_error("SESSION_REVOKED") is True
    assert oe._is_dead_session_error("different data center") is True
    # временный конфликт двух IP — НЕ мёртвая сессия
    assert oe._is_dead_session_error("AUTH_KEY_DUPLICATED") is False
    # обычный текст — не мёртвая
    assert oe._is_dead_session_error("timeout") is False


def test_session_conflict_helper():
    assert oe._is_session_conflict_error("AuthKeyDuplicated") is True
    assert oe._is_session_conflict_error("") is False
    assert oe._is_session_conflict_error(None) is False


def test_dead_session_is_fail_soft_when_metric_raises(monkeypatch):
    """Сбой метрики не имеет права менять вердикт — она наблюдатель (fail-soft)."""
    def _boom(*a, **k):
        raise RuntimeError("метрика упала")
    monkeypatch.setattr("services.metrics.inc", _boom)
    # обе ветки (конфликт и реальная смерть) должны отработать, несмотря на сбой
    assert oe._is_dead_session_error("AUTH_KEY_DUPLICATED") is False
    assert oe._is_dead_session_error("AuthKeyUnregistered") is True
