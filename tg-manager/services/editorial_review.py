"""Editorial Review — редакционный гейт на реальном пути (Virtual Channel Administrator).

Связывает три готовых слоя в одну capability, которую зовёт UI ПЕРЕД публикацией:
  • политику канала        — services/channel_brain_store (va_channel_brain),
                              а без неё — общую политику владельца (OWNER_DEFAULT_KEY);
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

    channel_key задан → политика и история КОНКРЕТНОГО канала; None → общая
    политика владельца и история по всем каналам (случай массовой публикации).
    Fail-soft: сбой → вердикт «замечаний нет», чтобы предпросмотр не пугал зря.
    """
    try:
        rules: cb.BrandRules | None = None
        dup_threshold = 0.6
        # Политика канала, а если её нет — общая политика владельца «для всех
        # каналов» (её задают в Mini App). Раньше массовая публикация шла без
        # channel_key и правила владельца не применялись к ней вообще.
        brain = None
        if channel_key:
            brain = await store.get_profile(pool, owner_id, channel_key)
        if brain is None:
            brain = await store.get_profile(pool, owner_id, store.OWNER_DEFAULT_KEY)
        if brain is not None:
            rules = brain.brand_rules
            dup_threshold = brain.dup_threshold
        if channel_key:
            recent = await content_memory.recent_texts(
                pool, owner_id, channel_key, limit=recent_limit,
            )
        else:
            recent = await content_memory.recent_texts_for_owner(
                pool, owner_id, limit=recent_limit,
            )
        # Spintax {А|Б}: в канал уходит ОДИН вариант, с ним и сравниваем историю —
        # сырой шаблон со всеми ветками на опубликованный пост не похож никогда,
        # и повтор проходил незамеченным. Запрещённые слова и начала ищем по
        # всему шаблону: слово в любой ветке рано или поздно уйдёт в канал.
        variant = _one_variant(draft)
        verdict = cb.editorial_gate(
            variant, rules=rules, recent_texts=recent, dup_threshold=dup_threshold,
        )
        if variant != draft and rules is not None:
            seen = {(v["code"], v["detail"]) for v in verdict.brand.get("violations", [])}
            extra = [
                v for v in cb.brand_check(draft, rules)["violations"]
                if v["code"] in ("forbidden_word", "banned_opening")
                and (v["code"], v["detail"]) not in seen
            ]
            if extra:
                verdict.brand.setdefault("violations", []).extend(extra)
                verdict.reasons.extend(cb.violation_text(v) for v in extra)
                verdict.ok, verdict.needs_review = False, True
        return verdict
    except Exception:
        log.debug("editorial_review.review_draft failed owner=%s", owner_id, exc_info=True)
        return cb.EditorialVerdict(ok=True, needs_review=False)


def _one_variant(draft: str) -> str:
    """Один вариант spintax-шаблона (как его получит канал); без {…} — как есть."""
    if not draft or "{" not in draft:
        return draft
    try:
        from services.dm_engine import expand_spintax
        return expand_spintax(draft)
    except Exception:
        return draft


def verdict_to_public(verdict: cb.EditorialVerdict) -> dict:
    """Вердикт → JSON для Mini App: только то, что нужно показать человеку."""
    rep = verdict.repetition or {}
    return {
        "needs_review": bool(verdict.needs_review),
        "reasons": [str(r) for r in verdict.reasons[:8]],
        "similarity": int(round(float(rep.get("max_similarity") or 0) * 100)),
    }


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
