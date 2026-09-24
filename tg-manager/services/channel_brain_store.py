"""Channel Brain Store — персистентная редакционная политика канала.

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
    """Политика канала или None. Fail-soft: сбой БД → None (не роняем вызывающего)."""
    try:
        row = await pool.fetchrow(
            "SELECT owner_id, channel_key, brand_rules, pillars, mix_weights, "
            "autonomy_mode, max_streak, dup_threshold "
            "FROM va_channel_brain WHERE owner_id=$1 AND channel_key=$2",
            owner_id, channel_key,
        )
    except Exception:
        log.debug("channel_brain_store.get_profile failed owner=%s", owner_id, exc_info=True)
        return None
    return _row_to_brain(row) if row else None


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
