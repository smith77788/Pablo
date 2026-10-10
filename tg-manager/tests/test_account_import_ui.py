"""Ядро «аккаунт → операция», шаг 1: добавить аккаунт можно В мини-аппе.

Раньше добавление аккаунта работало ТОЛЬКО в боте: список аккаунтов слал «Добавьте
через меню в Telegram», а бэкенд-роут /import_sessions (умеющий импортировать
сессии) не был подключён ни к одной кнопке. Первый шаг ядрового сценария не работал
в основном UI — прямой вклад в ощущение «сыро/MVP».

Фикс: кнопка «➕ Добавить» в шапке аккаунтов и в пустом состоянии → модалка импорта
сессий → существующий POST /api/miniapp/import_sessions → честный итог
(imported/failed/errors). Эти проверки фиксируют, что связь не оборвётся.
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

from services import mini_app_api


def _api_src() -> str:
    return inspect.getsource(mini_app_api)


def _index() -> str:
    return (Path(__file__).resolve().parent.parent / "mini_app" / "index.html").read_text(encoding="utf-8")


def test_import_route_registered_and_wired():
    src = _api_src()
    assert 'app.router.add_post("/api/miniapp/import_sessions", import_sessions_api)' in src
    m = re.search(r"async def import_sessions_api\(.*?\n(.*?)\n    app\.router", src, re.DOTALL)
    assert m, "import_sessions_api не найден"
    body = m.group(1)
    assert "if not uid" in body and "401" in body, "должен требовать авторизацию"
    assert "import_sessions" in body, "должен звать session_importer.import_sessions"


def test_accounts_screen_has_in_app_add():
    html = _index()
    # кнопка в шапке экрана аккаунтов
    assert "openAccImportModal()" in html, "нет точки входа добавления аккаунта в UI"
    # пустое состояние теперь actionable, а не «идите в бота»
    assert "Добавьте аккаунт через меню в Telegram" not in html, (
        "пустое состояние не должно просто отсылать в бота"
    )
    # модалка существует
    assert 'id="accImportMbg"' in html and "submitAccImport" in html


def test_import_assigns_selected_proxy():
    """Ре-аудит шага 1: выбранный в модалке прокси должен ЗАКРЕПЛЯТЬСЯ за
    импортированным аккаунтом (изоляция), а не только использоваться для проверки —
    иначе аккаунт падает на общий CF-relay с единым IP."""
    from services import session_importer
    src = inspect.getsource(session_importer.import_sessions)
    assert "proxy_id" in src, "import_sessions должен принимать proxy_id"
    assert "user_proxies WHERE id=$1 AND owner_id=$2" in src, (
        "proxy_id должен сверяться на принадлежность владельцу (не закреплять чужой)"
    )
    # proxy_id реально попадает в INSERT аккаунта
    assert re.search(r"INSERT INTO tg_accounts.*proxy_id", src, re.DOTALL), (
        "proxy_id должен писаться в tg_accounts при импорте"
    )
    # фронт передаёт proxy_id
    html = _index()
    m = re.search(r"async function submitAccImport\(\)\s*\{(.*?)\n\}", html, re.DOTALL)
    assert "proxy_id:proxyId" in m.group(1) or "proxy_id: proxyId" in m.group(1), (
        "модалка должна передавать proxy_id на бэкенд"
    )


def test_submit_calls_real_endpoint_with_honest_result():
    html = _index()
    m = re.search(r"async function submitAccImport\(\)\s*\{(.*?)\n\}", html, re.DOTALL)
    assert m, "submitAccImport не найдена"
    body = m.group(1)
    assert "/api/miniapp/import_sessions" in body, "должен звать реальный эндпойнт импорта"
    # честный итог: реальные imported/failed, а не «готово»
    assert "r.imported" in body and "r.failed" in body, (
        "итог должен показывать реальные imported/failed (не фейк-успех)"
    )
