"""Channel Brain Store — персистентная редакционная политика канала.

ПОДКЛЮЧЕНО. Политику читает services/editorial_review.review_draft, который зовёт
предпросмотр массовой публикации (в боте и в Mini App) — как советующий редакционный
гейт перед подтверждением. Задаётся на экране «Правила редактора» в Mini App
(/api/miniapp/editorial/policy, общая политика владельца под OWNER_DEFAULT_KEY).

Читает/пишет va_channel_brain (schema_v222) и связывает сохранённую политику с
чистой логикой контроля качества из services/channel_brain: даёт готовую
capability «проверить черновик по политике канала» и «выбрать следующую рубрику».

Хранение отделено от логики: channel_brain.py остаётся без БД (тестируется
детерминированно), а этот модуль — тонкий слой доступа (owner-scoped, fail-soft
в стиле проекта: сбой чтения не роняет вызывающего).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from services import channel_brain as cb

log = logging.getLogger(__name__)

_ALLOWED_AUTONOMY = ("manual", "semi", "autonomous")

# Политика владельца «для всех каналов». Массовая публикация идёт сразу во все
# каналы, и у неё нет одного channel_key — без общей политики правила, заданные
# владельцем, не применялись бы ни к одной массовой публикации.
OWNER_DEFAULT_KEY = "*"

# Границы правил, которые владелец задаёт из Mini App. Лимит Telegram на текст
# сообщения — 4096; списки слов ограничены, чтобы проверка каждого черновика не
# превращалась в перебор тысяч подстрок.
_MAX_TEXT = 4096
_MAX_WORDS = 100
_MAX_OPENINGS = 50
_MAX_WORD_LEN = 60
_DUP_MIN, _DUP_MAX = 0.3, 1.0
_MAX_PILLARS = 12
_MAX_PILLAR_LEN = 40
_MAX_WEIGHT = 10


def brand_rules_from_dict(d: Optional[dict]) -> cb.BrandRules:
    """dict (из JSONB) → BrandRules. Незнакомые ключи игнорируются; списки → кортежи."""
    d = d or {}
    def _tup(key: str, default=()):
        v = d.get(key, default)
        return tuple(v) if isinstance(v, (list, tuple)) else default
    return cb.BrandRules(
        min_chars=int(d.get("min_chars", 0)),
        max_chars=int(d.get("max_chars", 4096)),
        max_emoji=d.get("max_emoji"),
        max_cta=d.get("max_cta"),
        forbidden_words=_tup("forbidden_words"),
        banned_openings=_tup("banned_openings"),
        cta_markers=_tup("cta_markers", cb._DEFAULT_CTA_MARKERS),
    )


def _as_obj(value: Any, default):
    """JSONB из asyncpg приходит str или уже распарсенным — приводим к объекту."""
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return default
    return default


@dataclass
class ChannelBrain:
    owner_id: int
    channel_key: str
    brand_rules: cb.BrandRules
    pillars: list[str] = field(default_factory=list)
    mix_weights: dict[str, float] = field(default_factory=dict)
    autonomy_mode: str = "manual"
    max_streak: int = 2
    dup_threshold: float = 0.6

    # ── capability поверх сохранённой политики ──
    def check_draft(self, draft: str, recent_texts=()) -> cb.EditorialVerdict:
        """Пропустить/на ревью черновик по политике ЭТОГО канала."""
        return cb.editorial_gate(
            draft, rules=self.brand_rules,
            recent_texts=recent_texts, dup_threshold=self.dup_threshold,
        )

    def plan_next(self, recent_pillars: list[str]) -> Optional[str]:
        """Какую рубрику публиковать следующей (контент-микс канала)."""
        return cb.pick_next_pillar(
            recent_pillars, self.pillars,
            max_streak=self.max_streak, weights=self.mix_weights or None,
        )


def _row_to_brain(row) -> ChannelBrain:
    return ChannelBrain(
        owner_id=int(row["owner_id"]),
        channel_key=str(row["channel_key"]),
        brand_rules=brand_rules_from_dict(_as_obj(row["brand_rules"], {})),
        pillars=list(_as_obj(row["pillars"], [])),
        mix_weights=dict(_as_obj(row["mix_weights"], {})),
        autonomy_mode=str(row["autonomy_mode"]),
        max_streak=int(row["max_streak"]),
        dup_threshold=float(row["dup_threshold"]),
    )


async def get_profile(pool, owner_id: int, channel_key: str) -> Optional[ChannelBrain]:
    """Политика канала или None. Fail-soft: сбой БД → None (не роняем вызывающего).

    Разбор строки — тоже внутри try: руками поправленный JSONB с мусором
    («max_emoji": "много») иначе ронял бы каждого вызывающего.
    """
    try:
        row = await pool.fetchrow(
            "SELECT owner_id, channel_key, brand_rules, pillars, mix_weights, "
            "autonomy_mode, max_streak, dup_threshold "
            "FROM va_channel_brain WHERE owner_id=$1 AND channel_key=$2",
            owner_id, channel_key,
        )
        return _row_to_brain(row) if row else None
    except Exception:
        log.warning("channel_brain_store.get_profile failed owner=%s ch=%s",
                    owner_id, channel_key, exc_info=True)
        return None


def _clean_words(raw: Any, cap: int, label: str, errors: list[str]) -> list[str]:
    """Список слов/фраз: строка через запятую/перенос или массив → нижний регистр,
    без пустых и повторов, в пределах лимитов."""
    if raw is None:
        return []
    if isinstance(raw, str):
        items = raw.replace("\n", ",").split(",")
    elif isinstance(raw, (list, tuple)):
        items = raw
    else:
        errors.append(f"{label}: нужен список")
        return []
    out: list[str] = []
    for it in items:
        w = " ".join(str(it).split()).lower()
        if not w or w in out:
            continue
        if len(w) > _MAX_WORD_LEN:
            errors.append(f"{label}: «{w[:20]}…» длиннее {_MAX_WORD_LEN} символов")
            continue
        out.append(w)
    if len(out) > cap:
        errors.append(f"{label}: не больше {cap}, сейчас {len(out)}")
        out = out[:cap]
    return out


def _opt_int(raw: Any, lo: int, hi: int, label: str, errors: list[str]) -> Optional[int]:
    """Необязательный целый лимит: пусто → None (без ограничения)."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    try:
        v = int(raw)
    except (TypeError, ValueError):
        errors.append(f"{label}: нужно целое число")
        return None
    if isinstance(raw, float) and raw != v:
        errors.append(f"{label}: нужно целое число")
        return None
    if not lo <= v <= hi:
        errors.append(f"{label}: от {lo} до {hi}")
        return None
    return v


