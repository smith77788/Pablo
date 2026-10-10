"""Инвайт в канал без прав админа: честный стоп и понятная причина, а не 0 вслепую.

Репорт: запустил инвайт тысяч юзеров — 0 добавлено, а аккаунты не админы в чате.
Причина: InviteToChannelRequest без прав кидает ChatAdminRequiredError, который
движок НЕ ловил → попадал в общий except, писался как ошибка юзера и молотил ВСЕ
тысячи вхолостую (0 добавлено, никакого объяснения). Для канала добавлять
участников может только админ с правом «Добавлять подписчиков».
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = (ROOT / "services" / "mass_inviter_engine.py").read_text(encoding="utf-8")
WORKER = (ROOT / "services" / "op_worker.py").read_text(encoding="utf-8")


def test_engine_handles_admin_required():
    assert "ChatAdminRequiredError" in ENGINE, "движок не импортирует/не ловит ChatAdminRequired"
    m = re.search(r"except ChatAdminRequiredError:.*?break", ENGINE, re.S)
    assert m, "нет отдельной обработки ChatAdminRequired с остановкой"
    body = m.group(0)
    assert "group error" in body, "ошибка прав должна помечаться как групповая (стоп флота)"
    assert "прав" in body and ("админ" in body.lower() or "подписчик" in body.lower()), \
        "причина должна называть права/админа"


def test_engine_stops_not_grinds():
    """Групповая ошибка обрывает батч (break), а не перебирает все тысячи."""
    # В ветке ChatAdminRequired есть break сразу после append.
    m = re.search(r"except ChatAdminRequiredError:.*?errors\.append\(.*?\)\s*\n\s*break", ENGINE, re.S)
    assert m, "после пометки прав нет немедленного break"


def test_worker_surfaces_group_reason():
    """Итог операции показывает КОНКРЕТНУЮ причину недоступности, не общее «недоступна»."""
    assert "_group_reason" in WORKER, "причина групповой ошибки не захватывается"
    assert "Причина:" in WORKER, "причина не выводится в сводку"


def test_worker_explains_zero_added_privacy():
    """Когда 0 добавлено из-за приватности — честное объяснение, не молчаливый ноль."""
    assert "_fail_reasons" in WORKER, "нет разбора причин отказов"
    assert "приватност" in WORKER.lower(), "нет объяснения про приватность получателей"
