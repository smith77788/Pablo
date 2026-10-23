"""Воронка CRM: у фильтра по стадии появились числа, и считает их один код.

НАХОДКА (22.10.2026). Хаб умел ФИЛЬТР по стадии — шесть чипов «Лид»,
«Контакт», «Предложение», «Переговоры», «Выиграно», «Проиграно», — и ни
одного числа рядом. Чтобы понять, где лежат сделки, владелец нажимал по
очереди все шесть и считал глазами по списку.

Посчитать это умели ДВЕ функции, и обе были мертвы:
  * `get_crm_pipeline` сортировала стадии по CASE с именами, которых в
    contact_crm не бывает ('prospect', 'closed_won', 'closed_lost'), — её
    «порядок воронки» был бы случайным;
  * `get_crm_stats` считала то же самое без порядка вообще.
Ни одну не звала ни одна ручка. Осталась одна, `pipeline()`.

ТРЕТЬЯ ЧАСТЬ НАХОДКИ — ЗАПИСЬ. У `contact_crm.stage` нет ни CHECK, ни enum
(schema_v148), а `upsert_crm` писал туда то, что пришло с экрана. Любая
строка ложилась в базу и тихо выпадала из всего, что о стадиях знает:
сигнал виртуального слоя (STAGE_SIGNAL) на неё не срабатывает, чип фильтра
её не покажет, воронка посчитает отдельной стадией без подписи. Теперь
стадия проверяется по STAGE_LABELS и отказ приходит по-русски.
"""
from __future__ import annotations

import re

from tests.miniapp_source import js_function, miniapp_html, py_function


def test_one_vocabulary_of_stages_for_the_whole_project():
    """Разные списки стадий — это молча неработающий сигнал и пустая воронка."""
    from services import intent_sensor, virtual_layer
    from services.contacts_hub.crm_engine import STAGE_LABELS

    assert tuple(STAGE_LABELS) == tuple(intent_sensor.VALID_STAGES), (
        "список стадий CRM разошёлся с intent_sensor.VALID_STAGES")
    assert set(STAGE_LABELS) == set(virtual_layer.STAGE_SIGNAL), (
        "стадия без сигнала виртуального слоя: смена стадии ничего не поднимет")


def test_the_screen_chips_use_the_same_keys():
    html = miniapp_html()
    from services.contacts_hub.crm_engine import STAGE_LABELS

    chips = set(re.findall(r'class="chip contact-stage" data-s="(\w+)"', html))
    assert chips == set(STAGE_LABELS), (
        f"чипы стадий на экране {sorted(chips)} не совпадают с сервером "
        f"{sorted(STAGE_LABELS)}: фильтр и воронка показали бы разное")


def test_the_chips_show_counts_from_the_server():
    html = miniapp_html()
    assert "refreshStageCounts" in html, "числа на чипах стадий никто не ставит"
    body = js_function("refreshStageCounts")
    assert "/api/miniapp/uch/crm/pipeline" in body, "числа берутся не с сервера"
    assert "s.label" in body, (
        "подпись чипа не с сервера: два списка подписей разойдутся на первой "
        "же правке")
    assert "by_currency" in body, "суммы по валютам не читаются"
    assert "разных валютах" in body, (
        "рубли с долларами складываются в одно «итого» — красиво и неверно")
    assert "refreshStageCounts()" in js_function("loadContacts"), (
        "числа не обновляются вместе со списком")


def test_the_funnel_is_computed_once_and_shows_every_stage():
    fn = py_function("services/contacts_hub/crm_engine.py", "pipeline")
    assert "STAGE_LABELS.items()" in fn, (
        "пустые стадии выпадут из ответа, и воронка будет с дырой в середине")
    assert "GROUP BY stage, currency" in fn, "суммы не разложены по валютам"
    assert "owner_id = $1" in fn, "воронка считает не по владельцу"

    import services.contacts_hub.crm_engine as ce
    for dead in ("get_crm_pipeline", "get_crm_stats"):
        assert not hasattr(ce, dead), (
            f"{dead} вернулась: воронку снова считают два разных запроса")


def test_an_unknown_stage_is_refused_on_write():
    fn = py_function("services/contacts_hub/crm_engine.py", "upsert_crm")
    assert "STAGE_LABELS" in fn, "стадия пишется без проверки по списку"
    assert "BadStage" in fn, "отказ не отличить от сбоя сервера"
    handler = py_function("services/mini_app_uch.py", "uch_crm_upsert")
    assert "except ValueError" in handler, (
        "отказ по стадии уйдёт пятисоткой, то есть «попробуйте позже»")
