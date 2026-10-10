"""Прогрев из карточки аккаунта: приостановленный план ВОЗОБНОВЛЯЕТСЯ, не начинается заново.

Баг UX: на карточке аккаунта пауза-план показывал «🔥 Запустить (стандарт)» —
клик стартовал НОВЫЙ план с дня 0, теряя прогресс прогрева. Кнопки «Возобновить» не
было (resume был только на глобальном экране). Фикс: account_detail отдаёт id плана;
карточка при status='paused' показывает «▶ Возобновить» → resumeWarmup(accId, planId)
→ /warmup/{id}/resume (owner-scoped, только paused). «Запустить» остаётся лишь когда
плана нет/завершён.
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

from services import mini_app_api

HTML = (Path(__file__).resolve().parent.parent / "mini_app" / "index.html").read_text(encoding="utf-8")
SRC = inspect.getsource(mini_app_api)


def test_account_detail_returns_warmup_plan_id():
    # без id карточка не сможет вызвать resume по плану
    assert re.search(r"SELECT id, current_day, target_days, status, started_at\s+FROM account_warmup_plans", SRC)


def test_card_shows_resume_for_paused_not_restart():
    m = re.search(r"const warmupHtml = `(.*?)`;", HTML, re.DOTALL)
    assert m, "warmupHtml не найден"
    block = m.group(1)
    # paused → Возобновить с id плана
    assert "warmup.status==='paused'" in block
    assert "resumeWarmup(${a.id},${warmup.id})" in block
    # Запустить (новый план) — только когда НЕ active и НЕ paused
    assert "warmup.status!=='active'&&warmup.status!=='paused'" in block


def test_resume_endpoint_owner_scoped_and_paused_only():
    m = re.search(r"async def warmup_resume_plan\(request.*?\n(.*?)\n    async def ", SRC, re.DOTALL)
    assert m, "warmup_resume_plan не найден"
    body = m.group(1)
    assert "owner_id=$2" in body and "status='paused'" in body, "resume должен быть owner-scoped и только paused"


def test_resume_frontend_calls_endpoint():
    m = re.search(r"async function resumeWarmup\(accId, planId\)\s*\{(.*?)\n\}", HTML, re.DOTALL)
    assert m, "resumeWarmup не найден"
    assert "/api/miniapp/warmup/'+planId+'/resume" in m.group(1)
