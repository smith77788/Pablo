"""Генерация имён и @юзернеймов перестановкой ключевых слов (SEO-массив).

Задача владельца: держать свои каналы/чаты/боты в топе поиска Telegram по многим
ключам. Приём — создавать десятки ресурсов с ОДНИМИ ключами, но по-разному:
менять слова местами и добавлять буквы. Тогда одними ресурсами занимаешь много
мест выдачи по многим сочетаниям ключей.

Раньше массовое создание давало «Канал #1, Канал #2» — бесполезно для поиска.
Здесь — движок, который из шаблона делает поток РАЗНЫХ валидных вариантов:

  шаблон @Dostavka_Moskva →
    Dostavka_Moskva, Moskva_Dostavka, Dostavkaj_Moskva, Dostavka_Moskvajv,
    moskva_dostavkagy, moskvaj_dostavka, …

Приёмы (в порядке нарастания «расстояния» от базы):
  1. перестановка порядка ключевых слов (все уникальные порядки);
  2. добавление 1–2 букв к одному из слов (как это делает владелец вручную).

Только чистые функции (их и проверяют тесты); сеть/БД — у вызывающего.

Важно про юзернеймы Telegram: они РЕГИСТРОНЕЗАВИСИМЫ (@Moskva и @moskva — один и
тот же). Поэтому уникальность даёт ПОРЯДОК слов и добавленные буквы, а не регистр;
кандидаты сравниваем в нижнем регистре и дедупим. Имена (заголовки) регистр и
повтор допускают — там просто крутим порядок слов.
"""
from __future__ import annotations

import random
import re
from itertools import permutations

# Юзернейм Telegram: 5–32, буква в начале, [A-Za-z0-9_], без хвостового «_» и без
# сдвоенных «__». Ботам нужен суффикс «bot» — задаётся require_suffix.
_USERNAME_RE = re.compile(r"^[a-z][a-z0-9_]{3,30}[a-z0-9]$")
USERNAME_MIN = 5
USERNAME_MAX = 32
_NOISE = "abcdefghijklmnopqrstuvwxyz"
_SEP_RE = re.compile(r"[\s_\-\+]+")


def split_keywords(template: str) -> list[str]:
    """Разбить шаблон на ключевые слова по разделителям (_ пробел - +).
    С ведущего @ снимаем. Пустые отбрасываем."""
    t = (template or "").strip().lstrip("@")
    parts = [p for p in _SEP_RE.split(t) if p]
    return parts


def valid_username(u: str, *, require_suffix: str | None = None) -> bool:
    if not u:
        return False
    u = u.lower()
    if not (USERNAME_MIN <= len(u) <= USERNAME_MAX):
        return False
    if "__" in u:
        return False
    if not _USERNAME_RE.match(u):
        return False
    if require_suffix and not u.endswith(require_suffix.lower()):
        return False
    return True


def _ordered_permutations(words: list[str]) -> list[tuple[str, ...]]:
    """Уникальные перестановки слов в устойчивом порядке (base — первым)."""
    seen: set = set()
    out: list = []
    # itertools.permutations в лексикографическом порядке индексов — база первой.
    for p in permutations(words):
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _grow_word(word: str, rng: random.Random, n: int) -> str:
    """Добавить n «шумовых» букв в конец слова (как владелец: Dostavka→Dostavkaj)."""
    return word + "".join(rng.choice(_NOISE) for _ in range(n))


def username_candidates(template: str, *, seed: int = 0,
                        require_suffix: str | None = None):
    """Бесконечный поток РАЗНЫХ валидных @юзернеймов из шаблона (в нижнем
    регистре, без @). Порядок: сначала перестановки, потом с добавленными
    буквами. Дедуп внутри. Останавливается, только если вычерпал разумный запас.
    """
    words = [w.lower() for w in split_keywords(template)]
    if not words:
        return
    sep = "_"
    perms = _ordered_permutations(words)
    seen: set = set()

    def _emit(u: str):
        u = u.lower()
        if u in seen:
            return None
        if not valid_username(u, require_suffix=require_suffix):
            return None
        seen.add(u)
        return u

    # Тир 0: чистые перестановки.
    for p in perms:
        cand = sep.join(p)
        if require_suffix and not cand.endswith(require_suffix.lower()):
            cand = cand + require_suffix.lower()
        r = _emit(cand)
        if r:
            yield r

    # Тир 1+: добавляем буквы к одному из слов. Растим запас, пока хватает.
    rng = random.Random(f"{seed}:{template}")
    grow_len = 1
    stale = 0
    while grow_len <= 4 and stale < 400:
        produced_this_round = False
        for p in perms:
            for wi in range(len(p)):
                for _ in range(3):                # несколько вариантов букв
                    parts = list(p)
                    parts[wi] = _grow_word(parts[wi], rng, grow_len)
                    cand = sep.join(parts)
                    if require_suffix and not cand.endswith(require_suffix.lower()):
                        cand = cand + require_suffix.lower()
                    r = _emit(cand)
                    if r:
                        produced_this_round = True
                        stale = 0
                        yield r
                    else:
                        stale += 1
        if not produced_this_round:
            grow_len += 1
        else:
            grow_len += 1


def generate_usernames(template: str, count: int, *, seed: int = 0,
                       taken=(), require_suffix: str | None = None) -> list[str]:
    """count РАЗНЫХ валидных @юзернеймов (в нижнем регистре, без @), не попадающих
    в taken (сравнение регистронезависимое)."""
    taken_l = {str(t).lower().lstrip("@") for t in (taken or ())}
    out: list[str] = []
    for u in username_candidates(template, seed=seed, require_suffix=require_suffix):
        if u in taken_l:
            continue
        out.append(u)
        if len(out) >= count:
            break
    return out


def generate_titles(template: str, count: int, *, seed: int = 0) -> list[str]:
    """count заголовков-имён перестановкой ключей (регистр слов сохраняем). Имена
    в Telegram не обязаны быть уникальны, но для охвата поиска делаем их разными:
    крутим порядок, при нехватке — добавляем букву к слову (как в юзернеймах)."""
    words = split_keywords(template)
    if not words:
        return [template.strip() or "Channel"] * count if count > 0 else []
    if len(words) == 1:
        # Одно слово: имя можно повторять, но для разнообразия добавляем буквы.
        base = words[0]
        out = [base]
        rng = random.Random(f"{seed}:{template}:title")
        while len(out) < count:
            out.append(_grow_word(base, rng, 1 + len(out) % 2))
        return out[:count]

    perms = _ordered_permutations(words)
    out: list[str] = []
    for p in perms:
        out.append(" ".join(p))
        if len(out) >= count:
            return out
    # Перестановки кончились — добавляем буквы к слову, продолжая крутить порядок.
    rng = random.Random(f"{seed}:{template}:title")
    grow_len = 1
    while len(out) < count and grow_len <= 4:
        for p in perms:
            parts = list(p)
            wi = rng.randrange(len(parts))
            parts[wi] = _grow_word(parts[wi], rng, grow_len)
            out.append(" ".join(parts))
            if len(out) >= count:
                return out
        grow_len += 1
    return out[:count]
