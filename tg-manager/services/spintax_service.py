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

import difflib
import json
import logging
import os
import re
from typing import Awaitable, Callable

from services.spintax_engine import Context, SpintaxEngine

log = logging.getLogger(__name__)

DEFAULT_SPIN_COUNT = 2
MAX_LITERAL_RUN = 2

# Насколько «скелет» шаблона (первые варианты групп) должен совпадать с исходным
# текстом, чтобы считать, что модель СПИНТАКСИЛА текст, а не переписала его.
# Переопределяется переменной окружения SPIN_MIN_PRESERVE (0..1).
try:
    MIN_PRESERVE_RATIO = float(os.getenv("SPIN_MIN_PRESERVE", "0.6"))
except ValueError:
    MIN_PRESERVE_RATIO = 0.6

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
- Проверка (обязательна!): убери все скобки и оставь только первый вариант каждой
  группы — должен получиться ИСХОДНЫЙ текст СЛОВО В СЛОВО. Мысленно сделай это.
- НЕ переставляй слова местами и не меняй порядок частей предложения. «его
  читаешь» остаётся «его читаешь», а не «читаешь его».
- НЕ меняй тон на официальный: «Целесообразно», «имеются ли», «по прошествии
  времени» — брак, если этого не было в оригинале.
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
- ТОЛЬКО на языке оригинала. Никаких иностранных слов (mês, feed, práv).
- НЕ выдумывай и не слепляй слова: «сновавно», «достовольно» — брак.
- НЕ повторяй один и тот же вариант внутри группы: {{недавно|недавно}} — брак.
- НЕ смешивай в одной группе части речи с разным управлением (предлог и без
  предлога): «следишь апвоут» — брак, нужно «следишь за апвоутом».
- Согласуй местоимение с окончанием: с «вы» → «вы зарегистрированы», «вы сидите»
  (не «вы зарегистрирован», не «вы сидишь»). Рандомизируешь «ты|вы» — глагол тоже
  идёт группой с обеими формами.
- НЕ вставляй кривые связки «на то, что», «на факт, что» вместо простого «что».
- НЕ меняй смысл. Каждое предложение значит ровно то же, что в оригинале.
- НЕ дублируй смысл соседних слов. Если в тексте уже есть «недавно», НЕ ставь
  рядом ещё группу «{{совсем недавно|на днях}}» — выйдет «недавно … недавно».
  Одна мысль рандомизируется ОДИН раз.
- КАЖДЫЙ вариант группы должен грамотно читаться с ЛЮБЫМ вариантом соседних
  групп. «{{у вас получается|вы}}» рядом с «{{подписались|подписаны}}» даёт «у вас
  получается подписались» — БРАК. Проверяй крайние комбинации.

Как делать сильный спин:
1. Ставь группы там, где есть уместные синонимы. ОБЯЗАТЕЛЬНО 3-6 вариантов.
2. Рандомизируй целые обороты, а не отдельные слова.
3. Строго согласуй род, число, падеж и окончания.
4. Пиши по-живому, как в обычной переписке.
5. Лучше МЕНЬШЕ групп, но чтобы ВСЕ комбинации читались грамотно, чем много
   групп с кривыми сочетаниями.

Ответь СТРОГО в формате JSON: массив РОВНО из {count} строк, без markdown, без
пояснений. Каждая строка — один spintax-шаблон. НИКОГДА не используй двойные
скобки — только ОДИНАРНЫЕ фигурные скобки для каждой группы."""


def build_synonym_prompt(count: int) -> str:
    """Промпт для сбора синонимов (шаблон собирается кодом, не моделью).

    Модель НЕ пишет спинтакс и НЕ трогает текст — она только подбирает синонимы к
    словам/оборотам. Это исключает вставку новых слов, перестановку и переписывание.
    """
    return f"""Тебе дают ГОТОВЫЙ текст сообщения. Не переписывай его и ничего в нём
не меняй. Твоя единственная задача — подобрать синонимы к отдельным словам и
оборотам, которые РЕАЛЬНО есть в этом тексте.

