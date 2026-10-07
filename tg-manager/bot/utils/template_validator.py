"""Template validation helpers.

Validates message templates (HTML syntax, length) and asset templates
(required fields, value formats per asset type).

Used by templates.py and asset_templates.py handlers.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


# ── Constants ────────────────────────────────────────────────────────────────────

MAX_TEXT_LENGTH = 4096   # Telegram message limit
MAX_NAME_LENGTH = 64
MAX_DESCRIPTION_LENGTH = 512
MAX_SHORT_DESC_LENGTH = 120
MAX_USERNAME_LENGTH = 32

USERNAME_PATTERN = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{3,31}$|^$")

# Simple HTML tag balance check
HTML_TAG_RE = re.compile(r"</?([a-zA-Z][a-zA-Z0-9]*)\s*[^>]*>")


# ── Result type ──────────────────────────────────────────────────────────────────

@dataclass
class ValidationResult:
    valid: bool = True
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def merge(self, other: "ValidationResult") -> "ValidationResult":
        self.valid = self.valid and other.valid
        self.errors.extend(other.errors)
        self.warnings.extend(other.warnings)
        return self


# ── Message template validation ──────────────────────────────────────────────────

def validate_message_template(name: str, text: str) -> ValidationResult:
    """Validate a message template before saving."""
    result = ValidationResult()

    # Name checks
    if not name or not name.strip():
        result.valid = False
        result.errors.append("Название не может быть пустым")
    elif len(name) > MAX_NAME_LENGTH:
        result.valid = False
        result.errors.append(f"Название слишком длинное (макс. {MAX_NAME_LENGTH} символов)")

    # Text checks
    if not text or not text.strip():
        result.valid = False
        result.errors.append("Текст шаблона не может быть пустым")
    else:
        if len(text) > MAX_TEXT_LENGTH:
            result.valid = False
            result.errors.append(
                f"Текст слишком длинный ({len(text)} симв., макс. {MAX_TEXT_LENGTH})"
            )

        # HTML tag balance
        tag_result = _check_html_balance(text)
        result.merge(tag_result)

    return result


# ── Asset template validation ────────────────────────────────────────────────────

def validate_asset_template(
    asset_type: str,
    name: str,
    template: dict,
) -> ValidationResult:
    """Validate an asset template before saving."""
    result = ValidationResult()

    # Name checks (common)
    if not name or not name.strip():
        result.valid = False
        result.errors.append("Название шаблона не может быть пустым")
    elif len(name) > MAX_NAME_LENGTH:
        result.valid = False
        result.errors.append(f"Название слишком длинное (макс. {MAX_NAME_LENGTH} символов)")

    # Template data checks (must be non-empty dict)
    if not template or not isinstance(template, dict):
        result.valid = False
        result.errors.append("Данные шаблона пусты или имеют неверный формат")
        return result

    # Type-specific validation
    if asset_type == "bot":
        result.merge(_validate_bot_template(template))
    elif asset_type == "channel":
        result.merge(_validate_channel_template(template))
    elif asset_type == "group":
        result.merge(_validate_group_template(template))
    elif asset_type == "post":
        result.merge(_validate_post_template(template))
    elif asset_type == "operation":
        result.merge(_validate_operation_template(template))
    else:
        result.valid = False
        result.errors.append(f"Неизвестный тип шаблона: {asset_type}")

    return result


# ── Per-type validators ──────────────────────────────────────────────────────────

def _validate_bot_template(t: dict) -> ValidationResult:
    result = ValidationResult()
    name = (t.get("name") or "").strip()
    desc = (t.get("description") or "").strip()
    short = (t.get("short_description") or "").strip()

    if not name:
        result.valid = False
        result.errors.append("Имя бота обязательно (первое поле до ;;;)")
    elif len(name) > MAX_NAME_LENGTH:
        result.warnings.append(f"Имя бота длинное ({len(name)} симв.)")

    if desc and len(desc) > MAX_DESCRIPTION_LENGTH:
        result.warnings.append(
            f"Описание бота длинное ({len(desc)} симв., рекомендовано до {MAX_DESCRIPTION_LENGTH})"
        )

    if short and len(short) > MAX_SHORT_DESC_LENGTH:
        result.warnings.append(
            f"Краткое описание длинное ({len(short)} симв., Telegram лимит ~{MAX_SHORT_DESC_LENGTH})"
        )

    return result


def _validate_channel_template(t: dict) -> ValidationResult:
    result = ValidationResult()
    title = (t.get("title") or "").strip()
    desc = (t.get("description") or "").strip()
    username = (t.get("username") or "").strip()

    if not title:
        result.valid = False
        result.errors.append("Название канала обязательно (первое поле до ;;;)")

    if username and not USERNAME_PATTERN.match(username):
        result.valid = False
        result.errors.append(
            f"Username канала «{username}» неверного формата. "
            "Должен быть: 5–32 символа, латиница, цифры, подчёркивание."
        )

    if desc and len(desc) > MAX_DESCRIPTION_LENGTH:
        result.warnings.append(f"Описание канала длинное ({len(desc)} симв.)")

    return result


def _validate_group_template(t: dict) -> ValidationResult:
    result = ValidationResult()
    title = (t.get("title") or "").strip()
    username = (t.get("username") or "").strip()

    if not title:
        result.valid = False
        result.errors.append("Название группы обязательно (первое поле до ;;;)")

    if username and not USERNAME_PATTERN.match(username):
        result.valid = False
        result.errors.append(
            f"Username группы «{username}» неверного формата. "
            "Должен быть: 5–32 символа, латиница, цифры, подчёркивание."
        )

    return result


def _validate_post_template(t: dict) -> ValidationResult:
    result = ValidationResult()
    text = (t.get("text") or "").strip()

    if not text:
        result.valid = False
        result.errors.append("Текст поста не может быть пустым")
    else:
        if len(text) > MAX_TEXT_LENGTH:
            result.valid = False
            result.errors.append(
                f"Текст поста слишком длинный ({len(text)} симв., макс. {MAX_TEXT_LENGTH})"
            )
        result.merge(_check_html_balance(text))

    return result


def _validate_operation_template(t: dict) -> ValidationResult:
    result = ValidationResult()
    op_type = (t.get("op_type") or "").strip()

    VALID_OP_TYPES = {"mass_publish", "bulk_join", "bulk_leave", "bulk_bot_edit"}

    if not op_type:
        result.valid = False
        result.errors.append("Тип операции обязателен (первое поле до ;;;)")
    elif op_type not in VALID_OP_TYPES:
        result.valid = False
        result.errors.append(
            f"Неверный тип операции «{op_type}». "
            f"Допустимые: {', '.join(sorted(VALID_OP_TYPES))}"
        )

    if op_type == "mass_publish":
        text = (t.get("text") or "").strip()
        if not text:
            result.valid = False
            result.errors.append("Текст публикации не может быть пустым")
        elif len(text) > MAX_TEXT_LENGTH:
            result.valid = False
            result.errors.append(f"Текст публикации слишком длинный ({len(text)} симв.)")

    elif op_type == "bulk_join":
        links = t.get("links") or []
        if not links:
            result.valid = False
            result.errors.append("Список ссылок для join не может быть пустым")
        elif not isinstance(links, list):
            result.valid = False
            result.errors.append("Список ссылок должен быть списком строк")

    elif op_type == "bulk_leave":
        channels = t.get("channels") or []
        if not channels:
            result.valid = False
            result.errors.append("Список каналов для leave не может быть пустым")
        elif not isinstance(channels, list):
            result.valid = False
            result.errors.append("Список каналов должен быть списком строк")

    elif op_type == "bulk_bot_edit":
        field = (t.get("field") or "").strip()
        value = (t.get("value") or "").strip()
        VALID_FIELDS = {"name", "desc", "short_desc"}
        if not field:
            result.valid = False
            result.errors.append("Поле для редактирования обязательно (второе поле до ;;;)")
        elif field not in VALID_FIELDS:
            result.valid = False
            result.errors.append(
                f"Неверное поле «{field}». Допустимые: {', '.join(sorted(VALID_FIELDS))}"
            )
        if not value:
            result.valid = False
            result.errors.append("Значение для редактирования обязательно (третье поле до ;;;)")

    return result


# ── HTML helpers ─────────────────────────────────────────────────────────────────

def _check_html_balance(text: str) -> ValidationResult:
    """Check that HTML tags are balanced. Returns warnings, not errors."""
    result = ValidationResult()
    tags = HTML_TAG_RE.findall(text)

    stack: list[str] = []
    singleton_tags = {"br", "hr", "img", "input", "meta", "link"}

    for tag in tags:
        if tag.lower() in singleton_tags:
            continue
        stack.append(tag)

    # Simple heuristic: check that open/close pairs roughly match
    open_tags: dict[str, int] = {}
    close_tags: dict[str, int] = {}

    for match in HTML_TAG_RE.finditer(text):
        full = match.group(0)
        tag = match.group(1).lower()
        if tag in singleton_tags:
            continue
        if full.startswith("</"):
            close_tags[tag] = close_tags.get(tag, 0) + 1
        else:
            open_tags[tag] = open_tags.get(tag, 0) + 1

    for tag, count in open_tags.items():
        closed = close_tags.get(tag, 0)
        if count > closed:
            result.warnings.append(
                f"Незакрытый тег <{tag}> (открыт {count} раз, закрыт {closed})"
            )
        elif closed > count:
            result.warnings.append(
                f"Лишний закрывающий тег </{tag}> (закрыт {closed} раз, открыт {count})"
            )

    return result


def replace_placeholders(template_text: str, variables: dict[str, str]) -> str:
    """Replace {{KEY}} placeholders with values. Unknown keys are left as-is."""
    result = template_text
    for key, value in variables.items():
        result = result.replace(f"{{{{{key}}}}}", value)
    return result


def list_placeholders(template_text: str) -> list[str]:
    """Extract all unique placeholder keys from template text."""
    pattern = re.compile(r"\{\{(\w+)\}\}")
    return list(dict.fromkeys(pattern.findall(template_text)))  # dedup preserving order
