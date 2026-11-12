"""Агент роста учится на исходах своих публикаций.

Раньше каждый запуск начинал с нуля: запросы, которые ни разу не привели ни к
одной публикации (Telegram находит по ним закрытые или чужие по теме группы),
пробовались снова, а о судьбе поста — живёт ли он или его удалили админы через
час — система не узнавала никогда. Здесь — чистая часть обучения:

* `keyword_stats` — по журналу `niche_growth_targets`: сколько раз группа по
  этому запросу была взята в работу, сколько дало живую публикацию, сколько
  постов удалили;
* `order_keywords` — запросы с доказанно нулевой отдачей отсеиваются,
  остальные идут по убыванию отдачи; новые (без истории) — посередине.

Проверка судьбы поста (жив ли, просмотры) — `check_posts_due` в op_worker,
через аккаунт, который публиковал.
"""
from __future__ import annotations

from typing import Iterable

# Сколько попыток по запросу без единой живой публикации достаточно, чтобы
# перестать его пробовать. Меньше — шум одной неудачи, больше — повторы впустую.
DEAD_KEYWORD_TRIES = 3
# Меньше этого запросы не режутся: без запросов поиск не найдёт ничего.
MIN_KEYWORDS = 3
# Через сколько часов после публикации смотреть, жив ли пост.
CHECK_AFTER_HOURS = 6
CHECK_BATCH = 10


def _kw(k) -> str:
    return " ".join(str(k or "").lower().split())


def keyword_stats(rows: Iterable[dict]) -> dict[str, dict]:
    """{запрос: {tries, good, deleted}} из строк журнала.

    good — публикация прошла и пост не удалён (или ещё не проверялся);
    deleted — публикация прошла, но пост потом исчез (удалили админы — сигнал
    спама в этой нише по этому запросу)."""
    out: dict[str, dict] = {}
    for r in rows:
        k = _kw(r.get("keyword"))
        if not k:
            continue
        st = out.setdefault(k, {"tries": 0, "good": 0, "deleted": 0})
        st["tries"] += 1
        if r.get("outcome") == "posted":
            if r.get("alive") is False:
                st["deleted"] += 1
            else:
                st["good"] += 1
    return out


def keyword_score(st: dict | None) -> float:
    """Доля хороших исходов с априорной половиной (новый запрос = 0.5)."""
    if not st:
        return 0.5
    return (st["good"] + 0.5) / (st["tries"] + 1.0)


def order_keywords(keywords: list[str], stats: dict[str, dict]) -> tuple[list[str], list[str]]:
    """(порядок запросов, отсеянные). Отсеиваются запросы с ≥ DEAD_KEYWORD_TRIES
    попыток и ни одной живой публикации — но так, чтобы осталось не меньше
    MIN_KEYWORDS (лучшие из отсеянных возвращаются)."""
    kws = [k for k in keywords if _kw(k)]
    dead = [k for k in kws
            if (stats.get(_kw(k)) or {}).get("tries", 0) >= DEAD_KEYWORD_TRIES
            and not (stats.get(_kw(k)) or {}).get("good")]
    alive = [k for k in kws if k not in dead]
    if len(alive) < MIN_KEYWORDS:
        back = sorted(dead, key=lambda k: keyword_score(stats.get(_kw(k))), reverse=True)
        need = MIN_KEYWORDS - len(alive)
        alive += back[:need]
        dead = back[need:]
    alive.sort(key=lambda k: keyword_score(stats.get(_kw(k))), reverse=True)
    return alive, dead
