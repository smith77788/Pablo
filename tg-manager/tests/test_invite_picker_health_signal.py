"""Проактивный риск-пульс в пикере аккаунтов инвайтера.

Раньше пользователь узнавал о пропуске аккаунтов (карантин/под риском) только ПОСТ-
ФАКТУМ в итоге операции. Теперь пикер аккаунтов инвайтера помечает флагнутые аккаунты
(🛑 карантин / ⚠️ под риском) и показывает сводку «N под риск-пульсом — пропустят для
защиты» ДО запуска. Данные уже есть: /api/miniapp/accounts отдаёт health_status
(quarantine|at_risk|healthy).
"""
from __future__ import annotations

import re
from pathlib import Path

HTML = (Path(__file__).resolve().parent.parent / "mini_app" / "index.html").read_text(encoding="utf-8")


def test_backend_accounts_expose_health_status():
    src = (Path(__file__).resolve().parent.parent / "services" / "mini_app_api.py").read_text(encoding="utf-8")
    assert 'r["health_status"] = h["status"]' in src, "accounts должен отдавать health_status"


def test_invite_picker_marks_flagged_accounts():
    m = re.search(r"const wrap = document\.getElementById\('massInviteAccsWrap'\);(.*?)// Render history",
                  HTML, re.DOTALL)
    assert m, "блок рендера пикера инвайтера не найден"
    body = m.group(1)
    # per-account badge по обоим статусам
    assert "riskBadge" in body
    assert "health_status==='quarantine'" in body and "health_status==='at_risk'" in body
    # агрегатная сводка о пропуске
    assert "под риск-пульсом" in body
    assert "flagged" in body


def test_badge_reflects_skip_semantics():
    # текст badge объясняет, что аккаунт будет ПРОПУЩЕН (совпадает с поведением executor)
    i = HTML.index("riskBadge")
    seg = HTML[i:i + 500]
    assert "пропущен" in seg
