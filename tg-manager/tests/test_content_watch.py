"""Регрессия: Resource Compliance Scanner — чистый классификатор + проводка.

Сетевой скан тестируется интеграционно; здесь — агрегация вердиктов
content_safety в досье и guard'ы, что op_type compliance_scan диспетчеризован,
эндпоинт/route/UI подключены (иначе — мёртвая фича).

Граница фичи (важно проверить в коде): это ДЕТЕКТОР, а НЕ авто-жалоба —
исполнитель не должен постить/жаловаться/сносить.
"""
from __future__ import annotations

import os

from services.content_watch import classify_texts, CATEGORY_LABELS
from services import content_safety
from services import op_worker

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def test_classify_clean_when_no_hits():
    d = classify_texts([("заголовок", "Обычный канал про кошек"), ("сообщение #1", "Сегодня хорошая погода")])
    assert d["verdict"] == "clean"
    assert d["categories"] == []
    assert d["hits"] == []
    assert d["scanned"] == 2


def test_classify_skips_empty_texts():
    d = classify_texts([("a", ""), ("b", "   "), ("c", None)])
    assert d["scanned"] == 0 and d["verdict"] == "clean"


def test_classify_flags_prohibited_with_evidence():
    # Явный CSAM-паттерн из content_safety (аббревиатура + распространение).
    bad = "Продам cp детское, пишите в личку куплю продам"
    v = content_safety.scan_text(bad)
    if not v.blocked:
        # если детектор не ловит эту конкретную строку — берём заведомо сильный паттерн
        bad = "child porn"
        assert content_safety.scan_text(bad).blocked
    d = classify_texts([("сообщение #5", bad)])
    assert d["verdict"] == "prohibited"
    assert d["hits"] and d["hits"][0]["category"] in CATEGORY_LABELS
    # улика — цитата из текста, а не пусто
    assert d["hits"][0]["excerpt"]
    assert d["hits"][0]["label"] == "сообщение #5"


def test_category_labels_cover_detector_categories():
    assert content_safety.CATEGORY_CSAM in CATEGORY_LABELS
    assert content_safety.CATEGORY_TERROR in CATEGORY_LABELS


def test_op_endpoint_route_and_ui_wired():
    ow = _read("services/op_worker.py")
    assert op_worker.handler_for("compliance_scan") is not None and "_exec_compliance_scan(" in ow
    # исполнитель — детектор, НЕ авто-жалоба: не должен слать репорты/постить
    seg = ow[ow.index("async def _exec_compliance_scan"):ow.index("async def _exec_niche_growth_post")]
    assert "content_watch.scan_resource" in seg
    assert "ReportPeerRequest" not in seg and "send_message" not in seg
    api = _read("services/mini_app_api.py")
    assert "async def compliance_scan_submit" in api
    # Эндпоинт ставит op через operation_bus (рефактор с сырого INSERT на bus).
    _seg = api[api.index("async def compliance_scan_submit"):]
    _seg = _seg[:_seg.index("async def ", 10)]
    assert ".submit(" in _seg and '"compliance_scan"' in _seg
    assert 'add_post("/api/miniapp/compliance_scan", compliance_scan_submit)' in api
    ui = _read("mini_app/index.html")
    assert "submitComplianceScan" in ui and "/api/miniapp/compliance_scan" in ui
