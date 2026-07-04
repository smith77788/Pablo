"""Spintax-модуль Infragram.

Превращает обычный текст сообщения (сценарий) в spintax-шаблоны с помощью уже
подключённых к платформе LLM-провайдеров, проверяет их встроенным движком
``services.spintax_engine`` и умеет раскрывать готовые шаблоны в случайные
варианты.

Здесь только чистая логика: сетевой вызов LLM передаётся снаружи как
асинхронная функция ``complete(system, user) -> str``. Благодаря этому модуль
тестируется без aiogram/openai и без реального обращения к провайдерам.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Awaitable, Callable

from services.spintax_engine import Context, SpintaxEngine

log = logging.getLogger(__name__)

DEFAULT_SPIN_COUNT = 2
MAX_LITERAL_RUN = 2

# Асинхронный вызов LLM: принимает system+user промпты, возвращает текст ответа.
Completer = Callable[[str, str], Awaitable[str]]

# Служебные конструкции движка, которых в «чистом» спинтаксе быть не должно.
# ВАЖНО: обычная пунктуация (! ? # $ ~) допустима в тексте, поэтому её НЕ ловим —
# иначе получаем ложные срабатывания почти на каждом сообщении. Ловим только то,
# что реально нарушает правило «только { } |»: скобки [ ] и веса ::.
_DISALLOWED_PATTERN = re.compile(r"(?<!\\)(::|\[|\])")

# Общий движок с мягкими лимитами — потокобезопасен, кэширует шаблоны.
_engine = SpintaxEngine()


class SpintaxServiceError(Exception):
    """Ошибка генерации или разбора ответа LLM."""


def build_system_prompt(count: int) -> str:
    """Системный промпт с правилами качественного спинтакса."""
    return f"""Ты — профессиональный генератор spintax-шаблонов для рассылок. На
вход тебе дают ГОТОВЫЙ текст сообщения. Твоя задача — вернуть РОВНО {count}
spintax-версий ИМЕННО ЭТОГО текста.

❗ САМОЕ ГЛАВНОЕ ПРАВИЛО: ты НЕ придумываешь новый текст и НЕ переписываешь его.
Ты берёшь исходный текст КАК ЕСТЬ и просто вставляешь в него группы с
синонимами к отдельным словам и оборотам. Порядок слов, структура предложений,
факты, интонация и формулировки автора остаются точь-в-точь как в оригинале.

ФОРМАТ ГРУППЫ: одинарные фигурные скобки, ОДНА пара на группу.
Правильно: ОДНАКРАТНЫЕ скобки вокруг вариантов, разделённых вертикальной чёртой.
НЕПРАВИЛЬНО: двойные скобки — это ошибка. Никогда не ставь две скобки подряд.

- ПЕРВЫЙ вариант в каждой группе — это ИСХОДНОЕ слово/выражение из текста.
- Проверка: убери все скобки и оставь только первый вариант каждой группы —
  должен получиться ИСХОДНЫЙ текст слово в слово.
- Если синонима нет или он звучит криво — оставь слово без скобок.

ФОРМАТ:
Одинарные фигурные скобки для групп: варианты через вертикальную черту.
Пример формата: слово1 слово2 слово3
Спинтакс-формат: слово1 ВАРИАНТЫ_СЛОВА2 слово3
Где ВАРИАНТЫ_СЛОВА2 = opensym вариант1 черта вариант2 черта вариант3 closesym
opensym = одна открывающая фигурная скобка
closesym = одна закрывающая фигурная скобка

❗ ГЛАВНОЕ ПРАВИЛО КАЧЕСТВА: минимум 3 варианта на каждую группу.
Если к слову нет 3+ хороших синонимов — НЕ ставь группу, оставь слово как есть.

❗ ГЛАВНОЕ ПРАВИЛО ПРОВЕРКИ: проверяй КАЖДУЮ возможную комбинацию.
Если хотя бы одна комбинация нарушает грамматику или согласование — переделай группу.

ГЛАВНЫЕ ЗАПРЕТЫ (нарушение = брак):
- ТОЛЬКО на языке оригинала. Никаких иностранных слов.
- НЕ повторяй один и тот же вариант внутри группы.
- НЕ смешивай в одной группе части речи с разным управлением.
- НЕ меняй смысл. Каждое предложение значит ровно то же, что в оригинале.

