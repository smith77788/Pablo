"""Событие «операция завершилась» слалось по SSE и выбрасывалось фронтом.

ЧТО БЫЛО СЛОМАНО. `/api/miniapp/events` на каждом тике считает завершившиеся
операции (`fetch_completed_ops`) и пушит событие `op_complete` с id, типом,
меткой, статусом и счётчиками. Фронт подписан на `stats`, `activity` и
`op_progress` — на `op_complete` подписчика не было. Событие вычислялось,
сериализовалось, уходило по проводу и выбрасывалось.

Для пользователя это выглядело так: он запускает массовую операцию из мини-аппа,
видит её в «⏳ Выполняется», а потом она просто исчезает. Что получилось —
знал только чат бота (`db.notify_if_enabled`, op_worker). То есть человек,
работающий в мини-аппе как в «визуальном софте», обязан был переключиться в
переписку с ботом, чтобы узнать результат собственного действия.

Плюс сам итог (`result->>'summary'`) в событие не клался — без него мини-апп мог
сказать только «завершилась», но не «что получилось», а это тот же тихий успех.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "mini_app" / "index.html").read_text(encoding="utf-8")
API = (ROOT / "services" / "mini_app_api.py").read_text(encoding="utf-8")


def test_frontend_subscribes_to_op_complete():
    assert re.search(r"addEventListener\(\s*['\"]op_complete['\"]", HTML), (
        "событие приходит по проводу и выбрасывается — операция «исчезает»"
    )


def test_backend_sends_the_actual_summary():
    """«Завершилась» без результата — тот же тихий успех, только в новом месте."""
    m = re.search(r"async def fetch_completed_ops.*?ORDER BY finished_at", API, re.DOTALL)
    assert m, "источник события не найден"
    assert "summary" in m.group(0), "итог операции обязан попадать в событие"
    push = re.search(r'await push\("op_complete".*?\}\)', API, re.DOTALL)
    assert push and '"summary"' in push.group(0), "итог должен уходить на фронт"


def test_banner_shows_honest_outcome():
    m = re.search(r"function showOpComplete\(op\)\s*\{.*?\n\}", HTML, re.DOTALL)
    assert m, "обработчик не найден"
    body = m.group(0)
    assert "op.summary" in body, "сначала показываем реальный итог операции"
    assert "op.error_msg" in body, "у провала должна быть видимая причина"
    assert "op.done" in body and "op.total" in body, (
        "если ни итога, ни причины нет — показываем хотя бы честный счётчик, "
        "а не пустую плашку"
    )


def test_banner_leads_to_details_not_a_dead_end():
    m = re.search(r"function showOpComplete\(op\)\s*\{.*?\n\}", HTML, re.DOTALL)
    assert "openOpDetail(op.id)" in m.group(0), (
        "итог без перехода в детали — тупик: непонятно, что делать дальше"
    )
    assert re.search(r"function openOpDetail\(", HTML), "экран деталей должен существовать"


def test_banner_can_be_dismissed_and_auto_hides():
    """Плашка перекрывает низ экрана — она обязана уходить и по кнопке, и сама."""
    m = re.search(r"function showOpComplete\(op\)\s*\{.*?\n\}", HTML, re.DOTALL)
    body = m.group(0)
    assert "hideOpDone" in body, "нужна кнопка закрытия"
    assert "setTimeout" in body, "и авто-скрытие, иначе плашка залипнет навсегда"
    assert re.search(r"function hideOpDone\(\)", HTML)


def test_failed_op_is_not_shown_as_success():
    m = re.search(r"function showOpComplete\(op\)\s*\{.*?\n\}", HTML, re.DOTALL)
    body = m.group(0)
    assert "op.status === 'done'" in body, "провал не должен выглядеть успехом"
    assert "⚠️" in body and "✅" in body


def test_banner_element_exists_outside_screens():
    """Операцию запускают с любого экрана — итог должен быть виден отовсюду,
    а не только на главной (как блок «⏳ Выполняется»)."""
    assert 'id="opDone"' in HTML
    assert "#opDone{position:fixed" in HTML, (
        "плашка внутри экрана была бы не видна с того места, где запускали"
    )
