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


# ── Стилевые повторы: одинаковые начала, концовки и затёртые обороты ──────────────
#
# repetition_check ловит только почти дословный повтор (тройки слов ≥ 60%) и
# совпадение ПЕРВЫХ ПЯТИ слов подряд. Канал при этом выглядел штамповкой: каждый
# пост начинался «<Город>, схоже, …», заканчивался «Чекаємо на подальші…», а
# внутри кочевали «креативний спосіб», «вирішив додати трохи». Первые пять слов
# разные (разные города), общих троек мало — обе проверки молчали. Жалоба
# владельца: «посты не должны быть похожи друг на друга даже на 1%».
#
# Здесь сравниваются не строки, а ПРИЁМЫ: какими словами пост начинается и
# заканчивается (без имён собственных — разные города не делают начало разным)
# и какие связки из двух значимых слов уже встречались в канале. Слова берутся
# по основе (первые пять букв): «вирішив» и «вирішили» — один приём.
#
# Факты новости повторами не считаются: всё, что есть в тексте источника
# (allow_text), — это предмет поста, а не стиль.

_STYLE_WORD_RE = re.compile(r"[^\W\d_]+(?:['’ʼ][^\W\d_]+)*", re.UNICODE)
_SENT_RE = re.compile(r"(?<=[.!?…])\s+|\n+")

_STOP = frozenset("""
а і й та в у на з із зі що це не як до по за від для або чи же ж ще вже так але бо
то ми ви він вона вони його її їх нас вас цей ця ці той те там тут коли де хто щоб
якщо лише тільки про при над під без через після перед між є був була було були
буде нам вам їм собі себе свій своє свої своїх своїм мов мовляв адже навіть ну
може можна можуть має мають треба також теж дуже більше менше який яка яке які
может можно могут надо также тоже очень более менее который которая которое которые
и с со что это как но или ли бы он она оно они мы вы я ее о об от из к ко все всё
уже еще ещё где когда кто чтобы если только есть будет был была было были им ним
the a an and or of to in on for is are be was were this that it with as at by
""".split())


def _style_words(text: str) -> list[tuple[str, str]]:
    """[(как написано, основа)] значимых слов: без служебных и коротких."""
    out = []
    for w in _STYLE_WORD_RE.findall(text or ""):
        low = w.lower().replace("’", "'").replace("ʼ", "'")
        if len(low) < 4 or low in _STOP:
            continue
        out.append((w, low.replace("'", "")[:5]))
    return out


def _sentences(text: str) -> list[str]:
    return [x.strip() for x in _SENT_RE.split(text or "") if x and x.strip()]


def _edge_markers(sentence: str, limit: int) -> dict[str, str]:
    """Основа → слово для первых `limit` значимых слов фразы, кроме имён.

    Имя собственное (слово с заглавной НЕ в начале фразы) приёмом не является:
    «Київ, схоже» и «Рівне, схоже» — один приём, и общий у них именно «схоже».
    Первое слово фразы берём: там чаще всего и сидит приём («Чекаємо на…»,
    «Схоже, …»). Если это место из новости — его снимет allow_text.
    """
    out: dict[str, str] = {}
    words = _STYLE_WORD_RE.findall(sentence or "")
    first = words[0] if words else ""
    for w, st in _style_words(sentence)[:limit]:
        if w[:1].isupper() and w != first:
            continue
        out.setdefault(st, w.lower())
    return out


def _content_bigrams(text: str) -> dict[tuple[str, str], str]:
    """Связки двух соседних значимых слов: основы → как написано."""
    ws = _style_words(text)
    return {(ws[i][1], ws[i + 1][1]): f"{ws[i][0]} {ws[i + 1][0]}".lower()
            for i in range(len(ws) - 1)}


def worn_phrases(recent_texts: Iterable[str], *, window: int = 12,
                 min_posts: int = 2) -> list[str]:
    """Обороты, которые уже стали привычкой канала: есть в ≥ min_posts постах."""
    seen: dict[tuple[str, str], set[int]] = {}
    surface: dict[tuple[str, str], str] = {}
    for i, t in enumerate([t for t in recent_texts if (t or "").strip()][:window]):
        for key, phrase in _content_bigrams(t).items():
            seen.setdefault(key, set()).add(i)
            surface.setdefault(key, phrase)
    return [surface[k] for k, posts in seen.items() if len(posts) >= min_posts]


