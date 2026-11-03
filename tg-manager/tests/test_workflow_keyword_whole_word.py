"""Триггер «Сообщение со словом» ловит слово целиком, а не кусок слова.

ЧТО БЫЛО. `trigger_matches` искал слово как подстроку: сценарий на «да»
запускался на «когда» и «правда», на «нет» — на «интернет», на «цена» — на
«оценка». Человек, спросивший «когда доставка?», получал ответ на «да».

ЧТО ТЕПЕРЬ. Слово — целиком (без регистра и «ё/е»), фраза — как фраза,
«слово*» — по началу слова, чтобы ловить русские окончания.
"""
from services.workflow_runtime import keyword_hit, trigger_matches


def _ev(text):
    return {"event_type": "message.received", "data": {"text": text}}


def _wf(value):
    return {"trigger_type": "keyword", "trigger_value": value}


def test_a_word_inside_another_word_does_not_fire():
    assert not trigger_matches(_wf("да"), _ev("Когда будет доставка?"))
    assert not trigger_matches(_wf("нет"), _ev("Подключите интернет"))
    assert not trigger_matches(_wf("цена"), _ev("Какая оценка?"))


def test_the_whole_word_fires_in_any_case_and_with_punctuation():
    assert trigger_matches(_wf("да"), _ev("Да!"))
    assert trigger_matches(_wf("цена, купить"), _ev("Хочу КУПИТЬ, сколько?"))
    assert keyword_hit("ещё", "А есть еще?")


def test_a_phrase_and_a_prefix():
    assert keyword_hit("сколько стоит", "Скажите, сколько стоит курс?")
    assert not keyword_hit("сколько стоит", "сколько это стоит")
    assert keyword_hit("скидк*", "Есть скидки?")
    assert not keyword_hit("скидк*", "проскидка")
    assert not keyword_hit(" , *", "что угодно")
