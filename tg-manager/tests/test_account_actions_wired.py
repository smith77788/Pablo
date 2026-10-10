"""Регрессия: действия в карточке аккаунта не должны быть «фантомными op».

Баг: кнопки reauth / export_session / reset_cooldown в mini-app ставили op'ы
(reauth_account, export_session, reset_cooldown), которых НЕТ в op_worker →
клик «успешен», но ничего не происходило (сессия так и не переавторизована,
кулдаун не сброшен, сессия не экспортирована). Класс dead-button (CLAUDE.md).

Фикс: эти три действия обрабатываются СИНХРОННО в эндпоинте (reset/export —
реальный эффект; reauth — честная инструкция в бота, т.к. нужен код). Ни один
из трёх op_type не должен появляться в очереди.
"""
from __future__ import annotations

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def test_no_phantom_op_executors_left():
    ow = _read("services/op_worker.py")
    # этих op_type НЕТ исполнителей — значит их нельзя ставить в очередь
    for t in ("reauth_account", "export_session", "reset_cooldown"):
        assert f'op_type == "{t}"' not in ow, f"внезапно появился исполнитель {t}?"


def test_account_action_handles_three_inline_not_as_ops():
    api = _read("services/mini_app_api.py")
    seg = api[api.index("async def account_action"):api.index("async def account_post_story")]
    # больше НЕ ставим фантомные op'ы этих типов
    assert "\"reauth_account\"" not in seg and "'reauth_account'" not in seg
    assert "op_type, params, label = \"export_session\"" not in seg
    assert "op_type, params, label = \"reset_cooldown\"" not in seg
    # reset_cooldown — синхронный реальный эффект: сброс дожидаются прямо в
    # эндпоинте, а не ставят в очередь. Сам UPDATE переехал в
    # services/account_reset.py — одна реализация на кнопки Mini App и бота,
    # иначе они расходились по шагам (tests/test_risk_cleared_at.py).
    assert "await reset_account(" in seg
    # export_session — синхронно расшифровывает и отдаёт сессию владельцу
    assert "decrypt_token" in seg and '"session": sess' in seg
    # reauth — честная инструкция в бота (интерактивный код), без фантомного op
    assert "Переавторизатор" in seg
    # рабочие op-действия остаются через очередь
    assert "scan_owned_resources" in seg and "leave_all_chats" in seg


def test_reauth_ui_shows_instruction_not_fake_launch():
    ui = _read("mini_app/index.html")
    seg = ui[ui.index("async function reauthAccount"):ui.index("async function unbindAccProxy")]
    # убрали ложный «Запускаю переавторизацию…» — показываем реальную инструкцию
    assert "Запускаю переавторизацию" not in seg
    assert "/api/miniapp/account/${id}/action/reauth" in seg