Верни СТРОГО JSON такого вида:
{{"variants": [ [ {{"orig": "<точная подстрока из текста>", "syn": ["синоним1", "синоним2", "синоним3"]}}, ... ], ... ]}}

Правила:
- В "variants" ровно {count} набора (наборы отличаются подбором синонимов).
- "orig" — это ТОЧНАЯ подстрока из исходного текста (слово или короткий оборот),
  скопированная буква в букву. НЕ придумывай слов, которых нет в тексте.
- "syn" — 2-5 синонимов, которые можно подставить ВМЕСТО orig, и фраза останется
  грамотной и с тем же смыслом. Следи за падежом, родом, числом и управлением
  (предлогами). Если orig с большой буквы — синонимы тоже с большой.
- Пиши синонимы ТОЛЬКО на языке текста, без иностранных слов и выдумок.
- Бери как можно больше слов и оборотов, но НЕ перекрывай их между собой.
- Если к слову нет 2+ хороших синонимов — просто не включай его.
- Разные наборы (variants) должны отличаться выбором синонимов.

Никакого текста кроме JSON."""


def _clean_synonyms(orig: str, syns: list, script: str) -> list[str]:
    """Чистит список синонимов: без дублей, латиницы, служебных скобок; регистр."""
    out: list[str] = []
    seen = {orig.strip().lower()}
    orig_cap = orig[:1].isupper()
    for raw in syns:
        s = str(raw or "").strip()
        if not s:
            continue
        if any(ch in s for ch in "{}|[]"):
            continue
        if introduces_foreign_letters(s, script):
            continue
        key = s.lower()
        if key in seen:
            continue
        seen.add(key)
        # Согласуем регистр первой буквы с исходным словом.
        if orig_cap and s[:1].islower():
            s = s[:1].upper() + s[1:]
        elif not orig_cap and s[:1].isupper():
            s = s[:1].lower() + s[1:]
        out.append(s)
        if len(out) >= 6:
            break
    return out


def assemble_template(script: str, replacements: list) -> str:
    """Собирает spintax-шаблон из ИСХОДНОГО текста и синонимов (без переписывания).

    Оборачивает найденные в тексте подстроки в ``{orig|син1|син2}``, остальной
    текст остаётся дословно. Гарантирует: текст сохранён 1-в-1, порядок не изменён,
    лишних слов нет, первый вариант каждой группы = исходное слово.
    """
    # Оставляем только замены с достаточным числом синонимов для группы ≥3.
    cleaned: list[tuple[str, list[str]]] = []
    for rep in replacements:
        if not isinstance(rep, dict):
            continue
        orig = str(rep.get("orig") or "").strip()
        if not orig:
            continue
        syns = _clean_synonyms(orig, rep.get("syn") or [], script)
        if len(syns) < MIN_VARIANTS_PER_GROUP - 1:
            continue
        cleaned.append((orig, syns))

    result: list[str] = []
    cursor = 0
    n = len(script)
    guard = 0
    while cursor < n and guard < 10_000:
        guard += 1
        best_idx: int | None = None
        best: tuple[str, list[str]] | None = None
        for orig, syns in cleaned:
            idx = script.find(orig, cursor)
            if idx >= 0 and (best_idx is None or idx < best_idx):
                best_idx = idx
                best = (orig, syns)
        if best is None or best_idx is None:
            result.append(script[cursor:])
            break
        orig, syns = best
        result.append(script[cursor:best_idx])
        result.append("{" + "|".join([orig] + syns) + "}")
        cursor = best_idx + len(orig)
    else:
        result.append(script[cursor:])
    return "".join(result)


def parse_synonym_response(raw: str, count: int) -> list[list[dict]]:
    """Разбирает ответ модели в список наборов замен ``[{orig, syn}, ...]``."""
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        cleaned = re.sub(r"^json\s*", "", cleaned, count=1, flags=re.IGNORECASE).strip()
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise SpintaxServiceError(f"модель вернула не-JSON ответ: {exc}") from exc

    if isinstance(data, dict) and isinstance(data.get("variants"), list):
        variants = data["variants"]
    elif isinstance(data, list) and data and isinstance(data[0], list):
        variants = data
    elif isinstance(data, list):  # один плоский список замен
        variants = [data]
    else:
        raise SpintaxServiceError("не удалось разобрать синонимы из ответа модели")

    result: list[list[dict]] = []
    for variant in variants:
        if not isinstance(variant, list):
            continue
        reps = [r for r in variant if isinstance(r, dict) and r.get("orig")]
        if reps:
            result.append(reps)
    if not result:
        raise SpintaxServiceError("модель не вернула синонимов")
    return result


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


def _first_alt(inner: str) -> str:
    """Первая альтернатива до верхнеуровневого ``|`` внутри содержимого группы."""
    depth = 0
    i = 0
    n = len(inner)
    while i < n:
        ch = inner[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        elif ch == "|" and depth == 0:
            return inner[:i]
        i += 1
    return inner


def first_option_render(template: str) -> str:
    """«Скелет» шаблона: раскрытие, где в каждой группе взят ПЕРВЫЙ вариант.

    Если модель честно спинтаксила текст (первый вариант = исходное слово), то
    скелет совпадает с исходным сообщением.
    """

    def render(s: str) -> str:
        out: list[str] = []
        i = 0
        n = len(s)
        while i < n:
            ch = s[i]
            if ch == "\\" and i + 1 < n:
                out.append(s[i + 1])
                i += 2
                continue
            if ch == "{":
                depth = 1
                j = i + 1
                while j < n and depth > 0:
                    c = s[j]
                    if c == "\\":
                        j += 2
                        continue
                    if c == "{":
                        depth += 1
                    elif c == "}":
                        depth -= 1
                        if depth == 0:
                            break
                    j += 1
                inner = s[i + 1 : j]
                out.append(render(_first_alt(inner)))
                i = j + 1
                continue
            if ch == "}":
                i += 1
                continue
            out.append(ch)
            i += 1
        return "".join(out)

    return render(template)


_WORD_RE = re.compile(r"\w+", re.UNICODE)


def text_similarity(a: str, b: str) -> float:
    """Похожесть двух текстов по последовательности слов (0..1)."""
    ta = _WORD_RE.findall(a.lower())
    tb = _WORD_RE.findall(b.lower())
    if not ta and not tb:
        return 1.0
    if not ta or not tb:
        return 0.0
    return difflib.SequenceMatcher(None, ta, tb).ratio()


def preserves_original(template: str, script: str, threshold: float = MIN_PRESERVE_RATIO) -> bool:
    """True, если скелет шаблона достаточно близок к исходному тексту.

    Отсекает случаи, когда модель переписала сообщение заново вместо того, чтобы
    спинтаксить именно присланный текст.
    """
    return text_similarity(first_option_render(template), script) >= threshold


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
    script = script.strip()
    if not script:
        raise SpintaxServiceError("сценарий пуст")

    # Новый подход: модель отдаёт ТОЛЬКО синонимы, а шаблон собираем кодом из
    # исходного текста. Это by construction сохраняет текст 1-в-1 (без вставок,
    # перестановок и переписывания) — первый вариант каждой группы = ваше слово.
    system = build_synonym_prompt(count)
    raw = await complete(system, script)
    if not raw or not raw.strip():
        raise SpintaxServiceError("пустой ответ модели")

    variant_sets = parse_synonym_response(raw, count)

    templates: list[str] = []
    seen: set[str] = set()
    for replacements in variant_sets:
        template = assemble_template(script, replacements)
        # Сборка ничего не добавила (мало синонимов) — пропускаем.
        if not template or template == script:
            continue
        if not is_valid_template(template):
            continue
        if template in seen:
            continue
        seen.add(template)
        templates.append(template)

    if not templates:
        raise SpintaxServiceError(
            "не удалось подобрать синонимы к тексту — попробуйте ещё раз"
        )

    if len(templates) < count:
        log.info("собрано %d шаблонов из %d наборов", len(templates), len(variant_sets))
    return templates[:count]
