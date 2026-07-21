"""Настройки уведомлений мини-аппа = единый источник правды (notification_settings).

Был второй источник правды: мини-апп сохранял тумблеры в platform_users.settings_json
(notif_ops/notif_error), а бот гейтил уведомления по таблице notification_settings
(op_complete/restriction) через notify_if_enabled → тумблеры мини-аппа были «тихим
успехом» (сохранялись, но ни на что не влияли). Фикс: save синхронизирует в
notification_settings (notif_ops→op_complete, notif_error→restriction+flood_warning),
get отражает реальное состояние оттуда же.
"""
from __future__ import annotations

import inspect
import re

from services import mini_app_api


SRC = inspect.getsource(mini_app_api)


def _fn_src(name):
    m = re.search(r"async def " + name + r"\(request.*?\n(.*?)\n    async def ",
                  SRC, re.DOTALL)
    assert m, f"{name} не найден"
    return m.group(1)


def test_save_syncs_to_notification_settings():
    body = _fn_src("user_settings_save")
    assert "INSERT INTO notification_settings" in body, "save должен писать в единый источник"
    # маппинг: notif_ops→op_complete, notif_error→restriction(+flood_warning)
    assert 'data.get("notif_ops"' in body and 'data.get("notif_error"' in body
    assert "op_complete=$2" in body and "restriction=$3" in body and "flood_warning=$3" in body


def test_save_preserves_other_bot_toggles():
    body = _fn_src("user_settings_save")
    # ON CONFLICT обновляет ТОЛЬКО три поля — не трогает new_user/position_change/deploy
    assert "ON CONFLICT(user_id) DO UPDATE SET op_complete" in body
    assert "new_user" not in body and "position_change" not in body


def test_get_reflects_real_notification_state():
    body = _fn_src("user_settings_get")
    assert "get_notification_settings" in body, "get должен читать реальный источник"
    assert 'settings["notif_ops"] = bool(_ns.get("op_complete"' in body
    assert 'settings["notif_error"] = bool(_ns.get("restriction"' in body


def test_notify_gate_reads_same_table():
    # notify_if_enabled действительно гейтит по notification_settings (source of truth)
    from database import db
    src = inspect.getsource(db.notify_if_enabled)
    assert "get_notification_settings" in src


def test_dead_toggles_removed_from_settings_ui():
    # lang(en)/utc_logs ничего не делали (UI только рус, логи флаг не читают) — убраны,
    # чтобы не было «мёртвых» настроек.
    from pathlib import Path
    html = (Path(__file__).resolve().parent.parent / "mini_app" / "index.html").read_text(encoding="utf-8")
    assert 'id="set-lang"' not in html, "мёртвый селектор языка должен быть убран"
    assert 'id="set-utc-logs"' not in html, "мёртвый тумблер UTC-логов должен быть убран"
