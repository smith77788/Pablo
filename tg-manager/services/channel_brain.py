"""Channel Brain — редакционный контроль качества постов (первый срез Virtual Administrator).

ЗАЧЕМ. Ключевое отличие «цифрового редактора» от ИИ-копирайтера — не генерация, а
КОНТРОЛЬ перед публикацией: не повторяться, держать голос бренда, чередовать
форматы. Без этого слоя канал превращается в поток однотипных ИИ-текстов
(«Заголовок / 3 пункта / 🔥 / CTA»), который читатель распознаёт мгновенно.

Здесь — только ЧИСТЫЕ функции (без БД, ИИ и сети), поэтому детерминированы и
полностью тестируемы. Их ДОЛЖЕН вызывать пайплайн публикации ПЕРЕД постановкой в
очередь: repetition_check + brand_check + content-mix = единый quality gate (см.
spec §8, §9, §13/14, §51, §89). Генерация текста и хранение модели проекта —
отдельные срезы; этот слой их проверяет, а не заменяет.

Это не обход модерации и не накрутка: слой снижает спамность СВОЕГО канала и
уважает читателя, а не воздействует на третьих лиц.

ПОДКЛЮЧЕНО (как советующий гейт). Цепочка: op_worker._exec_mass_publish пишет тело
каждого опубликованного поста в историю канала (services/content_memory,
va_channel_posts); предпросмотр массовой публикации (bot/handlers/mass_publish.py)
зовёт services/editorial_review.review_draft, который берёт эту историю +
политику канала (services/channel_brain_store) и прогоняет editorial_gate,
показывая замечания ПЕРЕД подтверждением. Это подсказка на шаге подтверждения, а
НЕ блокировка: владелец уже написал текст и решает сам. Автоблокировка в
автономном режиме (администратор публикует без человека) — отдельный будущий срез.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

# ── Нормализация текста ─────────────────────────────────────────────────────────

_WORD_RE = re.compile(r"[^\w\s]+", re.UNICODE)
_WS_RE = re.compile(r"\s+", re.UNICODE)

# Диапазоны эмодзи (грубо, но покрывает основное — символы/пиктограммы/эмотиконы).
_EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF←-⇿⬀-⯿️]"
)

# Дефолтные CTA-маркеры (нижний регистр). Переопределяются в BrandRules.
_DEFAULT_CTA_MARKERS = (
    "подпишись", "подписывайтесь", "переходи", "переходите", "жми", "жмите",
    "успей", "успейте", "закажи", "закажите", "купи", "купите", "ссылка в",
    "по ссылке", "регистрируйся", "оставь заявку", "пиши в личку", "подробнее по",
)


def _normalize(text: str) -> str:
    """К нижнему регистру, без пунктуации, схлопнутые пробелы."""
    t = _WORD_RE.sub(" ", (text or "").lower())
    return _WS_RE.sub(" ", t).strip()


def _tokens(text: str) -> list[str]:
    n = _normalize(text)
    return n.split(" ") if n else []


def _shingles(text: str, k: int = 3) -> set[str]:
    """Множество словных k-грамм (для семантической близости без ИИ)."""
    toks = _tokens(text)
    if len(toks) < k:
        return {" ".join(toks)} if toks else set()
    return {" ".join(toks[i:i + k]) for i in range(len(toks) - k + 1)}


def jaccard(a: str, b: str, k: int = 3) -> float:
    """Похожесть двух текстов по k-граммам, 0..1. 1.0 — совпадают дословно."""
    sa, sb = _shingles(a, k), _shingles(b, k)
    if not sa and not sb:
        return 1.0
    if not sa or not sb:
        return 0.0
    inter = len(sa & sb)
    union = len(sa | sb)
    return inter / union if union else 0.0


def emoji_count(text: str) -> int:
    return len(_EMOJI_RE.findall(text or ""))


def cta_count(text: str, markers: Iterable[str] = _DEFAULT_CTA_MARKERS) -> int:
    low = (text or "").lower()
    return sum(low.count(m) for m in markers)


# ── Повторяемость (Content Memory, spec §9/§51) ──────────────────────────────────

def repetition_check(
    draft: str,
    recent_texts: Iterable[str],
    *,
    dup_threshold: float = 0.6,
    opening_words: int = 5,
    k: int = 3,
) -> dict:
    """Похож ли черновик на недавние посты.

    Возвращает {max_similarity, is_duplicate, opening_repeat, nearest_index}.
      is_duplicate    — max похожесть ≥ dup_threshold (почти повтор);
      opening_repeat  — первые opening_words слов совпадают с недавним постом
                        (одинаковые вступления — главный признак ИИ-потока).
    """
    recent = [t for t in recent_texts if (t or "").strip()]
    best, best_i = 0.0, -1
    for i, t in enumerate(recent):
        s = jaccard(draft, t, k)
        if s > best:
            best, best_i = s, i

    draft_open = _tokens(draft)[:opening_words]
    opening_repeat = bool(draft_open) and any(
        _tokens(t)[:opening_words] == draft_open for t in recent
    )
    return {
        "max_similarity": round(best, 4),
        "is_duplicate": best >= dup_threshold,
        "opening_repeat": opening_repeat,
        "nearest_index": best_i,
    }


# ── Голос бренда (Human Editorial Model, spec §8/§22) ─────────────────────────────

@dataclass
class BrandRules:
    """Проверяемые правила бренда. Все пороги — настраиваемые, без хардкода ниши."""
    min_chars: int = 0
    max_chars: int = 4096                 # лимит Telegram на caption/сообщение
    max_emoji: Optional[int] = None       # None = без ограничения
    max_cta: Optional[int] = None
    forbidden_words: tuple[str, ...] = ()  # нижний регистр, подстрока
    banned_openings: tuple[str, ...] = ()  # запрещённые вступления (нижний регистр)
    cta_markers: tuple[str, ...] = _DEFAULT_CTA_MARKERS


def brand_check(draft: str, rules: BrandRules) -> dict:
    """Соответствует ли черновик правилам бренда.

    Возвращает {passed, violations:[{code, detail}]}. Код нарушения машиночитаемый,
    detail — человеку. Пустой violations ⇒ passed=True.
    """
    text = draft or ""
    low = text.lower()
    violations: list[dict] = []

    n = len(text)
    if n < rules.min_chars:
        violations.append({"code": "too_short", "detail": f"{n} < {rules.min_chars} символов"})
    if n > rules.max_chars:
        violations.append({"code": "too_long", "detail": f"{n} > {rules.max_chars} символов"})

    if rules.max_emoji is not None:
        e = emoji_count(text)
        if e > rules.max_emoji:
            violations.append({"code": "too_many_emoji", "detail": f"{e} > {rules.max_emoji}"})

    if rules.max_cta is not None:
        c = cta_count(text, rules.cta_markers)
        if c > rules.max_cta:
            violations.append({"code": "too_many_cta", "detail": f"{c} > {rules.max_cta}"})

    for w in rules.forbidden_words:
        if w and w.lower() in low:
            violations.append({"code": "forbidden_word", "detail": w})

    stripped = low.lstrip()
    for op in rules.banned_openings:
        if op and stripped.startswith(op.lower()):
            violations.append({"code": "banned_opening", "detail": op})

    return {"passed": not violations, "violations": violations}


# ── Контент-микс (spec §13/§14) ──────────────────────────────────────────────────

def pick_next_pillar(
    recent_pillars: list[str],
    allowed: list[str],
    *,
    max_streak: int = 2,
    weights: Optional[dict[str, float]] = None,
) -> Optional[str]:
    """Какой формат/рубрику публиковать следующим.

    recent_pillars — история (самый свежий В КОНЦЕ). Правила:
      • не превышать max_streak одинаковых подряд (не «4 рекламы подряд»);
      • при прочих равных брать наиболее недопредставленный относительно weights
        (доля целевая) и давно не выходивший.
    Возвращает выбранный pillar или None, если allowed пуст.
    """
    allowed = [p for p in allowed if p]
    if not allowed:
        return None

    # Что нельзя из-за серии подряд.
    tail = _trailing_streak(recent_pillars)
    blocked = {tail[0]} if len(tail) >= max_streak else set()

    candidates = [p for p in allowed if p not in blocked] or allowed

    total = len(recent_pillars) or 1
    counts = {p: recent_pillars.count(p) for p in allowed}
    w = weights or {p: 1.0 for p in allowed}
    wsum = sum(w.get(p, 0.0) for p in allowed) or 1.0

    def deficit(p: str) -> float:
        target = w.get(p, 0.0) / wsum
        actual = counts.get(p, 0) / total
        return target - actual  # больше дефицит → выше приоритет

    def last_seen(p: str) -> int:
        # индекс с конца последнего появления; больше = давнее (или не было)
        for back, x in enumerate(reversed(recent_pillars)):
            if x == p:
                return back
        return len(recent_pillars) + 1

    return max(candidates, key=lambda p: (round(deficit(p), 6), last_seen(p)))


def _trailing_streak(seq: list[str]) -> list[str]:
    """Хвост одинаковых подряд элементов (напр. ['ad','ad'] для ...,'x','ad','ad')."""
    if not seq:
        return []
    last = seq[-1]
    out: list[str] = []
    for x in reversed(seq):
        if x == last:
            out.append(x)
        else:
            break
    return out


# ── Единый quality gate (spec §89) ───────────────────────────────────────────────

@dataclass
class EditorialVerdict:
    ok: bool
    needs_review: bool
    reasons: list[str] = field(default_factory=list)
    repetition: dict = field(default_factory=dict)
    brand: dict = field(default_factory=dict)


def editorial_gate(
    draft: str,
    *,
    rules: Optional[BrandRules] = None,
    recent_texts: Iterable[str] = (),
    dup_threshold: float = 0.6,
) -> EditorialVerdict:
    """Пропустить черновик, отправить на ревью или заблокировать.

    ok=True          — можно публиковать автономно;
    needs_review=True — есть предупреждения (повтор/бренд), нужен человек;
    reasons          — человекочитаемые причины.
    Не «имитирует человека» — снижает шаблонность и держит голос бренда.
    """
    rules = rules or BrandRules()
    rep = repetition_check(draft, recent_texts, dup_threshold=dup_threshold)
    br = brand_check(draft, rules)

    reasons: list[str] = []
    if rep["is_duplicate"]:
        reasons.append(f"почти-повтор недавнего поста (похожесть {rep['max_similarity']})")
    if rep["opening_repeat"]:
        reasons.append("одинаковое вступление с недавним постом")
    for v in br["violations"]:
        reasons.append(f"бренд: {v['code']} ({v['detail']})")

    needs_review = bool(reasons)
    return EditorialVerdict(
        ok=not needs_review,
        needs_review=needs_review,
        reasons=reasons,
        repetition=rep,
        brand=br,
    )
