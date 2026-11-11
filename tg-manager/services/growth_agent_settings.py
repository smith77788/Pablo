"""Настройки модуля «Агент роста»: одна точка приведения и границ.

Экран мини-аппа, бот и исполнители читают одни и те же параметры операций
`niche_growth_post` (продвижение в нише) и `ai_comment` (ИИ-комментинг). Раньше
границы жили в трёх местах (ручка, исполнитель, экран) и расходились: экран
обещал «до 5 групп», исполнитель резал до 5, а ручка ставила total_items=5 при
любом раскладе. Теперь границы — здесь, и сюда же смотрят тесты.

Безопасность флота сюда не вынесена и не ослаблена: паузы, карантин, захват
аккаунтов и губернатор темпа остаются в op_worker. Настройки только выбирают
ВНУТРИ безопасных рамок: темп можно сделать бережнее, но не быстрее
стандартного.

Все функции чистые — тестируются без базы и сети.
"""
from __future__ import annotations

import re
from typing import Any

# ── Продвижение в нише ───────────────────────────────────────────────────────

GROWTH_MAX_GROUPS_DEFAULT = 5
GROWTH_MAX_GROUPS_CAP = 10          # потолок за один запуск
GROWTH_MIN_MEMBERS_DEFAULT = 50
GROWTH_MEMBERS_CAP = 10_000_000

# Темп: (пауза вступление→пост, пауза между группами), секунды.
# «standard» — прежний безопасный режим; быстрее него вариантов нет.
GROWTH_PACES: dict[str, dict[str, tuple[int, int]]] = {
    "standard": {"join_to_post": (180, 480), "between": (600, 1200)},
    "careful": {"join_to_post": (360, 900), "between": (1200, 2400)},
}
GROWTH_PACE_LABELS = {"standard": "Стандартный", "careful": "Бережный"}

# ── ИИ-комментинг ────────────────────────────────────────────────────────────

COMMENT_POSTS_PER_CHANNEL_CAP = 3
COMMENT_CHANNELS_CAP = 50
COMMENT_PICKS = ("random", "latest")
COMMENT_LENGTHS = ("short", "medium")
COMMENT_EXTRA_MAX = 300
COMMENT_MAX_AGE_CAP_HOURS = 24 * 30


def _int(v: Any, default: int, lo: int, hi: int) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(n, hi))


def _bool(v: Any, default: bool) -> bool:
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "on", "да"):
        return True
    if s in ("0", "false", "no", "off", "нет", ""):
        return False
    return default


def split_list(v: Any, *, limit: int, item_max: int = 80) -> list[str]:
    """Список из массива или текста «через запятую / по строке». Без пустых и
    дублей (без учёта регистра), каждый элемент обрезан."""
    if v is None:
        return []
    if isinstance(v, str):
        raw = re.split(r"[\n,;]+", v)
    elif isinstance(v, (list, tuple)):
        raw = [str(x) for x in v]
    else:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for x in raw:
        x = " ".join(str(x).split())[:item_max]
        if not x or x.lower() in seen:
            continue
        seen.add(x.lower())
        out.append(x)
        if len(out) >= limit:
            break
    return out


def growth_settings(params: dict | None) -> dict:
    """Привести параметры продвижения к допустимым. Отсутствующее → дефолт,
    то есть старые операции (бот, очередь до обновления) идут как раньше."""
    p = params or {}
    pace = str(p.get("pace") or "standard").strip()
    if pace not in GROWTH_PACES:
        pace = "standard"
    min_members = _int(p.get("min_members"), GROWTH_MIN_MEMBERS_DEFAULT, 0, GROWTH_MEMBERS_CAP)
    max_members = _int(p.get("max_members"), 0, 0, GROWTH_MEMBERS_CAP)
    if max_members and max_members < min_members:
        max_members = 0  # перевёрнутый диапазон не должен молча отсеять всё
    return {
        "max_groups": _int(p.get("max_groups"), GROWTH_MAX_GROUPS_DEFAULT, 1, GROWTH_MAX_GROUPS_CAP),
        "acc_count": _int(p.get("acc_count"), 3, 1, 10),
        "min_members": min_members,
        "max_members": max_members,
        "keywords": split_list(p.get("keywords"), limit=15),
        "exclude_words": split_list(p.get("exclude_words"), limit=30, item_max=40),
        "pace": pace,
        "dry_run": _bool(p.get("dry_run"), False),
    }


def comment_settings(params: dict | None) -> dict:
    """Привести параметры ИИ-комментинга к допустимым (дефолт = прежнее
    поведение: один случайный из свежих постов, короткий комментарий)."""
    p = params or {}
    pick = str(p.get("post_pick") or "random").strip()
    if pick not in COMMENT_PICKS:
        pick = "random"
    length = str(p.get("length") or "short").strip()
    if length not in COMMENT_LENGTHS:
        length = "short"
    extra = " ".join(str(p.get("extra_instruction") or "").split())[:COMMENT_EXTRA_MAX]
    return {
        "acc_count": _int(p.get("acc_count"), 3, 1, 10),
        "posts_per_channel": _int(p.get("posts_per_channel"), 1, 1, COMMENT_POSTS_PER_CHANNEL_CAP),
        "post_pick": pick,
        "max_post_age_hours": _int(p.get("max_post_age_hours"), 0, 0, COMMENT_MAX_AGE_CAP_HOURS),
        "length": length,
        "extra_instruction": extra,
        "join_discussion": _bool(p.get("join_discussion"), True),
        "skip_commented": _bool(p.get("skip_commented"), True),
    }


def growth_eta_minutes(groups: int, pace: str) -> tuple[int, int]:
    """Сколько примерно займёт прогон (мин–макс, минуты) — для подтверждения
    на экране: владелец должен знать, что 5 групп — это час-полтора, а не миг."""
    g = max(0, int(groups))
    if not g:
        return 0, 0
    t = GROWTH_PACES.get(pace, GROWTH_PACES["standard"])
    lo = g * t["join_to_post"][0] + (g - 1) * t["between"][0]
    hi = g * t["join_to_post"][1] + (g - 1) * t["between"][1]
    return lo // 60, -(-hi // 60)
