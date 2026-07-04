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

DEFAULT_SPIN_COUNT = 5
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
    return f"""Ты — генератор spintax-шаблонов. На вход тебе дают обычный текст
сообщения (сценарий), а ты возвращаешь {count} максимально разных, максимально
рандомизированных spintax-версий этого текста.

Формат spintax:
- {{вариант1|вариант2|вариант3}} — при генерации программа случайно берёт один
  из вариантов внутри фигурных скобок;
- вложенность разрешена: {{сразу|в тот же {{момент|час}}}};
- внутри фигурных скобок пробелы возле {{ | }} не нужны;
- НИКАКИХ других спецсимволов, кроме {{ }} | использовать нельзя
  (запрещены [ ] # $ ~ ! ::).

Правила синонимизации:
1. Для каждого слова или выражения, у которого есть уместные синонимы, добавляй
   группу {{...|...|...}} из 3-6 близких по смыслу вариантов.
2. Если рандомизируешь слово с большой буквы — все варианты внутри тоже с
   большой буквы.
3. Следи за согласованием падежей и окончаний между соседними группами.
4. Пиши просто и по-живому, как в обычной переписке, без канцелярита и
   высокопарности.
5. Ни в одном раскрытии шаблона не должно оставаться подряд больше 2 неизменных
   (нерандомизированных) слов — если такое возможно, разбей их на группы.
6. Все {count} шаблонов должны заметно различаться по построению фраз, а не быть
   косметическими вариациями одного и того же текста.
7. Сохраняй язык, смысл, структуру обращения и эмодзи исходного сценария.

Ответь СТРОГО в формате JSON: массив из {count} строк, без markdown, без
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
    return valid
