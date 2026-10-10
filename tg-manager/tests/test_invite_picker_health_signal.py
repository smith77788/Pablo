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

# Мини-апп больше не один файл: экраны вынесены в mini_app/screens/*.js.
# Источник берём целиком, иначе вынос экрана роняет проверку, хотя
# функциональность на месте.
from tests.miniapp_source import miniapp_source

HTML = miniapp_source()


def test_backend_accounts_expose_health_status():
    src = (Path(__file__).resolve().parent.parent / "services" / "mini_app_api.py").read_text(encoding="utf-8")
    assert 'r["health_status"] = h["status"]' in src, "accounts должен отдавать health_status"


def _fn_body(name: str) -> str:
    """Тело функции по балансу скобок.

    Раньше блок рендера выхватывался отрезком «от строки с massInviteAccsWrap до
    комментария // Render history». Рендер переехал в отдельную функцию
    (`_invLoadAccs`, пикер получил поиск и постраничную загрузку), и такой отрезок
    стал перекрывать ДВЕ функции сразу: проверка всё ещё проходила, но смотрела
    уже не туда. Границы берём по коду, а не по расстоянию."""
    src = miniapp_source()
    m = re.search(r"^(?:async )?function " + re.escape(name) + r"\s*\(", src, re.M)
    assert m, f"функция {name} не найдена"
    i = src.index("{", m.end() - 1)
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[i:j + 1]
    raise AssertionError(f"не закрылось тело {name}")


def test_invite_picker_marks_flagged_accounts():
    body = _fn_body("_invLoadAccs")
    # per-account badge по обоим статусам
    assert "riskBadge" in body
    assert "health_status==='quarantine'" in body and "health_status==='at_risk'" in body
    # агрегатная сводка о пропуске
    assert "под риск-пульсом" in body
    assert "flagged" in body


def test_badge_reflects_skip_semantics():
    # текст badge объясняет, что аккаунт будет ПРОПУЩЕН (совпадает с поведением executor)
    body = _fn_body("_invLoadAccs")
    i = body.index("riskBadge")
    assert "пропущен" in body[i:i + 500]