def _hint_words(sentence: str, limit: int) -> dict[str, str]:
    """Как _edge_markers, но без слов с заглавной вовсе — для подсказки автору.

    В подсказке «не начинай со слова „Київ“» вредна: новость про Київ обязана
    его назвать. Проверка (style_repeats) снимает такие слова текстом
    источника, а подсказка их просто не показывает.
    """
    return {st: w.lower() for w, st in _style_words(sentence)[:limit] if not w[:1].isupper()}


def used_openings(recent_texts: Iterable[str], *, window: int = 12) -> list[str]:
    """Слова, которыми начинались недавние посты (для подсказки автору)."""
    out: dict[str, str] = {}
    for t in [t for t in recent_texts if (t or "").strip()][:window]:
        sents = _sentences(t)
        if sents:
            for st, w in _hint_words(sents[0], 6).items():
                out.setdefault(st, w)
    return list(out.values())


def used_closings(recent_texts: Iterable[str], *, window: int = 12) -> list[str]:
    """Слова, с которых начиналась последняя фраза недавних постов."""
    out: dict[str, str] = {}
    for t in [t for t in recent_texts if (t or "").strip()][:window]:
        sents = _sentences(t)
        if len(sents) > 1:
            ws = _style_words(sents[-1])[:4]
            for w, st in ws:
                out.setdefault(st, w.lower())
    return list(out.values())


def style_repeats(draft: str, recent_texts: Iterable[str], *, window: int = 12,
                  allow_text: str = "") -> dict:
    """Чем черновик повторяет недавние посты канала.

    Возвращает {"opening": [слова], "closing": [слова], "phrases": [обороты]}:
      opening — значимые слова начала, которыми уже начинался недавний пост;
      closing — то же для последней фразы;
      phrases — связки двух значимых слов, уже бывшие в недавних постах.
    Пусто везде — повторов приёма нет. Слова и связки из allow_text (текст
    новости-источника) не считаются: это факты, а не стиль.
    """
    recent = [t for t in recent_texts if (t or "").strip()][:window]
    allowed = {st for _, st in _style_words(allow_text)}
    allowed_pairs = set(_content_bigrams(allow_text))
    sents = _sentences(draft)
    if not sents or not recent:
        return {"opening": [], "closing": [], "phrases": []}

    def _edges(texts, first: bool, limit: int) -> set[str]:
        out: set[str] = set()
        for t in texts:
            ss = _sentences(t)
            if not ss or (not first and len(ss) < 2):
                continue
            out |= set(_edge_markers(ss[0] if first else ss[-1], limit))
        return out

    open_hist, close_hist = _edges(recent, True, 6), _edges(recent, False, 4)
    opening = [w for st, w in _edge_markers(sents[0], 6).items()
               if st in open_hist and st not in allowed]
    closing = []
    if len(sents) > 1:
        closing = [w for st, w in _edge_markers(sents[-1], 4).items()
                   if st in close_hist and st not in allowed]
    hist_pairs: set[tuple[str, str]] = set()
    for t in recent:
        hist_pairs |= set(_content_bigrams(t))
    phrases = [p for key, p in _content_bigrams(draft).items()
               if key in hist_pairs and key not in allowed_pairs
               and not (key[0] in allowed and key[1] in allowed)]
    return {"opening": opening, "closing": closing, "phrases": phrases}


def style_repeat_reasons(draft: str, recent_texts: Iterable[str], *,
                         allow_text: str = "") -> list[str]:
    """Замечания редактора по стилевым повторам — словами, которые поймёт автор."""
    r = style_repeats(draft, recent_texts, allow_text=allow_text)
    out = []
    if r["opening"]:
        out.append("начало повторяет приём недавних постов (" + ", ".join(
            f"«{w}»" for w in r["opening"][:5]) + ") — начни совсем иначе")
    if r["closing"]:
        out.append("концовка повторяет недавние посты (" + ", ".join(
            f"«{w}»" for w in r["closing"][:5]) + ") — закончи по-другому")
    if r["phrases"]:
        out.append("обороты уже были в канале: " + ", ".join(
            f"«{p}»" for p in r["phrases"][:8]) + " — перефразируй своими словами")
    return out


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
        violations.append({"code": "too_short", "detail": f"{n} < {rules.min_chars} символов",
                           "value": n, "limit": rules.min_chars})
    if n > rules.max_chars:
        violations.append({"code": "too_long", "detail": f"{n} > {rules.max_chars} символов",
                           "value": n, "limit": rules.max_chars})

    if rules.max_emoji is not None:
        e = emoji_count(text)
        if e > rules.max_emoji:
            violations.append({"code": "too_many_emoji", "detail": f"{e} > {rules.max_emoji}",
                               "value": e, "limit": rules.max_emoji})

    if rules.max_cta is not None:
        c = cta_count(text, rules.cta_markers)
        if c > rules.max_cta:
            violations.append({"code": "too_many_cta", "detail": f"{c} > {rules.max_cta}",
                               "value": c, "limit": rules.max_cta})

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


