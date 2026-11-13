"""Уборка проблемных ресурсов по итогу проверки каналов/чатов/ботов.

Просьба владельца (13.11.2026): «после проверки сущностей я не вижу возможности
быстро удалить заблокированные ресурсы или те, которые в теневом бане или
недоступны; такой возможности нет и во вкладках каналов/ботов».

Сервер уже умел снимать с учёта только НЕДОСТУПНЫЕ (restrictions/dead,
status=unreachable). Добавлены: быстрое действие по итогу проверки, эндпоинт
со ВСЕМИ проблемными статусами (ограничен / теневой бан / недоступен) и экран
уборки, достижимый из карточки операции и из каталогов.
"""
from __future__ import annotations

import os

from services import op_quick_actions as qa

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(rel):
    return open(os.path.join(ROOT, rel), encoding="utf-8").read()


def test_check_offers_cleanup_when_there_are_problems():
    res = {"findings": [
        {"kind": "channel", "id": 1, "status": "restricted"},
        {"kind": "bot", "id": 2, "status": "hidden"},
        {"kind": "channel", "id": 3, "status": "unreachable"},
        {"kind": "channel", "id": 4, "status": "clean"},
    ]}
    acts = qa.suggest("check_owned_restrictions", "done", res, op_id=843, params={})
    cu = [a for a in acts if a["id"] == "cleanup_restrictions"]
    assert cu, "нет быстрого действия уборки"
    assert cu[0]["fn"] == "openRestrictionCleanup" and cu[0]["arg"] == 843
    assert "(3)" in cu[0]["label"], "в счёт должны войти ограничен+теневой+недоступен, но не clean"


def test_no_cleanup_action_when_everything_is_clean():
    res = {"findings": [{"kind": "channel", "id": 1, "status": "clean"}]}
    acts = qa.suggest("check_owned_restrictions", "done", res, op_id=5, params={})
    assert not [a for a in acts if a["id"] == "cleanup_restrictions"]


def test_cleanup_action_only_for_the_right_op_type():
    res = {"findings": [{"kind": "channel", "id": 1, "status": "restricted"}]}
    acts = qa.suggest("mass_invite", "done", res, op_id=5, params={})
    assert not [a for a in acts if a["id"] == "cleanup_restrictions"]


def test_problems_endpoint_exists_and_covers_all_problem_statuses():
    src = _src("services/mini_app_api.py")
    assert "async def restrictions_problems(" in src
    # Фильтр — через единый классификатор, а не свой список статусов.
    body = src[src.index("async def restrictions_problems("):]
    body = body[:body.index("async def restrictions_remove_dead(")]
    assert "_erc.is_problem(" in body, "эндпоинт должен брать все проблемные статусы"
    assert "op_id" in body, "должна быть возможность разобрать конкретную проверку"
    assert '"id_text": str(ent_id)' in body
    snap = _src("tests/miniapp_routes_snapshot.txt")
    assert "GET /api/miniapp/restrictions/problems" in snap


def test_cleanup_screen_wired_in_frontend():
    js = _src("mini_app/screens/shield_check.js")
    assert "async function openRestrictionCleanup(" in js
    assert "/api/miniapp/restrictions/problems" in js
    assert "/api/miniapp/restrictions/remove_dead" in js
    assert "'s-restrcleanup'" in js and "push('s-restrcleanup')" in js
    assert "x.dead && x.id_text" in js and "const id = cb.value" in js


def test_catalogs_offer_cleanup():
    html = _src("mini_app/index.html")
    # Каталог каналов (меню «⋯ Ещё») и каталог ботов зовут уборку.
    assert html.count("openRestrictionCleanup(0)") >= 2, (
        "уборка проблемных должна быть и в каталоге каналов, и в каталоге ботов"
    )
