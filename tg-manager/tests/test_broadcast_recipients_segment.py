"""Честный предпросмотр рассылки: число получателей учитывает сегмент.

Баг: updateBcastRecip показывал ПОЛНОЕ число подписчиков даже при выборе сегмента
«Активным 7д/30д» → предпросмотр (и confirm) врал, хотя бэк слал по сегменту. Фикс:
эндпоинт /broadcast/recipients?bot_id&segment считает точное число тем же _seg_sql,
что create_broadcast/исполнитель; UI подтягивает его при смене сегмента (с гонка-гардом).
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

from services import mini_app_api

SRC = inspect.getsource(mini_app_api)
HTML = (Path(__file__).resolve().parent.parent / "mini_app" / "index.html").read_text(encoding="utf-8")


def test_recipients_endpoint_registered():
    assert 'add_get("/api/miniapp/broadcast/recipients", broadcast_recipients)' in SRC


def test_recipients_uses_segment_sql_and_owner_check():
    m = re.search(r"async def broadcast_recipients\(request.*?\n(.*?)\n    async def broadcast_resend",
                  SRC, re.DOTALL)
    assert m, "broadcast_recipients не найден"
    body = m.group(1)
    # тот же сегментный SQL, что в create_broadcast
    assert "active_7d" in body and "interval '7 days'" in body
    assert "active_30d" in body and "interval '30 days'" in body
    # владение ботом проверяется
    assert "added_by=$2" in body
    # считает bot_users с фильтром сегмента
    assert "FROM bot_users WHERE bot_id=$1 AND is_active=true" in body


def test_ui_fetches_segment_count():
    m = re.search(r"async function updateBcastRecip\(\)\s*\{(.*?)\n\}", HTML, re.DOTALL)
    assert m, "updateBcastRecip не найден"
    body = m.group(1)
    # при не-'all' сегменте тянем точное число с бэка
    assert "/api/miniapp/broadcast/recipients" in body
    assert "segment==='all'" in body  # для 'all' точное число уже есть локально
    # гонка-гард: игнорируем устаревший ответ
    assert "updateBcastRecip._t" in body
