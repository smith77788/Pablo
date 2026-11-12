"""Манера персоны, ЗАМЕРЕННАЯ по образцам, а не описанная словами.

«Пиши коротко и небрежно» модель понимает по-своему: её «коротко» — это два
предложения, а «небрежно» — аккуратный текст без точки в конце. Живой человек
пишет иначе, и это видно в цифрах: обычная длина реплики, доля сообщений со
строчной буквы, точка в конце, вопросы, смайлики, свои словечки. Здесь эти
цифры снимаются с образцов (их вводит владелец или берёт из СВОИХ переписок),
превращаются в конкретные правила для модели и потом исполняются над её
ответом — то, что можно проверить, не просится, а делается.

Всё в модуле ЧИСТОЕ: ни базы, ни сети.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Any, Optional

MIN_SAMPLES = 3            # меньше — это не манера, а случайность
SAMPLES_KEEP = 30          # сколько образцов хранить у персоны
SAMPLE_LEN = 200

_EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF⭐⬆↔-↪]")
_WORD_RE = re.compile(r"[a-zа-яё]+", re.IGNORECASE)
_LINK_RE = re.compile(r"(https?://|t\.me/|www\.|@\w{4,})", re.IGNORECASE)

# Слова, которые есть у всех: по ним манеру не отличить.
_COMMON = frozenset("""
и в во не на я ты он она мы вы они что как а но да нет это то так же бы ли
у к с со по за из от до для о об про при над под все всё его её их мне меня
тебя тебе там тут вот если или уже еще ещё был была было были есть будет
можно нужно надо очень просто только даже когда где кто чем чего этот эта эти
""".split())

# Разговорные начала реплики: их в чате пишут строчными.
_CHATTY = frozenset("""
ну ок окей ага угу хз короче слушай кстати привет здорово блин типа вообще
согласен согласна думаю наверное походу видимо честно ладно спасибо спс норм
""".split())


def _clean(t: str) -> str:
    return " ".join(str(t or "").split())


def _first_alpha(t: str) -> str:
    for ch in t:
        if ch.isalpha():
            return ch
    return ""


def _pct(values: list[int], q: float) -> int:
    if not values:
        return 0
    s = sorted(values)
    i = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
    return int(s[i])


def fingerprint(samples: list[str]) -> Optional[dict[str, Any]]:
    """Замер манеры по образцам. None — образцов меньше MIN_SAMPLES."""
    texts = [_clean(x) for x in (samples or []) if _clean(x)]
    if len(texts) < MIN_SAMPLES:
        return None
    n = len(texts)
    lens = [len(t) for t in texts]
    alpha = [t for t in texts if _first_alpha(t)]
    lower = sum(1 for t in alpha if _first_alpha(t).islower())
    period = sum(1 for t in texts if t.endswith(".") and not t.endswith(".."))
    question = sum(1 for t in texts if "?" in t)
    exclaim = sum(1 for t in texts if "!" in t)
    ellipsis = sum(1 for t in texts if "..." in t or "…" in t)
    with_emoji = sum(1 for t in texts if _EMOJI_RE.search(t))
    # Словечки: слово, которое встречается хотя бы в двух образцах и не из
    # общих. Считаем по образцам, а не по вхождениям: «ну ну ну» в одном
    # сообщении — не привычка.
    per_msg = Counter()
    openers = Counter()
    for t in texts:
        words = [w.lower() for w in _WORD_RE.findall(t)]
        per_msg.update(set(w for w in words if w not in _COMMON and len(w) <= 12))
        if words:
            openers[words[0]] += 1
    pet = [w for w, c in per_msg.most_common(12) if c >= 2][:6]
    open_ = [w for w, c in openers.most_common(4) if c >= 2 and w not in _COMMON][:3]
    return {
        "n": n,
        "len_typical": _pct(lens, 0.5),
        "len_max": max(_pct(lens, 0.9), 12),
        "lower_start": round(lower / len(alpha), 2) if alpha else 0.0,
        "end_period": round(period / n, 2),
        "question": round(question / n, 2),
        "exclaim": round(exclaim / n, 2),
        "ellipsis": round(ellipsis / n, 2),
        "emoji": round(with_emoji / n, 2),
        "pet_words": pet,
        "openers": open_,
    }


def _share(x: float) -> str:
    if x >= 0.85:
        return "почти всегда"
    if x >= 0.6:
        return "обычно"
    if x >= 0.35:
        return "примерно через раз"
    if x >= 0.12:
        return "изредка"
    return "почти никогда"


def rules(fp: Optional[dict[str, Any]]) -> str:
    """Замер манеры → конкретные правила для модели. '' — замера нет."""
    if not fp:
        return ""
    r = [f"Длина реплики — как в образцах: обычно около {fp['len_typical']} знаков, "
         f"не длиннее {fp['len_max']}."]
    r.append("Со строчной буквы начинаешь " + _share(fp["lower_start"]) + ".")
    r.append("Точку в конце ставишь " + _share(fp["end_period"]) + ".")
    r.append("Смайлики — " + _share(fp["emoji"]) + ".")
    if fp["question"] >= 0.35:
        r.append("Часто спрашиваешь в ответ.")
    if fp["exclaim"] < 0.12:
        r.append("Восклицательных знаков почти не ставишь.")
    if fp["ellipsis"] >= 0.25:
        r.append("Любишь многоточие.")
    if fp.get("pet_words"):
        r.append("Твои словечки (вставляй изредка, к месту): "
                 + ", ".join(fp["pet_words"]) + ".")
    if fp.get("openers"):
        r.append("Часто начинаешь с: " + ", ".join(fp["openers"]) + ".")
    return "Замер твоей манеры по твоим же сообщениям — держись его: " + " ".join(r)


def _cut(text: str, limit: int) -> str:
    """Укоротить до limit по границе предложения, иначе по слову."""
    if len(text) <= limit:
        return text
    parts = re.split(r"(?<=[.!?…])\s+", text)
    out = ""
    for p in parts:
        cand = (out + " " + p).strip()
        if len(cand) > limit:
            break
        out = cand
    if out:
        return out
    cut = text[:limit]
    if " " in cut:
        cut = cut[:cut.rfind(" ")]
    return cut.rstrip(" ,;:-")


def conform(text: str, fp: Optional[dict[str, Any]]) -> str:
    """Довести ответ модели до замеренной манеры. Не делает текст пустым."""
    t = _clean(text)
    if not t or not fp:
        return t
    # Длина: запас в полтора раза — живой человек тоже иногда пишет длиннее.
    t = _cut(t, max(int(fp["len_max"] * 1.5), 24)) or t
    if fp["emoji"] == 0 and fp["n"] >= 5:
        t = _clean(_EMOJI_RE.sub("", t)) or t
    if fp["end_period"] <= 0.15 and t.endswith(".") and not t.endswith(".."):
        t = t[:-1].rstrip()
    if fp["lower_start"] >= 0.75:
        m = re.match(r"\W*([A-Za-zА-Яа-яЁё]+)", t)
        # Опускаем только служебное слово: имя собственное («Казань норм»)
        # строчным не пишет и тот, кто всё пишет строчными, а отличить имя без
        # словаря нельзя — поэтому список, а не догадка.
        w = m.group(1) if m else ""
        if w and w[0].isupper() and not (len(w) > 1 and w.isupper()) and (
                m.group(1).lower() in _COMMON or m.group(1).lower() in _CHATTY
                or m.group(1).lower() in fp.get("openers", [])):
            i = m.start(1)
            t = t[:i] + t[i].lower() + t[i + 1:]
    return t


def summary(fp: Optional[dict[str, Any]]) -> str:
    """Замер одной строкой для экрана (по-русски)."""
    if not fp:
        return ""
    bits = [f"обычно {fp['len_typical']} знаков",
            f"со строчной {_share(fp['lower_start'])}",
            f"точка в конце {_share(fp['end_period'])}",
            f"смайлики {_share(fp['emoji'])}"]
    if fp.get("pet_words"):
        bits.append("словечки: " + ", ".join(fp["pet_words"][:4]))
    return f"По {fp['n']} образцам: " + "; ".join(bits)


def pick_samples(texts: list[str], limit: int = SAMPLES_KEEP) -> list[str]:
    """Образцы манеры из живой переписки: без ссылок, упоминаний, пустых и
    служебных, без повторов, разной длины. ЧИСТАЯ."""
    seen: set[str] = set()
    good: list[str] = []
    for raw in texts or []:
        t = _clean(raw)
        if not (3 <= len(t) <= SAMPLE_LEN) or _LINK_RE.search(t):
            continue
        if not _WORD_RE.search(t) or re.search(r"\d{5,}", t):
            continue   # номера телефонов, карт, кодов — не манера, а данные
        key = t.lower()
        if key in seen:
            continue
        seen.add(key)
        good.append(t)
    if len(good) <= limit:
        return good
    # Равномерно по длине: и короткие «ок», и развёрнутые — всё это манера.
    good.sort(key=len)
    step = len(good) / limit
    return [good[int(i * step)] for i in range(limit)]