def validate_policy(payload: Any) -> tuple[dict, list[str]]:
    """Правила из Mini App → (чистые поля для save_profile, ошибки по-русски).

    Возвращает brand_rules (dict для JSONB) и dup_threshold. Ошибки не
    «исправляются молча»: при любой ошибке вызывающий должен отказать с
    перечнем, а не сохранять полуправильную политику.
    """
    errors: list[str] = []
    if not isinstance(payload, dict):
        return {}, ["Ожидался объект с правилами"]
    min_chars = _opt_int(payload.get("min_chars"), 0, _MAX_TEXT, "Минимальная длина", errors)
    max_chars = _opt_int(payload.get("max_chars"), 1, _MAX_TEXT, "Максимальная длина", errors)
    if min_chars is not None and max_chars is not None and min_chars > max_chars:
        errors.append("Минимальная длина больше максимальной")
    rules: dict = {
        "min_chars": min_chars or 0,
        "max_chars": max_chars or _MAX_TEXT,
        "max_emoji": _opt_int(payload.get("max_emoji"), 0, 100, "Эмодзи в посте", errors),
        "max_cta": _opt_int(payload.get("max_cta"), 0, 20, "Призывов к действию", errors),
        "forbidden_words": _clean_words(payload.get("forbidden_words"), _MAX_WORDS,
                                        "Запрещённые слова", errors),
        "banned_openings": _clean_words(payload.get("banned_openings"), _MAX_OPENINGS,
                                        "Запрещённые начала", errors),
    }
    dup_raw = payload.get("dup_threshold", 0.6)
    try:
        dup = float(dup_raw)
        if dup > 1.0:          # из UI может прийти процент
            dup = dup / 100.0
        if not _DUP_MIN <= dup <= _DUP_MAX:
            errors.append("Порог повтора: от 30 до 100%")
            dup = 0.6
    except (TypeError, ValueError):
        errors.append("Порог повтора: нужно число")
        dup = 0.6
    return {"brand_rules": rules, "dup_threshold": round(dup, 3)}, errors


