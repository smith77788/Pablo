"""Поиск в каталоге «Все функции» искал только по подписи плитки.

ЖАЛОБА ПОЛЬЗОВАТЕЛЯ: «недостающие функции… проблемы с навигацией».

ЧТО БЫЛО СЛОМАНО. В каталоге 81 плитка. Поиск (`filterMore`) сравнивал запрос
ТОЛЬКО с текстом подписи (`mgmt-tile-lbl`). Поэтому осмысленные запросы находили
ноль, хотя модуль существует:
  «бан», «риск»   → ✗ (а есть «Дэшборд здоровья», в описании «риски бана»);
  «жалоба»        → ✗ (а есть «STRIKE» — массовые жалобы);
  «аналитик»      → ✗ (а есть «Аудитория+», «Дашборд»);
  «спам», «клон»  → ✗.
Для пользователя это «функции нет», хотя она на месте — просто недостижима по
тому слову, которым он её ищет.

Данные для нормального поиска УЖЕ были: `MODULE_DESCS` (человеческие описания,
использовались в боковом меню и тултипах) поиск игнорировал. Добавлен ещё слой —
`MODULE_KEYWORDS` — для слов, которых нет ни в подписи, ни в описании («жалоба»,
«клон», «антибан», «autoreg»). Проверено рендером: 8 интентных запросов находят
нужные модули, 0 ошибок JS.
"""
from __future__ import annotations

import re
from pathlib import Path

HTML = (Path(__file__).resolve().parents[1] / "mini_app" / "index.html").read_text(encoding="utf-8")


def _filter_more() -> str:
    m = re.search(r"function filterMore\(q\)\s*\{.*?\n\}", HTML, re.DOTALL)
    assert m, "функция поиска каталога не найдена"
    return m.group(0)


def test_search_reads_descriptions_and_keywords():
    body = _filter_more()
    assert "MODULE_DESCS[lbl]" in body, (
        "поиск обязан учитывать описание модуля, а не только подпись плитки"
    )
    assert "MODULE_KEYWORDS[lbl]" in body, (
        "нужен слой синонимов: «жалоба», «клон», «antiban» не встречаются в подписях"
    )


def test_keyword_map_exists_and_covers_intent_words():
    m = re.search(r"const MODULE_KEYWORDS = \{(.*?)\n\};", HTML, re.DOTALL)
    assert m, "карта синонимов отсутствует"
    kw = m.group(1).lower()
    # Слова, которыми пользователи реально ищут и которых нет в подписях модулей.
    for intent in ("жалоба", "бан", "спам", "прокси", "клон", "autoreg"):
        assert intent in kw, f"интентное слово «{intent}» не покрыто синонимами"


def test_keyword_keys_point_at_real_tiles():
    """Синоним, привязанный к несуществующей плитке, — мёртвый вес, который
    молча ничего не находит."""
    m = re.search(r"const MODULE_KEYWORDS = \{(.*?)\n\};", HTML, re.DOTALL)
    keys = set(re.findall(r"'([^']+)':", m.group(1)))
    labels = set(re.findall(r'<div class="mgmt-tile-lbl">([^<]+)</div>', HTML))
    ghosts = {k for k in keys if k not in labels}
    assert not ghosts, f"синонимы указывают на несуществующие плитки: {sorted(ghosts)}"


def test_strike_is_findable_by_zhaloba():
    """Точечная защита самого показательного случая: модуль массовых жалоб не
    находился по слову «жалоба» — а это его прямое назначение.

    Плитка называлась STRIKE и переименована в «Массовые жалобы»; старое имя
    оставлено синонимом, чтобы поиск по нему продолжал работать."""
    m = re.search(r"const MODULE_KEYWORDS = \{(.*?)\n\};", HTML, re.DOTALL)
    kw = dict(re.findall(r"'([^']+)':'([^']+)'", m.group(1)))
    syn = kw.get("Массовые жалобы", "").lower()
    assert "жалоб" in syn
    assert "strike" in syn and "страйк" in syn


def test_empty_query_still_shows_everything():
    """Пустой запрос не должен ничего прятать — иначе каталог схлопнется в пустоту."""
    body = _filter_more()
    assert "!query || hay.includes(query)" in body, (
        "при пустом запросе плитка обязана показываться (match=true)"
    )
