"""Сеть контента: видно, что ушло и почему упало, и сеть можно удалить.

Экран «🕸 Сеть контента» показывал список сетей с числом «В очереди: N» и
кнопкой «Цели». Очередь репостов (mesh_queue) с самого начала хранит каждую
запись: в какой канал, какой пост, ушло или упало и с каким текстом ошибки —
но увидеть этого было нельзя. Настройки сети нельзя было поправить, саму сеть
нельзя было удалить (только выключить), а отдельную цель — поставить на паузу.

Тест держит и скоуп: цель выключается через владельца её сети, иначе по
голому номеру цели можно было бы выключить чужую.
"""
from __future__ import annotations

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = open(os.path.join(ROOT, "mini_app", "index.html"), encoding="utf-8").read()
API = open(os.path.join(ROOT, "services", "mini_app_api.py"), encoding="utf-8").read()
SNAPSHOT = open(os.path.join(ROOT, "tests", "miniapp_routes_snapshot.txt"),
                encoding="utf-8").read()


def _fn(name: str) -> str:
    m = re.search(r"(?:async\s+)?function\s+" + re.escape(name) + r"\s*\(", HTML)
    assert m, f"функция {name} не найдена"
    i = HTML.index("{", m.end() - 1)
    depth = 0
    for j in range(i, len(HTML)):
        if HTML[j] == "{":
            depth += 1
        elif HTML[j] == "}":
            depth -= 1
            if depth == 0:
                return HTML[i:j + 1]
    raise AssertionError(name)


def _handler(name: str) -> str:
    i = API.index(f"async def {name}(request")
    j = API.index("\n    async def ", i + 10)
    return API[i:j]


def test_routes_exist():
    for r in ("GET /api/miniapp/content_mesh/{mesh_id}",
              "PUT /api/miniapp/content_mesh/{mesh_id}",
              "DELETE /api/miniapp/content_mesh/{mesh_id}",
              "PUT /api/miniapp/content_mesh/target/{target_id}/toggle"):
        assert r in SNAPSHOT, f"нет маршрута: {r}"


def test_detail_returns_the_repost_queue():
    h = _handler("content_mesh_detail")
    assert "mesh_queue" in h, "очередь репостов не отдаётся"
    assert "error_msg" in h, "причина падения репоста не видна"
    assert "target_channel" in h, "непонятно, в какой канал шёл репост"
    assert "_own_mesh(uid, mesh_id)" in h


def test_update_and_delete_are_owner_scoped():
    upd = _handler("content_mesh_update")
    assert "_own_mesh(uid, mesh_id)" in upd
    assert "WHERE id=$1 AND owner_id=$2" in upd
    dele = _handler("content_mesh_delete")
    assert "WHERE id=$1 AND owner_id=$2" in dele


def test_update_validates_the_delay():
    upd = _handler("content_mesh_update")
    assert "min_val=1, max_val=1440" in upd, "задержка репоста без границ"
    assert "Название сети обязательно" in upd


def test_target_toggle_is_scoped_through_its_mesh():
    h = _handler("content_mesh_target_toggle")
    assert "JOIN content_meshes" in h and "cm.owner_id=$2" in h, \
        "по номеру цели можно выключить чужую"


# ── экран ────────────────────────────────────────────────────────────────────

def test_mesh_row_opens_a_card():
    body = _fn("openContentMesh")
    assert "openMeshDetail(" in body, "сеть нельзя открыть"
    assert 'id="s-meshdetail"' in HTML


def test_card_shows_queue_and_errors():
    body = _fn("openMeshDetail")
    assert "d.queue" in body and "d.recent" in body, "очередь и история не показаны"
    assert "error_msg" in body, "причина ошибки не выводится"
    assert "MESH_Q_RU" in body, "статусы очереди уходят на экран по-английски"


def test_card_can_edit_pause_and_delete():
    body = _fn("openMeshDetail")
    for call in ("editMesh(", "toggleMeshFrom(", "deleteMesh(", "toggleMeshTarget("):
        assert call in body, f"на карточке нет действия: {call}"
    assert "async function deleteMesh(" in HTML
    assert "confirmDelete(" in _fn("deleteMesh"), "сеть удаляется без подтверждения"


def test_a_mesh_without_targets_says_so():
    lst = _fn("openContentMesh")
    assert "без получателей сеть ничего не делает" in lst, \
        "сеть без целей выглядит рабочей"
    card = _fn("openMeshDetail")
    assert "читает источник впустую" in card


def test_russian_labels():
    for gone in ("Мешей нет", "Новая Content Mesh", "Цели меша"):
        assert gone not in HTML, f"осталась старая подпись: {gone}"
    assert "Сетей контента нет" in HTML


def test_errors_offer_a_retry():
    for name in ("openContentMesh", "openMeshDetail", "loadMeshTargets"):
        assert re.search(r"errHtml\(errRu\(e\)\s*,", _fn(name)), \
            f"{name}: ошибка без «Повторить»"
