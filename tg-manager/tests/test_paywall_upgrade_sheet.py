"""Пейволл: отказ по подписке → экран апгрейда (конверсия), а не сухой тост.

Момент, когда бесплатный пользователь упирается в платную функцию — лучшая точка
конверсии. Раньше он получал тост «Требуется подписка» без ценности и без пути к
оформлению. Теперь любой 403-«подписка» маркируется в едином _err, а фронт
показывает экран апгрейда с кнопкой в биллинг.
"""
from __future__ import annotations

import json
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _err_source() -> str:
    """Тело def _err(...) целиком — до первой пустой строки после его
    return. НЕ фиксированное число символов: комментарии внутри функции
    (например, про redact_secrets) со временем растут, и жёсткое окно вида
    src[i:i+600] обрезает маркер пейволла раньше, чем до него доходит текст —
    тест начинает падать на ЗДОРОВОМ коде. Границей служит сама структура
    функции, а не подобранное когда-то число."""
    src = _read("services/mini_app_api.py")
    i = src.index("def _err(")
    j = src.index("\n\n", src.index("return _json_resp(body, status)", i))
    return src[i:j]


def test_err_marks_subscription_403_centrally():
    seg = _err_source()
    # маркер ставится в ЕДИНОМ месте (_err) для всех 20+ платных гейтов
    assert '"paywall"' in seg and "subscription_required" in seg
    assert "status == 403" in seg and "подписк" in seg


def test_err_does_not_mark_non_subscription_errors():
    # маркер строго под 403+подписка: обычные ошибки его не получают
    seg = _err_source()
    assert 'if status == 403 and msg and "подписк" in str(msg).lower():' in seg

def _err_body(msg: str, status: int) -> dict:
    """Ответ _err как разобранный JSON.

    Проверка выше держит АРХИТЕКТУРУ (маркер стоит именно в _err, в одном месте
    на все 20+ гейтов) и читает для этого исходник. Эта — держит ПОВЕДЕНИЕ: что
    _err действительно так отвечает. Вторая не зависит от текста исходника
    вообще, поэтому переживает любое переформатирование функции.
    """
    from services.mini_app_api import _err

    resp = _err(msg, status)
    return json.loads(resp.text)


def test_err_response_carries_paywall_marker():
    body = _err_body("Требуется подписка", 403)
    assert body["paywall"] is True
    assert body["code"] == "subscription_required"


def test_err_marks_paywall_regardless_of_wording():
    # гейты пишут отказ по-разному — маркер не должен зависеть от формулировки
    for msg in ("Требуется подписка", "нужна ПОДПИСКА уровня Pro",
                "Эта функция доступна по подписке"):
        assert _err_body(msg, 403).get("paywall") is True, msg


def test_err_response_has_no_marker_for_other_errors():
    # маркер строго под 403+подписка: обычные ошибки его не получают
    for msg, status in (("Требуется подписка", 400),   # та же причина, не 403
                        ("Нет доступа к каналу", 403),  # 403, но не подписка
                        ("Некорректный запрос", 400)):
        body = _err_body(msg, status)
        assert "paywall" not in body, (msg, status)
        assert "code" not in body, (msg, status)


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
