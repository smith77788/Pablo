"""Операторы поиска работали в боте и не работали в мини-аппе.

НАХОДКА (23.10.2026). `search_engine` понимает `@ник`, `#тег`, `company:` и
`phone:` — и эти операторы доступны ТОЛЬКО через поиск бота
(`bot/handlers/contacts_hub.py`). Список хаба собирал условие своим кодом в
`repository._segment_where` и об операторах не знал, а подсказки о них не было
ни в одном placeholder. То есть владелец, набравший `#vip` в единственном поле
поиска приложения, не находил никого: тот же тег, та же база, разный ответ.

Это та же жалоба, что закрывалась в самом `_segment_where` раньше («поиск в
БОТЕ по тегам ищет, а в списке нет»), только на уровень глубже: тогда к
условию добавили точный тег, а операторы остались в стороне.

Теперь разбор один: `search_engine.parse_operators` +
`build_operator_conditions` зовут и бот, и `_segment_where`, поэтому операторы
работают в списке, в срезе для массовых действий и в выгрузке — одним кодом.
Остаток строки `_segment_where` бьёт на слова своим правилом (у него проходят
и односимвольные), поэтому матчинг слов не менялся.

УДАЛЕНО: ручка GET /uch/search — те же семь колонок и теги, что у фильтра
списка, но своим условием и без пагинации, и её не звал ни один экран.
GET /uch/contacts/{id}/history — строгое подмножество ленты /timeline (тот же
contact_history), тоже без двери и с другим скоупом.

ОСТАВЛЕНО И ПОДКЛЮЧЕНО: GET /uch/spotlight. Это не дубль фильтра — движок
прощает опечатку (fuzzy-близость) и ранжирует попадания, больше такого в
продукте нет нигде, а двери у него не было. Теперь на пустом результате поиска
список показывает «Похожие контакты», и подсказка открывает ОДНУ карточку, а
не задаёт срез: расхождения «вижу одно — применяю массовое действие к другому»
не возникает.

ОБРАТНЫЙ КОНТРОЛЬ (прогонялся):
  1. вернуть `_segment_where` на разбор без операторов -> красные
     test_the_list_filter_understands_the_tag_operator,
     test_the_list_filter_understands_the_other_operators,
     test_the_operators_do_not_leak_into_the_words;
  2. убрать вызов contactSuggest из пустого состояния списка
     -> красный test_an_empty_search_offers_similar_contacts;
  3. вернуть на пустом списке единственный текст «Синхронизируйте аккаунты»
     -> красные test_an_empty_list_names_the_real_reason и
     test_an_empty_search_offers_similar_contacts (вместе с ветками уходит и
     вызов подсказок).
"""
from __future__ import annotations

from tests.miniapp_source import js_function, miniapp_html


def _where(search: str):
    from services.contacts_hub.repository import _segment_where
    return _segment_where(7, {"search": search})


def test_the_list_filter_understands_the_tag_operator():
    where, params, _ = _where("#vip")
    assert "= ANY(tags)" in where, "оператор #тег не дошёл до условия списка"
    assert "vip" in params, f"тег не попал в параметры: {params}"


def test_the_list_filter_understands_the_other_operators():
    where, params, _ = _where("@ivan company:acme phone:7999")
    assert "username ILIKE" in where, "@ник не дошёл до условия"
    assert "company ILIKE" in where, "company: не дошёл до условия"
    assert "%ivan%" in params and "%acme%" in params and "%7999%" in params, params


def test_the_operators_do_not_leak_into_the_words():
    """`#vip петров` — это тег И слово, а не слово «#vip»."""
    where, params, _ = _where("#vip петров")
    assert "%петров%" in params, "слово потерялось вместе с оператором"
    assert "%#vip%" not in params, (
        "оператор ушёл в поиск по словам: такой подстроки нет ни в одной колонке, "
        f"то есть запрос не нашёл бы никого: {params}")


def test_single_letter_words_still_match():
    """Операторы не должны менять матчинг слов заодно с собой."""
    _, params, _ = _where("я")
    assert "%я%" in params, "односимвольное слово перестало искаться"


def test_the_bot_and_the_hub_share_one_parser():
    import inspect

    from services.contacts_hub import repository
    src = inspect.getsource(repository._segment_where)
    assert "parse_operators" in src, "список разбирает операторы своим кодом"
    assert "build_operator_conditions" in src, (
        "условия операторов построены второй копией правил")


def test_the_search_box_mentions_the_operators():
    html = miniapp_html()
    assert "id=\"contactSearch\"" in html
    i = html.index("id=\"contactSearch\"")
    box = html[i - 200:i + 200]
    assert "@ник" in box and "#тег" in box, (
        "операторы есть, и о них негде узнать: " + box[-200:])


def test_an_empty_search_offers_similar_contacts():
    body = js_function("loadContacts")
    assert "contactSuggest(q)" in body, (
        "на пустом поиске подсказки по опечатке не зовутся")
    sug = js_function("contactSuggest")
    assert "uch/spotlight" in sug, "подсказки берутся не из движка опечаток"
    assert "openContactDetail" in sug, (
        "подсказка не открывает карточку, то есть это снова срез")


def test_an_empty_list_names_the_real_reason():
    """«Синхронизируйте аккаунты» на пустом поиске — это неправда."""
    body = js_function("loadContacts")
    assert "Ничего не найдено" in body, "пустой поиск не называет себя"
    assert "В этом срезе нет контактов" in body, "пустой срез не называет себя"
    # Совет про синхронизацию должен остаться только в последней ветке: он
    # верен ровно тогда, когда ни поиска, ни фильтра нет.
    i = body.index("empty('📇'")
    assert "} else {" in body[i - 60:i], (
        "совет про синхронизацию остался безусловным: " + body[i - 60:i])


def test_the_duplicate_search_doors_are_gone():
    src = open("services/mini_app_uch.py", encoding="utf-8").read()
    snap = open("tests/miniapp_routes_snapshot.txt", encoding="utf-8").read()
    assert "GET /api/miniapp/uch/search\n" not in snap, "вторая ручка поиска вернулась"
    assert "GET /api/miniapp/uch/contacts/{contact_id}/history\n" not in snap, (
        "вторая лента истории вернулась")
    assert "GET /api/miniapp/uch/spotlight\n" in snap, (
        "подсказки по опечатке снова без двери")
    assert "uch_spotlight" in src
