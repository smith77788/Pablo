"""Продвинутый инвайт: автовыдача админки инвайтерам + промоут-трюк.

Пользователь: инвайтер должен получать права админа перед инвайтингом (для канала
это обязательно), а при блокировке прямого инвайта — добавлять через выдачу/снятие
админки. Реализовано в _exec_mass_invite (prep + fallback) поверх движка.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = (ROOT / "services" / "mass_inviter_engine.py").read_text(encoding="utf-8")
WORKER = (ROOT / "services" / "op_worker.py").read_text(encoding="utf-8")


def test_engine_helpers_exist():
    import services.mass_inviter_engine as inv  # модуль импортируется без telethon
    assert callable(getattr(inv, "channel_admin_status", None)), "нет проверки прав аккаунта"
    assert callable(getattr(inv, "add_via_promote", None)), "нет промоут-трюка"


def test_invite_batch_reports_privacy_failed():
    assert '"privacy_failed"' in ENGINE, "invite_batch не возвращает список отклонённых приватностью"
    assert "privacy_failed.append(ref)" in ENGINE, "отклонённые приватностью не собираются"


def test_add_via_promote_grants_then_revokes():
    m = re.search(r"async def add_via_promote\(.*?\n\n\n", ENGINE, re.S)
    assert m, "add_via_promote не найден"
    body = m.group(0)
    assert body.count("EditAdminRequest") >= 2, "трюк должен и выдать, и снять права"
    assert "invite_users=True" in body and "invite_users=False" in body, "нет выдачи+снятия"


def test_worker_prep_promotes_inviters():
    assert "channel_admin_status(" in WORKER, "воркер не ищет аккаунт-промоутер"
    assert "promote_to_admin(" in WORKER, "воркер не выдаёт админку инвайтерам"
    assert 'params.get("auto_promote", True)' in WORKER, "автовыдача не включена по умолчанию"


def test_worker_uses_promote_trick_for_blocked():
    assert "add_via_promote(" in WORKER, "воркер не применяет промоут-трюк к заблокированным"
    assert 'params.get("promote_trick", True)' in WORKER, "промоут-трюк не включён по умолчанию"
    assert "_privacy_blocked" in WORKER, "заблокированные приватностью цели не собираются в воркере"


def test_prep_is_failopen_no_promoter():
    """Нет аккаунта-админа → не падаем, честно логируем и идём как есть."""
    assert "ни один инвайтер не создатель/админ" in WORKER, "нет честного сообщения при отсутствии промоутера"
