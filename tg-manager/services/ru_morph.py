"""Склонение русских топонимов — родительный и предложный падежи.

Зачем: генератор названий подставляет город в шаблон, и без склонения на выходе
получается «Работа в Москва» / «Новости города Самара». Это не косметика — при
1500 объектах одинаково сломанная грамматика становится подписью сетки:
человек так не пишет, а модерация и пользователи это видят сразу.

Подход сознательно консервативный: правило применяется только там, где оно
надёжно (продуктивные окончания русских топонимов), иначе слово возвращается
без изменений. Ошибка «не просклонял» безобидна (именительный читается
нормально), ошибка «просклонял неправильно» — заметна и хуже исходной.

Полноценная морфология (pymorphy) сюда не тянется намеренно: лишняя тяжёлая
зависимость ради одной подстановки, которая всё равно нуждается в списке
исключений для топонимов.
"""

from __future__ import annotations

import re

_CYRILLIC_RE = re.compile(r"[а-яёА-ЯЁ]")

# Буквы, которых нет в русском алфавите: их наличие означает, что слово
# украинское/белорусское (Київ, Львів, Магілёў). Русские правила там не
# применимы — «Київа» звучит хуже, чем несклоняемое «Київ».
_NON_RUSSIAN_CYRILLIC = frozenset("іїєґўІЇЄҐЎ")

# Шипящие и заднеязычные: после них в род. падеже пишется «и», а не «ы»
# (Калуга → Калуги, не «Калугы»), и «-ом» вместо «-ем» в предложном.
_HUSH_VELAR = frozenset("гкхжшчщ")

_VOWELS = frozenset("аеёиоуыэюя")
_CONSONANTS = frozenset("бвгджзклмнпрстфхцчшщ")

# Топонимы, которые не склоняются вовсе (заимствованные, аббревиатуры,
# оканчивающиеся на гласную не по русской модели).
_INDECLINABLE = frozenset(
    {
        "сочи",
        "тбилиси",
        "баку",
        "хельсинки",
        "осло",
        "токио",
        "чикаго",
        "монако",
        "торонто",
        "сан-паулу",
    }
)

# Слова-связки внутри дефисных названий: если встречаются, склоняется ПЕРВАЯ
# часть (Ростов-на-Дону → Ростове-на-Дону), иначе последняя (Санкт-Петербург →
# Санкт-Петербурге).
_HYPHEN_LINKERS = frozenset({"на", "в", "у", "при", "под"})


def has_cyrillic(text: str) -> bool:
    return bool(_CYRILLIC_RE.search(text or ""))


def is_russian_declinable(text: str) -> bool:
    """Кириллица без украинских/белорусских букв — только тогда склоняем."""
    if not has_cyrillic(text):
        return False
    return not any(ch in _NON_RUSSIAN_CYRILLIC for ch in text)


def _match_case(source: str, result: str) -> str:
    """Сохранить регистр первой буквы исходного слова."""
    if not result:
        return result
    if source[:1].isupper():
        return result[0].upper() + result[1:]
    return result


def _is_adjectival(word: str) -> bool:
    """Прилагательное-определение в составном топониме (Нижний Новгород)."""
    return word.lower().endswith(("ий", "ый", "ой", "ая", "яя", "ое", "ее", "ые", "ие"))


def _decline_adjective(word: str, case: str) -> str:
    low = word.lower()
    # Множественное число: и род., и предл. дают «-ых/-их» (Великие Луки →
    # Великих Луках). Проверяем ДО единственного, иначе «-ие» съест правило «-е».
    if low.endswith(("ые", "ие")):
        stem = low[:-2]
        return _match_case(word, stem + ("ых" if low.endswith("ые") else "их"))
    if low.endswith(("ая",)):
        stem, end = low[:-2], ("ой" if case == "gen" else "ой")
    elif low.endswith("яя"):
        stem, end = low[:-2], ("ей" if case == "gen" else "ей")
    elif low.endswith("ий"):
        stem = low[:-2]
        if case == "gen":
            end = "ого" if stem[-1:] in _HUSH_VELAR else "его"
        else:
            end = "ом" if stem[-1:] in _HUSH_VELAR else "ем"
    elif low.endswith("ый"):
        stem, end = low[:-2], ("ого" if case == "gen" else "ом")
    elif low.endswith("ой"):
        stem, end = low[:-2], ("ого" if case == "gen" else "ом")
    elif low.endswith(("ое", "ее")):
        return word  # среднего рода — оставляем как есть
    else:
        return word
    return _match_case(word, stem + end)