# Причины видит владелец, а он не читает по-английски: раньше в подсказку уходил
# сырой код («бренд: too_many_emoji (5 > 3)»). Код остаётся в brand.violations
# для машин, человеку — фраза.
def violation_text(v: dict) -> str:
    code, detail = v.get("code"), str(v.get("detail", ""))
    have, limit = v.get("value"), v.get("limit")
    if code == "too_short":
        return f"слишком короткий: {have} символов, нужно от {limit}"
    if code == "too_long":
        return f"слишком длинный: {have} символов, можно до {limit}"
    if code == "too_many_emoji":
        return f"много эмодзи: {have}, можно до {limit}"
    if code == "too_many_cta":
        return f"много призывов к действию: {have}, можно до {limit}"
    if code == "forbidden_word":
        return f"запрещённое слово: «{detail}»"
    if code == "banned_opening":
        return f"запрещённое начало поста: «{detail}»"
    return f"нарушено правило канала: {detail}"


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
        pct = int(round(rep["max_similarity"] * 100))
        reasons.append(f"почти повторяет недавний пост (совпадает на {pct}%)")
    if rep["opening_repeat"]:
        reasons.append("начинается так же, как недавний пост")
    for v in br["violations"]:
        reasons.append(violation_text(v))

    needs_review = bool(reasons)
    return EditorialVerdict(
        ok=not needs_review,
        needs_review=needs_review,
        reasons=reasons,
        repetition=rep,
        brand=br,
    )


# Режимы автономности редактора. Определяют, что редактор ДЕЛАЕТ с замечанием, а
# не только показывает ли его. По возрастанию строгости:
#   manual     — только советует; публикацию не останавливает никогда;
#   semi       — блокирует явный брак (запрещённые слова/начала — нарушение
#                голоса бренда), но повтор пропускает как мягкое замечание;
#   autonomous — блокирует любое замечание (и повтор, и бренд): владелец
#                делегировал контроль полностью, пост-брак в канал не уходит.
AUTONOMY_MANUAL = "manual"
AUTONOMY_SEMI = "semi"
AUTONOMY_AUTONOMOUS = "autonomous"
AUTONOMY_MODES = (AUTONOMY_MANUAL, AUTONOMY_SEMI, AUTONOMY_AUTONOMOUS)

# Коды нарушений, которые считаются «жёстким браком» бренда (блокируются уже в
# semi-режиме). Мягкие сигналы (длина, эмодзи, CTA, повтор) в semi лишь советуют.
_HARD_BRAND_CODES = ("forbidden_word", "banned_opening")


def enforce_decision(mode: str, verdict: EditorialVerdict) -> tuple[bool, list[str]]:
    """Решение авто-блокировки по режиму и вердикту редактора.

    Возвращает (блокировать?, причины). Причины непусты только при блокировке.
    Чистая функция — вся политика блокировки здесь, тестируется без БД и сети.

    Fail-open по построению: чистый вердикт (ok) не блокируется ни в каком
    режиме; неизвестный режим трактуется как manual (самый мягкий) — включить
    блокировку можно только сознательно выбранным режимом, а не опечаткой.
    """
    if verdict is None or not verdict.needs_review:
        return False, []
    m = mode if mode in AUTONOMY_MODES else AUTONOMY_MANUAL
    if m == AUTONOMY_MANUAL:
        return False, []
    if m == AUTONOMY_AUTONOMOUS:
        return True, list(verdict.reasons)
    # semi: только жёсткие нарушения бренда
    hard = [
        v for v in (verdict.brand or {}).get("violations", [])
        if v.get("code") in _HARD_BRAND_CODES
    ]
    if hard:
        return True, [violation_text(v) for v in hard]
    return False, []
