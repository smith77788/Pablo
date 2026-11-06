"""Экран, скрывший сбой, выглядит рабочим — и владелец ищет несуществующую причину.

Обход на пятисотках (`MINIAPP_FAIL=1`) задаёт один вопрос: сказал ли экран, что
сервер ответил ошибкой. До 06.11.2026 вопрос не задавался вовсе — признак
ошибки в этом режиме был слеп (самопроверка проверялась на живом экране,
который сам уже в состоянии ошибки), и три экрана скрывали сбой молча:

* «Настройки» — молчаливый откат к ЛОКАЛЬНОМУ кэшу. Рядом в коде написано,
  зачем завели запрос к серверу: кэш врал, когда настройки меняли через бота.
  Хуже молчания было сохранение: кнопка отправляла кэш и значения по умолчанию
  поверх настроек, которых владелец не видел.
* «Передача подарков» — пустота вместо сохранённых получателей. Передача
  необратима, и ник пришлось бы вводить руками.
* «Массовое оформление» — «?» вместо числа доступных аккаунтов. «?» не
  сообщение: дальше заказывают операцию, не зная её размера.
"""
from __future__ import annotations

from tests.miniapp_source import js_function


def test_settings_say_when_they_did_not_load():
    body = js_function("openSettings")
    assert "set-err" in body and "openSettings()" in body, (
        "сбой настроек снова молчит — экран выглядит рабочим")
    assert "dataset.settingsLoaded" in body, (
        "без отметки о загрузке сохранение затрёт настройки с сервера")


def test_settings_refuse_to_save_what_was_never_loaded():
    body = js_function("saveSettings")
    assert "settingsLoaded" in body, (
        "кнопка «Сохранить» отправит кэш поверх настроек, которых не видели")
    i, j = body.index("settingsLoaded"), body.index("api('/api/miniapp/settings'")
    assert i < j, "замок стоит после отправки — то есть не стоит"


def test_the_gift_recipients_say_when_they_did_not_load():
    body = js_function("gtLoadSaved")
    assert "errHtml(" in body and "retryCall('gtLoadSaved')" in body, (
        "пустой список получателей у необратимой передачи — тихая ложь")


def test_the_profile_setter_says_more_than_a_question_mark():
    body = js_function("openProfileSetter")
    assert "setterErr" in body, "«?» вместо числа аккаунтов — не сообщение"
    assert "openProfileSetter()" in body, "повторить запрос нечем"
