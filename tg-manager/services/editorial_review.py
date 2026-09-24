"""Editorial Review — редакционный гейт на реальном пути (Virtual Channel Administrator).

Связывает три готовых слоя в одну capability, которую зовёт UI ПЕРЕД публикацией:
  • политику канала        — services/channel_brain_store (va_channel_brain);
  • историю постов канала  — services/content_memory (va_channel_posts);
  • чистый quality gate     — services/channel_brain.editorial_gate.

ЗАЧЕМ отдельным слоем. channel_brain — детерминированная логика без БД,
channel_brain_store и content_memory — доступ к БД. UI не должен знать, как они
складываются: он зовёт review_draft(pool, owner_id, draft) и получает вердикт,
плюс format_advisory для готового текста-подсказки.

ГРАНИЦА БЕЗОПАСНОСТИ. Это СОВЕТ на шаге подтверждения, а не блокировка. Владелец
уже написал текст и решает публиковать — молча отменять его публикацию нельзя
(это и «мёртвый экран», и подмена решения пользователя). Поэтому:
  • review_draft fail-soft: любой сбой → «замечаний нет» (ok), а не ложная тревога;
  • format_advisory возвращает подсказку, но кнопку «Запустить» не трогает.
Автономный режим (когда администратор публикует сам, без человека) — отдельный
срез; там вердикт needs_review будет уводить на ревью, но не в этой ручной ветке.
"""
from __future__ import annotations

import html
import logging

from services import channel_brain as cb
from services import channel_brain_store as store
from services import content_memory

log = logging.getLogger(__name__)


async def review_draft(
    pool,
    owner_id: int,
    draft: str,
    *,
    channel_key: str | None = None,
    recent_limit: int = 20,
) -> cb.EditorialVerdict:
    """Прогнать черновик через редакционный гейт на реальной истории и политике.

    channel_key задан → политика и история КОНКРЕТНОГО канала; None → правила по
    умолчанию и история владельца по всем каналам (случай массовой публикации).
    Fail-soft: сбой → вердикт «замечаний нет», чтобы предпросмотр не пугал зря.
    """
    try:
        rules: cb.BrandRules | None = None
        dup_threshold = 0.6
        if channel_key:
            brain = await store.get_profile(pool, owner_id, channel_key)
            if brain is not None:
                rules = brain.brand_rules
                dup_threshold = brain.dup_threshold
            recent = await content_memory.recent_texts(
                pool, owner_id, channel_key, limit=recent_limit,
            )
        else:
            recent = await content_memory.recent_texts_for_owner(
                pool, owner_id, limit=recent_limit,
            )
        return cb.editorial_gate(
            draft, rules=rules, recent_texts=recent, dup_threshold=dup_threshold,
        )
    except Exception:
        log.debug("editorial_review.review_draft failed owner=%s", owner_id, exc_info=True)
        return cb.EditorialVerdict(ok=True, needs_review=False)


def format_advisory(verdict: cb.EditorialVerdict) -> str:
    """Вердикт → короткий HTML-блок для предпросмотра (или '' если замечаний нет).

    Текст-подсказка, а не запрет: явно говорит, что публикацию можно запустить как есть.
    """
    if not verdict.needs_review or not verdict.reasons:
        return ""
    lines = "\n".join(f"• {html.escape(str(r))}" for r in verdict.reasons[:6])
    return (
        "\n\n✍️ <b>Редактор советует проверить:</b>\n"
        f"{lines}\n"
        "<i>Это подсказка — публикацию можно запустить как есть.</i>"
    )
