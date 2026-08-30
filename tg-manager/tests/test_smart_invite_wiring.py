"""Разводка безопасного режима инвайтинга: UI → params → governor в исполнителе.

Проверяем статически, что флаг safe_mode доходит из бота и мини-аппа до
op_worker и что исполнитель реально спрашивает governor (can_invite) и копит
исходы (note_sent/note_outcome), а не игнорирует режим.
"""
from __future__ import annotations

import ast
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    return open(os.path.join(ROOT, rel), encoding="utf-8").read()


def test_bot_offers_safe_mode_and_passes_it():
    h = _read("bot/handlers/mass_inviter.py")
    assert 'item="safe"' in h, "нет кнопки безопасного режима в боте"
    assert '"safe_mode": bool(data.get("inv_safe"))' in h, "флаг не уходит в params"


def test_miniapp_passes_safe_mode():
    api = _read("services/mini_app_api.py")
    m = api[api.index("async def mass_inviter_submit"):]
    m = m[:m.index("\n    async def ", 1)]
    assert 'body.get("safe_mode")' in m, "мини-апп не читает safe_mode"
    assert '"safe_mode"' in m
    html = _read("mini_app/index.html")
    assert "invSafeMode" in html, "нет чекбокса безопасного режима во фронте"
    assert "body.safe_mode = true" in html, "чекбокс не уходит в запрос"


def test_executor_gates_on_governor_in_safe_mode():
    """op_worker в safe-режиме обязан спрашивать governor и копить исходы."""
    src = _read("services/op_worker.py")
    ex = src[src.index("async def _exec_mass_invite"):]
    ex = ex[:ex.index("\nasync def ", 1)]
    assert "_safe_mode" in ex, "safe_mode не читается исполнителем"
    assert "smart_invite" in ex, "governor не подключён"
    assert "can_invite(" in ex, "исполнитель не спрашивает governor перед инвайтом"
    assert "note_sent(" in ex and "note_outcome(" in ex, "исходы по чату не копятся"
    # chat-flood отражается как заморозка приёма чата
    assert '"chat_flood"' in ex, "флуд чата не отмечается как заморозка приёма"
    # живость чата оценивается до массового притока
    assert "assess_and_store_liveness" in ex, "живость чата не оценивается"


def test_governor_module_has_all_mechanics():
    """Ядро обязано покрывать заявленные механики: частота, заморозка, живость,
    негатив, разогрев."""
    src = _read("services/smart_invite.py")
    tree = ast.parse(src)
    names = {n.name for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    for fn in ("decide", "can_invite", "note_sent", "note_outcome",
               "set_liveness", "liveness_from_signals", "assess_and_store_liveness"):
        assert fn in names, f"governor не имеет {fn}"
    # частотное окно, заморозка, негатив-стоп — прямо в decide
    d = src[src.index("def decide("):]
    d = d[:d.index("\ndef ", 1)]
    assert "60" in d and "window" in d, "нет окна частоты/мин"
    assert "paused_until" in d, "нет заморозки приёма чата"
    assert "abort" in d, "нет стопа (мёртвый чат / негатив)"
