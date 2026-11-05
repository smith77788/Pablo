"""Посты канала не повторяют друг друга ни началом, ни концовкой, ни оборотами.

Жалоба владельца (05.11.2026, канал «Свіжі Новини України»): каждый пост
начинался «<Город>, схоже, …», заканчивался «Чекаємо на подальші…», а внутри
кочевали «креативний спосіб», «вирішив додати трохи». Канал читался как
штамповка. Защита от повтора при этом была, но мерила другое: почти
дословную копию (тройки слов ≥ 60%) и совпадение ПЕРВЫХ ПЯТИ слов подряд.
Города разные — первые слова разные, общих троек мало, и обе проверки молчали.

Теперь:
* редактор (review_text) отклоняет пост, повторяющий приём начала или
  концовки и обороты недавних постов, и называет конкретные слова — с ними
  автор переписывает пост на следующей попытке;
* задание модели заранее перечисляет использованные начала, концовки и
  затёртые обороты и назначает каждому посту свой приём начала и концовки;
* факты новости (слова из текста источника) повтором не считаются.

Тексты ниже — с экрана владельца.
"""
from __future__ import annotations

import asyncio

from services import channel_admin as ca
from services import channel_brain as cb

RECENT = [
    "Рівненський район, схоже, знову вирішив додати трохи яскравих фарб у вечірню "
    "палітру. Повітряна тривога, та ще й \"жовтий рівень\".\n\nЦе, мабуть, щоб ніхто не "
    "заснув раніше часу. Або, можливо, це креативний спосіб нагадати, що життя в Україні "
    "ніколи не буває нудним.\n\nЧекаємо на подальші оновлення, а поки що – зберігаємо "
    "спокій і перевіряємо, чи не забули парасольку.",
    "Київ, схоже, готується до чергового етапу \"енергетичної оптимізації\". Мер Кличко, "
    "як завжди, не дає засумувати.\n\nЧекаємо на подальші креативні рішення від міської влади.",
    "У Запоріжжі сьогодні може бути \"гаряче\". Моніторингові канали попереджають про "
    "загрозу удару по мостах.\n\nМожливо, це просто черговий креативний спосіб нагадати про "
    "свою присутність. Залишаємося пильними і чекаємо на офіційні підтвердження.",
]

TEMPLATED = (
    "У Сарнах, схоже, вирішили додати трохи гострих відчуттів у буденність. Вчора на "
    "вулиці Європейській водій збив пенсіонерку.\n\nМожливо, наступного разу він "
    "подивиться у дзеркала. Чекаємо на офіційні підтвердження."
)
FRESH = (
    "Сарни, 4 листопада: водій кросовера Hyundai Kona, рухаючись заднім ходом по вулиці "
    "Європейській, травмував 70-річну жінку.\n\nПоліція відкрила провадження, постраждала "
    "в лікарні. Свідків просять зателефонувати на 102."
)


def test_the_templated_post_is_caught_on_all_three_counts():
    r = cb.style_repeats(TEMPLATED, RECENT)
    assert "схоже" in r["opening"], "начало «<місто>, схоже, …» не поймано"
    assert "чекаємо" in r["closing"], "концовка «Чекаємо на …» не поймана"
    assert r["phrases"], "кочующие обороты не пойманы"
    reasons = cb.style_repeat_reasons(TEMPLATED, RECENT)
    assert len(reasons) == 3 and all("«" in x for x in reasons), (
        "замечание обязано назвать слова — иначе автору нечего исправлять")


def test_a_fresh_factual_post_passes():
    assert cb.style_repeat_reasons(FRESH, RECENT) == []


def test_facts_from_the_source_are_not_style():
    # «Повітряна тривога» и «Київ» — предмет новости, а не приём автора.
    src = "Київ: Кличко попередив про можливий блекаут. Повітряна тривога у Києві."
    post = "Київ може залишитися без світла: Кличко попередив про блекаут. Повітряна тривога триває."
    assert cb.style_repeats(post, RECENT, allow_text=src)["phrases"] == []
    assert cb.style_repeats(post, RECENT, allow_text=src)["opening"] == []


def test_the_old_check_was_blind_to_it():
    # Контроль: прежняя мерка этот пост пропускала — новая проверка не дубль её.
    old = cb.repetition_check(TEMPLATED, RECENT)
    assert not old["is_duplicate"] and not old["opening_repeat"]


def test_the_prompt_names_what_to_avoid_and_assigns_a_move():
    _, user = ca.build_post_prompt({"title": "Новини", "topic": "новини України"},
                                   pillar="Новини", recent_texts=RECENT)
    assert "РАЗНООБРАЗИЕ" in user
    assert "схоже" in user.split("начинались недавние посты")[1][:400]
    assert "Затёртые обороты" in user
    assert any(m in user for m in ca._OPENING_MOVES), "посту не назначен приём начала"
    assert any(m in user for m in ca._CLOSING_MOVES), "посту не назначен приём концовки"


def test_a_batch_gets_a_different_move_for_every_post():
    _, user = ca.build_post_prompt({"title": "Новини"}, pillar="Новини", recent_texts=RECENT,
                                   tasks=[("Новини", ""), ("Новини", ""), ("Новини", "")])
    used = [m for m in ca._OPENING_MOVES if m in user]
    assert len(used) == 3, f"в пакете приёмы начала повторяются: {used}"


def test_the_editor_rejects_it(monkeypatch):
    """review_text — тот гейт, по которому решается автопубликация."""
    from services import editorial_review

    async def _clean(*a, **k):
        return cb.EditorialVerdict(ok=True, needs_review=False, reasons=[])

    monkeypatch.setattr(editorial_review, "review_draft", _clean)
    ctx = {"profile": {}, "recent": RECENT, "rival_names": []}
    bad = asyncio.run(ca.review_text(None, 1, 2, ctx, TEMPLATED))
    good = asyncio.run(ca.review_text(None, 1, 2, ctx, FRESH))
    assert any("начало повторяет" in r for r in bad), bad
    assert good == [], good
