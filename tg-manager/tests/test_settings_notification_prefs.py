"""Мини-апп управлял 2 из 4 реальных уведомлений — два были недоступны.

ЖАЛОБА ПОЛЬЗОВАТЕЛЯ: «недостающие функции и настройки».

ЧТО БЫЛО. Таблица `notification_settings` (schema_v31) хранит ПЯТЬ преференций:
`op_complete`, `restriction`, `flood_warning`, `new_user`, `position_change`. Все
их читает `notify_if_enabled` перед отправкой. Мини-апп же управлял только двумя
тумблерами (op_complete и restriction+flood_warning). `new_user` («новый
подписчик») и `position_change` («позиция в поиске изменилась») были ЗАШИТЫ в
True без всякого способа выключить: если бота засыпало́ подписчиками, пользователь
получал поток уведомлений и не мог его заглушить из мини-аппа.

Плюс тумблеры показывали ЛОКАЛЬНЫЙ кэш, а не состояние бэкенда: смена настройки
через бота не отражалась в мини-аппе — тумблер «врал».

Проверено на живой БД: выключение «Новые подписчики» реально глушит
`notify_if_enabled(pref="new_user")`.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
API = (ROOT / "services" / "mini_app_api.py").read_text(encoding="utf-8")
HTML = (ROOT / "mini_app" / "index.html").read_text(encoding="utf-8")


def _handler(name: str) -> str:
    m = re.search(rf"async def {name}\(request.*?\n    async def ", API, re.DOTALL)
    assert m, f"хендлер {name} не найден"
    return m.group(0)


def test_backend_get_overlays_all_four_prefs():
    body = _handler("user_settings_get")
    for pref in ("notif_ops", "notif_error", "notif_new_user", "notif_position"):
        assert f'"{pref}"' in body, f"GET не отдаёт {pref} — тумблер не с чем синхронизировать"


def test_backend_save_maps_new_prefs_to_columns():
    body = _handler("user_settings_save")
    assert "new_user=$4" in body and "position_change=$5" in body, (
        "новые тумблеры не пишутся в реальные колонки — были бы тихим успехом"
    )


def test_save_does_not_clobber_when_client_omits_them():
    """Старый клиент, не знающий про новые поля, НЕ должен сбрасывать их в дефолт
    при каждом сохранении — иначе включённый в боте флаг слетал бы молча."""
    body = _handler("user_settings_save")
    assert '"notif_new_user" in data or "notif_position" in data' in body, (
        "нужна проверка присутствия полей: без неё старый клиент затрёт настройку"
    )
    assert body.count("INSERT INTO notification_settings") == 2, (
        "должно быть две ветки: полная (с новыми полями) и совместимая (без них)"
    )


def test_prefs_are_real_columns():
    """Колонки обязаны существовать в схеме, иначе SAVE упадёт на INSERT."""
    schema = (ROOT / "schema_v31.sql").read_text(encoding="utf-8")
    m = re.search(r"CREATE TABLE IF NOT EXISTS notification_settings \((.*?)\)", schema, re.DOTALL)
    assert m
    cols = m.group(1)
    assert "new_user" in cols and "position_change" in cols


def test_frontend_has_the_two_new_toggles():
    assert 'id="set-notif-newuser"' in HTML, "нет тумблера «Новые подписчики»"
    assert 'id="set-notif-position"' in HTML, "нет тумблера «Изменение позиций»"


def test_frontend_saves_new_toggles():
    m = re.search(r"async function saveSettings\(\)\s*\{.*?\n\}", HTML, re.DOTALL)
    assert m
    body = m.group(0)
    assert "notif_new_user" in body and "notif_position" in body, (
        "тумблеры на экране, но не уходят на сервер — снова тихий успех"
    )


def test_frontend_toggles_reflect_backend_not_local_cache():
    """Тумблеры обязаны показывать состояние сервера — иначе смена через бота
    в мини-аппе невидима, и тумблер врёт."""
    m = re.search(r"async function openSettings\(\)\s*\{.*?\n\}", HTML, re.DOTALL)
    assert m
    body = m.group(0)
    # После получения ответа бэкенда все четыре тумблера переустанавливаются.
    tail = body.split("const backend = await api")[-1]
    for tog in ("set-notif-ops", "set-notif-error", "set-notif-newuser", "set-notif-position"):
        assert tog in tail, f"{tog} не синхронизируется с бэкендом"