def _decline_noun(word: str, case: str) -> str:
    """Просклонять одиночное существительное-топоним. Не уверены — как есть."""
    low = word.lower()
    if low in _INDECLINABLE:
        return word
    if len(low) < 3:
        return word

    # -ия → -ии (Россия → России, Анапия). Единая форма для обоих падежей.
    if low.endswith("ия"):
        return _match_case(word, low[:-1] + "и")

    # Топонимы на -ово/-ево/-ино/-ыно и прочие на -о/-е: строгая норма их
    # склоняет (в Иванове), но несклоняемая форма давно общепринята и никогда
    # не читается как ошибка. Выбираем безопасный вариант.
    if low.endswith(("о", "е", "у", "ю", "э")):
        return word

    # Множественное число (Химки, Люберцы, Мытищи, Чебоксары).
    # Предложный надёжен: основа + -ах/-ях. Родительный — НЕТ: там беглая
    # гласная (Химки → Химок, Люберцы → Люберец), правила без словаря не
    # выводятся, а «Химк» хуже несклоняемого «Химки». Поэтому род. не трогаем.
    if low.endswith(("ы", "и")):
        if case == "gen":
            return word
        stem = low[:-1]
        # «-ы» — всегда твёрдая основа → «-ах» (Чебоксары → Чебоксарах).
        # «-и» — «-ах» после шипящих/заднеязычных (Мытищи → Мытищах,
        # Химки → Химках), иначе мягкая основа → «-ях».
        if low.endswith("ы") or stem[-1:] in _HUSH_VELAR:
            return _match_case(word, stem + "ах")
        return _match_case(word, stem + "ях")

    # -а → -ы/-и (род.) либо -е (предл.): Москва → Москвы / Москве.
    if low.endswith("а"):
        stem = low[:-1]
        if case == "gen":
            return _match_case(word, stem + ("и" if stem[-1:] in _HUSH_VELAR else "ы"))
        return _match_case(word, stem + "е")

    # -я → -и (род.) / -е (предл.): Кемля → Кемли / Кемле.
    if low.endswith("я"):
        stem = low[:-1]
        return _match_case(word, stem + ("и" if case == "gen" else "е"))

    # -ь → -и в обоих падежах: Тверь → Твери, Казань → Казани, Пермь → Перми.
    if low.endswith("ь"):
        return _match_case(word, low[:-1] + "и")

    # -й → -я / -е: Ишимбай → Ишимбая / Ишимбае.
    if low.endswith("й"):
        return _match_case(word, low[:-1] + ("я" if case == "gen" else "е"))

    # Согласная на конце — самая массовая модель: Саратов, Омск, Краснодар.
    if low[-1] in _CONSONANTS:
        return _match_case(word, low + ("а" if case == "gen" else "е"))

    return word


def _decline_phrase(name: str, case: str) -> str:
    """Просклонять словосочетание: определения + главное слово."""
    words = name.split()
    if not words:
        return name
    if len(words) == 1:
        return _decline_word(words[0], case)

    head_idx = len(words) - 1  # главное слово в русских топонимах — последнее
    head = _decline_word(words[head_idx], case)
    if head == words[head_idx]:
        # Главное слово не склонилось (несклоняемое или ненадёжный случай) —
        # определения тоже оставляем: «Великих Луки» хуже, чем «Великие Луки».
        return name

    out: list[str] = []
    for i, w in enumerate(words):
        if i == head_idx:
            out.append(head)
        elif _is_adjectival(w):
            out.append(_decline_adjective(w, case))
        else:
            out.append(w)
    return " ".join(out)


def _decline_word(word: str, case: str) -> str:
    """Одно слово (может быть дефисным)."""
    if "-" not in word:
        if _is_adjectival(word):
            return _decline_adjective(word, case)
        return _decline_noun(word, case)

    parts = word.split("-")
    # Ростов-на-Дону: есть предлог-связка → склоняем первую часть.
    if any(p.lower() in _HYPHEN_LINKERS for p in parts):
        parts[0] = _decline_word(parts[0], case)
        return "-".join(parts)
    # Санкт-Петербург, Йошкар-Ола: склоняем последнюю часть.
    parts[-1] = _decline_word(parts[-1], case)
    return "-".join(parts)


def genitive(name: str) -> str:
    """Родительный падеж: «Новости города {Самары}», «жители {Москвы}».

    Не кириллица или неопознанная модель → возвращается исходное имя.
    """
    name = (name or "").strip()
    if not name or not is_russian_declinable(name):
        return name
    return _decline_phrase(name, "gen")


def prepositional(name: str) -> str:
    """Предложный падеж: «Работа в {Москве}», «Что происходит в {Сочи}».

    Не кириллица или неопознанная модель → возвращается исходное имя.
    """
    name = (name or "").strip()
    if not name or not is_russian_declinable(name):
        return name
    return _decline_phrase(name, "loc")