Как делать сильный спин:
1. Ставь группы там, где есть уместные синонимы. ОБЯЗАТЕЛЬНО 3-6 вариантов.
2. Рандомизируй целые обороты, а не отдельные слова.
3. Строго согласуй род, число, падеж и окончания.
4. Пиши по-живому, как в обычной переписке.

Ответь СТРОГО в формате JSON: массив РОВНО из {count} строк, без markdown, без
пояснений. Каждая строка — один spintax-шаблон. НИКОГДА не используй двойные
скобки — только ОДИНАРНЫЕ фигурные скобки для каждой группы."""


def parse_spin_response(text: str) -> list[str]:
    """Достаёт список шаблонов из ответа LLM.

    Сначала пытается разобрать JSON-массив строк; если модель ответила не по
    формату — вытягивает строки, похожие на spintax (содержат ``{``, ``}`` и
    ``|``). Так модуль устойчив к «слабым» бесплатным моделям.
    """
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        cleaned = re.sub(r"^json\s*", "", cleaned, count=1, flags=re.IGNORECASE).strip()

    try:
        data = json.loads(cleaned)
        if isinstance(data, list):
            items = [item.strip() for item in data if isinstance(item, str) and item.strip()]
            if items:
                return items
    except json.JSONDecodeError:
        pass

    # Фолбэк: берём строки, которые выглядят как spintax-шаблоны.
    fallback: list[str] = []
    for line in cleaned.splitlines():
        candidate = line.strip().lstrip("-*0123456789.) ").strip()
        candidate = candidate.strip('"').strip()
        if "{" in candidate and "}" in candidate and "|" in candidate:
            fallback.append(candidate)
    if fallback:
        return fallback

    raise SpintaxServiceError("не удалось разобрать ответ модели как spintax")


def max_literal_word_run(template: str) -> int:
    """Наибольшее число подряд идущих слов вне групп ``{..}`` / ``[..]``."""
    best = 0
    depth = 0
    buf: list[str] = []
    i = 0
    n = len(template)
    while i < n:
        ch = template[i]
        if ch == "\\" and i + 1 < n:
            if depth == 0:
                buf.append(template[i + 1])
            i += 2
            continue
        if ch in "{[":
            if depth == 0 and buf:
                best = max(best, len(re.findall(r"\S+", "".join(buf))))
                buf = []
            depth += 1
            i += 1
            continue
        if ch in "}]":
            depth = max(0, depth - 1)
            i += 1
            continue
        if depth == 0:
            buf.append(ch)
        i += 1
    if buf:
        best = max(best, len(re.findall(r"\S+", "".join(buf))))
    return best


MIN_VARIANTS_PER_GROUP = 3


def count_group_variants(template: str) -> list[int]:
    """Подсчитывает количество вариантов в каждой группе {{...|...}}."""
    variants_per_group = []
    depth = 0
    group_start = -1
    i = 0
    n = len(template)
    while i < n:
        ch = template[i]
        if ch == "\\" and i + 1 < n:
            i += 2
            continue
        if ch == "{":
            depth += 1
            if depth == 1:
                group_start = i
                pipe_count = 0
        elif ch == "}":
            if depth == 1 and group_start >= 0:
                variants_per_group.append(pipe_count + 1)
                group_start = -1
            depth = max(0, depth - 1)
        elif ch == "|" and depth == 1:
            pipe_count += 1
        i += 1
    return variants_per_group


def check_minimum_variants(template: str) -> list[str]:
    """Проверяет что все группы содержат минимум 3 варианта."""
    errors = []
    variants = count_group_variants(template)
    for i, count in enumerate(variants):
        if count < MIN_VARIANTS_PER_GROUP:
            errors.append(
                f"группа #{i+1} содержит только {count} вариант(ов), "
                f"нужно минимум {MIN_VARIANTS_PER_GROUP}"
            )
    return errors


def find_disallowed_tokens(template: str) -> list[str]:
    """Служебные конструкции движка (``[ ] ::``), недопустимые в чистом спинтаксе.

    Обычную пунктуацию (``! ? # $ ~``) НЕ считаем ошибкой — она встречается в
    любом живом тексте.
    """
    return _DISALLOWED_PATTERN.findall(template)


def fix_double_brackets(template: str) -> str:
    """Заменяет двойные скобки {{ и }} на одинарные { и }.
    
    LLM иногда генерирует двойные скобки вместо одинарных.
    Эта функция исправляет такие ошибки.
    """
    result = template
    # Заменяем }} на } (только вне JSON-строк и不在 шаблонных строках)
    while '}}' in result:
        result = result.replace('}}', '}', 1)
    # Заменяем {{ на { (только в начале групп)
    while '{{' in result:
        result = result.replace('{{', '{', 1)
    return result


def quality_warnings(template: str) -> list[str]:
    """Мягкие предупреждения о качестве шаблона (не блокируют выдачу)."""
    warnings: list[str] = []
    run = max_literal_word_run(template)
    if run > MAX_LITERAL_RUN:
        warnings.append(f"подряд {run} нерандомизированных слов")
    tokens = find_disallowed_tokens(template)
    if tokens:
        warnings.append("лишние символы: " + ", ".join(sorted(set(tokens))))
    variant_errors = check_minimum_variants(template)
    if variant_errors:
        warnings.extend(variant_errors)
    if '}}' in template or '{{' in template:
        warnings.append("обнаружены двойные скобки — исправлено автоматически")
    return warnings


def is_valid_template(template: str) -> bool:
    """Проходит ли шаблон валидацию движка + проверку минимума вариантов."""
    if not _engine.validate(template).ok:
        return False
    variant_errors = check_minimum_variants(template)
    return len(variant_errors) == 0


def keep_valid_templates(templates: list[str]) -> list[str]:
    """Оставляет только синтаксически корректные шаблоны."""
    return [t for t in templates if is_valid_template(t)]


# Латиница (включая диакритику Latin-1: á, ê, ñ …). Кириллица сюда НЕ попадает.
_LATIN_RE = re.compile(r"[A-Za-zÀ-ÿ]")


def introduces_foreign_letters(template: str, script: str) -> bool:
    """True, если в шаблоне появилась латиница, которой не было в исходном тексте.

    Ловит типичные утечки слабых моделей: «práv», «mês», «feed». Если в исходном
    тексте латиница уже была (бренды, ссылки) — не фильтруем.
    """
    if _LATIN_RE.search(script or ""):
        return False
    return bool(_LATIN_RE.search(template))


def expand_template(template: str, *, seed: int | None = None) -> str:
    """Раскрывает шаблон в один случайный вариант."""
    return _engine.generate(template, Context(), seed=seed)


def expand_many(template: str, count: int, *, unique: bool = False) -> list[str]:
    """Раскрывает шаблон в ``count`` вариантов."""
    return _engine.generate_many(template, count, unique=unique)


async def generate_spins(
    script: str,
    *,
    complete: Completer,
    count: int = DEFAULT_SPIN_COUNT,
) -> list[str]:
    """Сгенерировать и отвалидировать spintax-шаблоны из обычного текста.

    :param script: исходный сценарий (обычный текст).
    :param complete: асинхронный вызов LLM ``(system, user) -> str``.
    :param count: сколько вариантов запросить.
    :returns: список валидных шаблонов (может быть короче ``count``).
    :raises SpintaxServiceError: пустой ввод, пустой/битый ответ модели, либо
        среди ответов не оказалось ни одного корректного шаблона.
    """
    if not script.strip():
        raise SpintaxServiceError("сценарий пуст")

    system = build_system_prompt(count)
    raw = await complete(system, script.strip())
    if not raw or not raw.strip():
        raise SpintaxServiceError("пустой ответ модели")

    templates = parse_spin_response(raw)
    # Авто-исправление двойных скобок
    templates = [fix_double_brackets(t) for t in templates]
    valid = keep_valid_templates(templates)
    if not valid:
        raise SpintaxServiceError("модель вернула некорректный spintax")

    # Фильтруем шаблоны с группами <3 вариантов — это главное правило качества.
    with_min_variants = [t for t in valid if not check_minimum_variants(t)]
    if with_min_variants:
        valid = with_min_variants
        log.info(
            "отфильтрованы шаблоны с <3 вариантами: осталось %d из %d",
            len(valid), len(templates),
        )

    # Отсеиваем шаблоны с чужой латиницей («práv», «mês») — если текст был
    # без латиницы. Но если после фильтра ничего не осталось — оставляем как есть,
    # чтобы не падать в ошибку из-за одной утечки.
    clean = [t for t in valid if not introduces_foreign_letters(t, script)]
    if clean:
        valid = clean

    if len(valid) < count:
        log.warning("получено %d валидных шаблонов из %d", len(valid), count)
    # Не больше запрошенного — модель иногда отдаёт лишние варианты.
    return valid[:count]
