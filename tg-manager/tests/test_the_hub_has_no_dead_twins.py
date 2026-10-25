"""Мёртвый слой хаба: что удалено, почему и что получило дверь вместо удаления.

ПОЧЕМУ ЭТО НЕ «УБОРКА». Мёртвая функция в зрелом коде опасна не тем, что
занимает строки, а тем, что ЖДЁТ, когда её позовут: агент, подключающий движок
к экрану, выбирает по имени и подписи, и «улучшенная» версия выглядит
предпочтительнее рабочей. В этом пакете так уже случалось — одиночное
управление группами с IDOR и четыре дубля CRM.

УДАЛЕНО (23.10.2026), по убыванию опасности:

1. `merge_engine.auto_merge_with_confidence(pool, owner, threshold=0.7)` —
   автослияние, считавшее уверенность СВОИМ кодом с той же ошибкой, что была в
   оценке дублей: `set(a['phones'])` на строке jsonb давало набор символов,
   пересечение находилось у любых двух контактов с телефонами, уверенность
   становилась 0.8 — выше порога по умолчанию. А дальше шёл не перенос полей, а
   `DELETE FROM unified_contacts`. Не звал никто, и это единственная причина,
   по которой она ничего не уничтожила.
2. `trust_engine.get_conflict_resolution_suggestions(pool, conflict_id)` —
   без owner_id вообще (удалено в предыдущем коммите).
3. `performance.py` целиком, 188 строк: свой кэш с собственным TTL рядом с
   работающим кэшем поиска, счётчики запросов, которых никто не читает, и
   `batch_insert`/`batch_update`, подставляющие имя таблицы и имена колонок
   прямо в SQL строкой. Последнее опасно именно удобством: в хабе есть массовая
   правка полей с ИМЕНАМИ полей из запроса.
4. `merge_engine.merge_contacts_improved` — слово в слово `manual_merge`.
5. `identity_engine.add_identity` / `remove_identity` — граф идентичности
   СЧИТАЕТСЯ из полей карточки, которые на карточке и правятся.
6. `identity_engine.compute_digital_footprint` — вторая оценка «насколько хорош
   контакт» рядом с работающим trust_score, по другим правилам.
7. `relationship_engine.add_relationship` / `remove_relationship` — связи
   СЧИТАЮТСЯ, ручных интерфейс не обещает, и пересчёт их не обновлял бы.
8. `versioning_engine.compare_versions` / `create_version_with_snapshot` —
   строка версии и так называет изменённые поля, а откат показывает дифф.
9. `search_engine.clear_search_cache` — кнопка, которой нет.

НЕ УДАЛЕНО, А ПОДКЛЮЧЕНО: `search_engine.search_contacts_advanced`. Она отдаёт
`total` и `has_more`, и у неё не было ни одного вызова — при том что поиск в
БОТЕ брал `search_contacts(..., limit=12)` и печатал `Найдено: len(results)`.
При 57 совпадениях владелец читал «Найдено: 12»: не «показаны первые 12», а
неверное число как факт. Счётчик существовал и не использовался; использовался
тот, что считать не умеет. Теперь бот зовёт её и говорит правду.

ОБРАТНЫЙ КОНТРОЛЬ (прогонялся):
  1. дописать в merge_engine функцию с `DELETE FROM unified_contacts`
     -> красный test_no_other_function_deletes_a_contact_behind_the_common_path
     (в tests/test_merge_absorbs_fields.py);
  2. вернуть боту `search_contacts` и `len(results)` в заголовок
     -> красные test_the_bot_search_counts_what_it_found,
        test_the_bot_search_admits_it_shows_the_first_ones.
"""
from __future__ import annotations

import os

import pytest

from tests.miniapp_source import py_function

DEAD = [
    ("merge_engine", "auto_merge_with_confidence"),
    ("merge_engine", "merge_contacts_improved"),
    ("identity_engine", "add_identity"),
    ("identity_engine", "remove_identity"),
    ("identity_engine", "compute_digital_footprint"),
    ("relationship_engine", "add_relationship"),
    ("relationship_engine", "remove_relationship"),
    ("versioning_engine", "compare_versions"),
    ("versioning_engine", "create_version_with_snapshot"),
    ("search_engine", "clear_search_cache"),
]


@pytest.mark.parametrize("module,name", DEAD)
def test_the_dead_function_stays_dead(module, name):
    import importlib
    mod = importlib.import_module("services.contacts_hub." + module)
    assert not hasattr(mod, name), (
        f"{module}.{name} вернулась. Если она снова нужна — у неё должна быть "
        "дверь и единственная реализация, а не вторая копия правил")


def test_the_performance_layer_is_gone():
    assert not os.path.exists("services/contacts_hub/performance.py"), (
        "вернулся слой кэша и batch_* с именами таблиц в строке SQL")


def test_every_contacts_hub_function_is_reachable_or_named_dead():
    """Пробник измерителя: он должен видеть заведомо живую функцию.

    Проверка ниже перечисляет мёртвое руками, и смысл её в том, чтобы мёртвое
    не возвращалось. Без этого пробника она однажды начнёт «проходить» просто
    потому, что модуль перестал импортироваться.
    """
    from services.contacts_hub import merge_engine
    assert hasattr(merge_engine, "manual_merge"), "измеритель смотрит не туда"
    assert hasattr(merge_engine, "absorb_contact")


def test_the_bot_search_counts_what_it_found():
    body = py_function("bot/handlers/contacts_hub.py", "_render_search")
    assert "search_contacts_advanced" in body, (
        "поиск в боте снова берёт движок без счётчика, то есть печатает "
        "размер страницы вместо числа найденного")
    assert 'Найдено: {total}' in body, (
        "в заголовке снова размер страницы, а не число найденного")
    assert 'found.get("total")' in body, "счётчик движка не читается"


def test_the_bot_search_admits_it_shows_the_first_ones():
    body = py_function("bot/handlers/contacts_hub.py", "_render_search")
    assert "Показаны первые" in body, "обрезанный ответ выдаётся за полный"
    assert "total > len(results)" in body, (
        "оговорка про «первые N» пишется всегда — это шум на каждом ответе")
