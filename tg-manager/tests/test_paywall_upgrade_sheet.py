"""Пейволл: отказ по подписке → экран апгрейда (конверсия), а не сухой тост.

Момент, когда бесплатный пользователь упирается в платную функцию — лучшая точка
конверсии. Раньше он получал тост «Требуется подписка» без ценности и без пути к
оформлению. Теперь любой 403-«подписка» маркируется в едином _err, а фронт
показывает экран апгрейда с кнопкой в биллинг.
"""
from __future__ import annotations

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def test_err_marks_subscription_403_centrally():
    src = _read("services/mini_app_api.py")
    i = src.find("def _err(")
    seg = src[i:i + 600]
    # маркер ставится в ЕДИНОМ месте (_err) для всех 20+ платных гейтов
    assert '"paywall"' in seg and "subscription_required" in seg
    assert "status == 403" in seg and "подписк" in seg


def test_err_does_not_mark_non_subscription_errors():
    # маркер строго под 403+подписка: обычные ошибки его не получают
    src = _read("services/mini_app_api.py")
    i = src.find("def _err(")
    seg = src[i:i + 600]
    assert 'if status == 403 and msg and "подписк" in str(msg).lower():' in seg


def test_frontend_shows_paywall_on_marked_403():
    html = _read("mini_app/index.html")
    assert "function showPaywall" in html and "function closePaywall" in html
    # api() детектит маркер и зовёт showPaywall
    assert "_j.paywall" in html or "subscription_required" in html
    assert "showPaywall(msg)" in html
    # апгрейд ведёт в биллинг
    assert "openBilling()" in html
    # у брошенной ошибки есть флаг, чтобы не дублировать UI где не нужно
    assert "e.paywall = true" in html
