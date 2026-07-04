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

# Символы движка, которых в «ручном» спинтаксе быть не должно (только { } |).
_DISALLOWED_PATTERN = re.compile(r"(?<!\\)(::|~|!|\$|#|\[|\])")

# Общий движок с мягкими лимитами — потокобезопасен, кэширует шаблоны.
_engine = SpintaxEngine()


class SpintaxServiceError(Exception):
    """Ошибка генерации или разбора ответа LLM."""


def build_system_prompt(count: int) -> str:
    """Системный промпт с правилами качественного спинтакса."""
    return f"""Ты — профессиональный генератор spintax-шаблонов для рассылок. На
вход тебе дают обычный текст сообщения (сценарий), а ты возвращаешь РОВНО {count}
разных, максимально рандомизированных spintax-версий ЭТОГО ЖЕ текста.

Что такое spintax:
- {{вариант1|вариант2|вариант3}} — при отправке программа случайно берёт один из
  вариантов внутри скобок. Пример: {{Сильная|Мощная|Крепкая}} {{любовь|страсть}}.
- Разрешена вложенность (рандом внутри рандома):
  {{сразу|в тот же {{момент|час}}}} — сначала выбирается «сразу» или «в тот же …»,
  затем внутри второго — «момент» или «час».
- Внутри скобок НЕ должно быть пробелов рядом с {{ | }}.
- Разрешены ТОЛЬКО символы {{ }} и |. Любые другие служебные знаки
  ([ ] # $ ~ ! ::) запрещены — это сломает шаблон.

ГЛАВНЫЕ ЗАПРЕТЫ (нарушение = брак):
- Пиши ТОЛЬКО на языке оригинала. НИ ОДНОГО слова из других языков
  (никаких mês, feed, post и т.п.).
- НЕ выдумывай несуществующих слов («достовольно», «без малой помехи» — брак).
  Только реальные, естественные слова. Сомневаешься в синониме — не ставь его.
- НЕ меняй смысл. Каждое предложение значит ровно то же, что в оригинале; новых
  фактов, тем и деталей не добавляй.
- Любой вариант внутри группы {{...}} должен быть взаимозаменяем: подставь любой —
  фраза остаётся грамотной, осмысленной и с правильным падежом/окончанием.

Как делать сильный спин:
1. Для КАЖДОГО слова и выражения, где есть уместные синонимы, ставь группу
   {{...|...|...}} — синонимов бери как можно больше (обычно 3-6), но только
   действительно подходящих по смыслу и склонению.
2. Рандомизируй не только отдельные слова, но и целые обороты и приветствия.
   Используй вложенность, где это уместно.
3. Если группа с большой буквы — все варианты внутри тоже с большой буквы.
4. Строго согласуй род, число, падеж и окончания между соседними группами и с
   остальным текстом.
5. Пиши просто и по-живому, как в обычной переписке. Без официоза и
   высокопарности («Не соблаговолите ли вы…» — плохо).
6. Стремись, чтобы подряд не оставалось больше 2 неизменных слов — но НЕ в ущерб
   смыслу: лучше 3 обычных слова, чем кривой синоним.
7. Если в исходном тексте есть эмодзи — заменяй их группой близких по настроению
   эмодзи, например {{😊|🙂|🙃|😌|🤗}}.
8. Оба шаблона должны заметно различаться построением фраз, но каждый обязан
   читаться как живое, грамотное сообщение.

Пример. Вход: «Здравствуйте! Недавно подписался на ваш канал.»
Хороший спин: {{Здравствуйте|Добрый день|Приветствую|Привет}}! {{Недавно|Не так
давно|Совсем недавно|На днях}} {{подписался|подписалась}} на {{ваш канал|вашу
группу|ваше сообщество}}.

Ответь СТРОГО в формате JSON: массив РОВНО из {count} строк, без markdown, без
пояснений и без нумерации. Каждая строка — один самостоятельный spintax-шаблон."""


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


def find_disallowed_tokens(template: str) -> list[str]:
    """Символы движка (``[ ] # $ ~ ! ::``), недопустимые в ручном спинтаксе."""
    return _DISALLOWED_PATTERN.findall(template)


def quality_warnings(template: str) -> list[str]:
    """Мягкие предупреждения о качестве шаблона (не блокируют выдачу)."""
    warnings: list[str] = []
    run = max_literal_word_run(template)
    if run > MAX_LITERAL_RUN:
        warnings.append(f"подряд {run} нерандомизированных слов")
    tokens = find_disallowed_tokens(template)
    if tokens:
        warnings.append("лишние символы: " + ", ".join(sorted(set(tokens))))
    return warnings


def is_valid_template(template: str) -> bool:
    """Проходит ли шаблон валидацию движка."""
    return _engine.validate(template).ok


def keep_valid_templates(templates: list[str]) -> list[str]:
    """Оставляет только синтаксически корректные шаблоны."""
    return [t for t in templates if is_valid_template(t)]


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
    valid = keep_valid_templates(templates)
    if not valid:
        raise SpintaxServiceError("модель вернула некорректный spintax")
    if len(valid) < count:
        log.warning("получено %d валидных шаблонов из %d", len(valid), count)
    # Не больше запрошенного — модель иногда отдаёт лишние варианты.
    return valid[:count]