def validate_pillars(raw: Any) -> tuple[list[str], dict[str, float], list[str]]:
    """Рубрики канала → (названия, доли, ошибки по-русски).

    Принимает строки «Название: доля» (по одной на строку) или список
    {name, weight}. Доля — целое 1…10, по умолчанию 1: это относительный вес,
    а не процент, поэтому «Новости: 3, Реклама: 1» значит «новостей втрое больше».
    """
    errors: list[str] = []
    items: list[tuple[str, Any]] = []
    if raw is None:
        return [], {}, errors
    if isinstance(raw, str):
        for line in raw.splitlines():
            if not line.strip():
                continue
            # «Итоги: неделя» — это название с двоеточием, а не доля.
            name, _, w = line.rpartition(":")
            if not name or not w.strip().lstrip("-").isdigit():
                name, w = line, ""
            items.append((name, w or None))
    elif isinstance(raw, list):
        for it in raw:
            if isinstance(it, dict):
                items.append((it.get("name", ""), it.get("weight")))
            else:
                items.append((it, None))
    else:
        return [], {}, ["Рубрики: нужен список"]
    names: list[str] = []
    weights: dict[str, float] = {}
    for name, w in items:
        n = " ".join(str(name or "").split())
        if not n:
            continue
        if len(n) > _MAX_PILLAR_LEN:
            errors.append(f"Рубрика «{n[:20]}…» длиннее {_MAX_PILLAR_LEN} символов")
            continue
        if n.lower() in (x.lower() for x in names):
            errors.append(f"Рубрика «{n}» указана дважды")
            continue
        weight = 1
        if w is not None and str(w).strip():
            try:
                weight = int(str(w).strip())
            except ValueError:
                errors.append(f"Рубрика «{n}»: доля должна быть целым числом")
                continue
            if not 1 <= weight <= _MAX_WEIGHT:
                errors.append(f"Рубрика «{n}»: доля от 1 до {_MAX_WEIGHT}")
                continue
        names.append(n)
        weights[n] = float(weight)
    if len(names) > _MAX_PILLARS:
        errors.append(f"Рубрик не больше {_MAX_PILLARS}, сейчас {len(names)}")
    return names[:_MAX_PILLARS], weights, errors


def to_public(brain: Optional[ChannelBrain]) -> dict:
    """Политика → JSON для Mini App. Нет политики → значения по умолчанию."""
    r = brain.brand_rules if brain else cb.BrandRules()
    return {
        "configured": brain is not None,
        "min_chars": r.min_chars,
        "max_chars": r.max_chars,
        "max_emoji": r.max_emoji,
        "max_cta": r.max_cta,
        "forbidden_words": list(r.forbidden_words),
        "banned_openings": list(r.banned_openings),
        "dup_threshold": brain.dup_threshold if brain else 0.6,
        "pillars": [
            {"name": p, "weight": int((brain.mix_weights or {}).get(p, 1))}
            for p in (brain.pillars if brain else [])
        ],
    }


async def save_profile(
    pool, owner_id: int, channel_key: str, *,
    brand_rules: Optional[dict] = None,
    pillars: Optional[list] = None,
    mix_weights: Optional[dict] = None,
    autonomy_mode: str = "manual",
    max_streak: int = 2,
    dup_threshold: float = 0.6,
) -> Optional[int]:
    """Создать/обновить политику канала (upsert по owner_id+channel_key). Возвращает id."""
    mode = autonomy_mode if autonomy_mode in _ALLOWED_AUTONOMY else "manual"
    try:
        row = await pool.fetchrow(
            """INSERT INTO va_channel_brain
                   (owner_id, channel_key, brand_rules, pillars, mix_weights,
                    autonomy_mode, max_streak, dup_threshold, updated_at)
               VALUES ($1,$2,$3::jsonb,$4::jsonb,$5::jsonb,$6,$7,$8, now())
               ON CONFLICT (owner_id, channel_key) DO UPDATE SET
                   brand_rules=EXCLUDED.brand_rules, pillars=EXCLUDED.pillars,
                   mix_weights=EXCLUDED.mix_weights, autonomy_mode=EXCLUDED.autonomy_mode,
                   max_streak=EXCLUDED.max_streak, dup_threshold=EXCLUDED.dup_threshold,
                   updated_at=now()
               RETURNING id""",
            owner_id, channel_key,
            json.dumps(brand_rules or {}, ensure_ascii=False),
            json.dumps(pillars or [], ensure_ascii=False),
            json.dumps(mix_weights or {}, ensure_ascii=False),
            mode, int(max_streak), float(dup_threshold),
        )
        return int(row["id"]) if row else None
    except Exception:
        log.debug("channel_brain_store.save_profile failed owner=%s", owner_id, exc_info=True)
        return None
