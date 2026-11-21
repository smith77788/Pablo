"""Накрутка: наборы настроек и последние настройки.

Частая работа — те же каналы, тот же разброс и то же число аккаунтов; форма
каждый раз открывалась пустой на «Просмотрах». Теперь набор (тип + все поля
формы + реакции) сохраняется под именем, а последний запуск подставляется
сам.
"""
from __future__ import annotations

import re

from tests.miniapp_source import miniapp_html, miniapp_source


def _fn(src: str, name: str) -> str:
    i = src.index(f"function {name}(")
    return src[i:i + 5000]


def test_presets_row_and_actions_exist():
    assert 'id="boostPresetRow"' in miniapp_html()
    ui = miniapp_source()
    for fn in ("renderBoostPresets", "useBoostPreset", "saveBoostPreset", "delBoostPreset"):
        assert f"function {fn}(" in ui, fn


def test_snapshot_covers_every_field_the_submit_reads():
    ui = miniapp_source()
    submit = _fn(ui, "submitBoost")
    read = set(re.findall(r"getElementById\('(boost\w+)'\)\.value", submit))
    fields = set(re.findall(r"'(boost\w+)'", ui.split("const BOOST_FIELDS", 1)[1][:600]))
    assert read and read <= fields, f"набор не помнит поля: {read - fields}"


def test_last_settings_saved_after_queue_and_restored_on_open():
    ui = miniapp_source()
    submit = _fn(ui, "submitBoost")
    i_api = submit.index("/api/miniapp/boost")
    assert submit.index("saveFormPrefs('boostLast'") > i_api, \
        "последние настройки пишутся только после того, как накрутка ушла в очередь"
    assert "loadFormPrefs('boostLast')" in _fn(ui, "openBoost")
