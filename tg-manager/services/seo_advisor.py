"""SEO-советник: оценка находимости объекта (канал/бот/группа) в поиске Telegram.

Telegram-поиск ранжирует по релевантности заголовка/@username/описания запросу и
по размеру. Свои объекты под это никто не оптимизировал. Здесь — чистые
эвристики: скор 0–100 + конкретные проблемы и рекомендации. Без сети — только
анализ строк, поэтому детерминирован и тестируется без Telegram.
"""
from __future__ import annotations

import re

_EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF←-⇿⬀-⯿]")
_WORD = re.compile(r"[^\W\d_]{3,}", re.UNICODE)


def _emoji_count(s: str) -> int:
    return len(_EMOJI.findall(s or ""))


def _keywords(title: str, description: str) -> set[str]:
    text = f"{title} {description}".lower()
    return {w for w in _WORD.findall(text)}


def analyze(title: str = "", username: str = "", description: str = "",
            target_keywords: list[str] | None = None) -> dict:
    """Вернуть {score, issues, tips, keyword_hit}. Чем выше score, тем находимее."""
    title = (title or "").strip()
    username = (username or "").strip().lstrip("@")
    description = (description or "").strip()
    tk = [k.strip().lower() for k in (target_keywords or []) if k.strip()]

    score = 100
    issues: list[str] = []
    tips: list[str] = []

    def penalize(pts, issue, tip):
        nonlocal score
        score -= pts
        issues.append(issue)
        if tip:
            tips.append(tip)

    # ── Username ──
    if not username:
        penalize(25, "Нет @username", "Задайте короткий читаемый @username — без него объект почти не находится.")
    else:
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", username):
            penalize(10, "Username нестандартный", "5–32 символа, латиница/цифры/подчёркивание, начинать с буквы.")
        if len(username) > 20:
            penalize(5, "Username длинный", "Короткий username запоминается и лучше вводится.")
        if re.search(r"\d{3,}", username):
            penalize(6, "Много цифр в username", "Цифровые хвосты выглядят как сетка и хуже запоминаются.")

    # ── Title ──
    if not title:
        penalize(20, "Пустой заголовок", "Заголовок — главный сигнал релевантности. Вставьте ключевое слово.")
    else:
        if len(title) < 3:
            penalize(10, "Слишком короткий заголовок", "Добавьте ключевое слово ниши.")
        if len(title) > 40:
            penalize(6, "Длинный заголовок", "Держите до ~40 символов — обрезается в выдаче.")
        letters = re.sub(r"[^A-Za-zА-Яа-я]", "", title)
        if letters and letters == letters.upper() and len(letters) > 4:
            penalize(6, "ЗАГОЛОВОК КАПСОМ", "Капс читается как спам — обычный регистр ранжируется лучше.")
        if _emoji_count(title) > 2:
            penalize(6, "Много эмодзи в заголовке", "1–2 эмодзи максимум — эмодзи-стаффинг снижает доверие.")
        if not _WORD.findall(title):
            # только символы/цифры/эмодзи — поиск ранжирует по СЛОВАМ, не по значкам
            penalize(8, "Заголовок без слов", "Добавьте ключевое слово ниши словами — по значкам и цифрам канал не находят.")

    # ── Description ──
    if not description:
        penalize(15, "Нет описания", "Опишите нишу ключевыми словами — описание индексируется.")
    elif len(description) < 60:
        penalize(8, "Короткое описание", "60+ символов с ключами и призывом к действию.")
    if description and not re.search(r"(t\.me/|@[A-Za-z])", description):
        tips.append("Добавьте в описание ссылку/@упоминание связанных каналов (кросс-линковка).")

    # ── Target keywords ──
    keyword_hit = {}
    if tk:
        haystack = f"{title} {description}".lower()
        for k in tk:
            keyword_hit[k] = k in haystack
        missed = [k for k, v in keyword_hit.items() if not v]
        if missed:
            penalize(min(20, 6 * len(missed)),
                     f"Ключи не в тексте: {', '.join(missed)}",
                     "Вставьте целевые ключи в заголовок и описание.")

    score = max(0, min(100, score))
    if not tips and score >= 90:
        tips.append("Хорошо оптимизировано — можно отслеживать позиции по ключам.")
    grade = "green" if score >= 75 else "amber" if score >= 50 else "red"
    return {"score": score, "grade": grade, "issues": issues, "tips": tips,
            "keyword_hit": keyword_hit}
