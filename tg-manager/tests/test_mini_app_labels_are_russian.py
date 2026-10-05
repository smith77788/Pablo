"""Подписи мини-аппа — по-русски. Владелец не читает по-английски.

Это требование корневого CLAUDE.md, но держалось оно только вниманием: в
интерфейс раз за разом просачивались английские подписи там, где значение
приходит ключом из кода — режимы прогрева («🌱 Gentle»), стиль персоны
(«😊 Casual»), роли ботов («🚪 Entry»), имя ошибки Telegram («⛔ PeerFlood»),
подпись поля («Username»), уровень губернатора («green»).

Ищем не любую латиницу в коде — идентификаторы, CSS, пути и URL нормальны, — а
ЗНАЧЕНИЯ словарей-подписей: то, что подставляется в строку экрана как готовый
текст. Признак подписи для человека: пробел или эмодзи внутри значения.

Измеритель проверяет себя: test_detector_catches_a_planted_label подсовывает
заведомо английскую подпись и требует, чтобы она нашлась. Без этого тест мог бы
«позеленеть» от собственной поломки.
"""
from __future__ import annotations

import glob
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Слова, которые по-русски не переводят: имена сервисов, единицы, аббревиатуры,
# названия языков на самих языках, тарифы продукта.
ALLOWED_WORDS = {
    # Telegram и платежи
    "telegram", "stars", "ton", "usdt", "trc", "trc20", "bot", "premium",
    # технические сокращения, принятые в русском тексте
    "api", "id", "ip", "url", "csv", "json", "vcf", "html", "sms", "qr", "ttl",
    "seo", "er", "cpm", "crm", "dm", "utm", "http", "https", "socks5", "vpn",
    "proxy", "ok", "mini", "app", "session", "tdata",
    # модели и движки ИИ
    "ai", "gpt", "claude", "groq", "opus", "sonnet", "haiku",
    # тарифы продукта
    "starter", "pro", "enterprise", "strike", "infragram", "host", "server",
    # названия языков пишутся на самих языках
    "english", "deutsch", "français", "espanol", "español", "italiano",
    "portugues", "português",
    # одиночные буквы-множители и подписи осей
    "x", "v", "n", "a", "b",
}

_LABEL_RE = re.compile(r"""(\w+)\s*:\s*(?:'([^'\\\n]{2,70})'|"([^"\\\n]{2,70})")""")
_EMOJI_RE = re.compile(r"[\U0001F300-\U0001FAFF☀-➿⬀-⯿]")
# Значения, которые подписями не являются: куски CSS, разметки, путей, чисел.
# Флаг страны (пара региональных индикаторов) — значит это название языка, а их
# пишут на самих языках: «🇫🇷 Français» переводить не нужно.
_FLAG_RE = re.compile(r"[\U0001F1E6-\U0001F1FF]{2}")
# Значения, которые подписями не являются: куски CSS, разметки, путей, чисел, а
# также обрывки склейки строк вида «' + x + '» — в кавычки там попал код.
_NOT_A_LABEL_RE = re.compile(
    r"[</>{}();=#]|\.(?:js|css|png|svg|mjs)\b|^var\(|^#|^\d|\s\+\s|^\s*\+|\+\s*$|\s:\s*$")


def _sources() -> list[tuple[str, str]]:
    files = [os.path.join(ROOT, "mini_app", "index.html")]
    files += sorted(glob.glob(os.path.join(ROOT, "mini_app", "screens", "*.js")))
    return [(os.path.relpath(f, ROOT), open(f, encoding="utf-8").read()) for f in files]


def _english_labels(name: str, src: str) -> list[tuple[int, str, str]]:
    """Подписи, в которых нет ни одной русской буквы, а есть английские слова."""
    out = []
    for m in _LABEL_RE.finditer(src):
        key = m.group(1)
        val = m.group(2) if m.group(2) is not None else m.group(3)
        if re.search(r"[А-Яа-яЁё]", val):
            continue                      # есть русский — подпись переведена
        if _NOT_A_LABEL_RE.search(val):
            continue                      # это не подпись, а код
        if _FLAG_RE.search(val):
            continue                      # название языка на самом языке
        words = re.findall(r"[A-Za-z][A-Za-z\-']{2,}", val)
        if not words:
            continue
        if not any(w.lower() not in ALLOWED_WORDS for w in words):
            continue                      # только допустимые слова
        if " " not in val and not _EMOJI_RE.search(val):
            continue                      # похоже на ключ/идентификатор, не на подпись
        line = src[: m.start()].count("\n") + 1
        out.append((line, key, val))
    return out


def test_detector_catches_a_planted_label():
    """Самопроверка измерителя: заведомо английская подпись обязана найтись."""
    planted = "const T = {gentle:'🌱 Gentle', standard:'🌿 Standard'};"
    found = _english_labels("<проверка>", planted)
    vals = {v for _, _, v in found}
    assert vals == {"🌱 Gentle", "🌿 Standard"}, found


def test_detector_does_not_flag_russian_or_technical_values():
    """Контроль ложных срабатываний на заведомо здоровых примерах."""
    ok = (
        "const A = {gentle:'🌱 Мягкий'};"
        "const B = {en:'🇬🇧 English', de:'🇩🇪 Deutsch'};"
        "const C = {price:'~15,0К ⭐ Stars'};"
        "const D = {style:'width:100%', bg:'var(--bg2)', ico:'📂 .session'};"
        "const E = {kind:'mass_invite'};"
        "const F = {color:' + tone.c + '};"
        "const G = {lang:'🇫🇷 Français'};"
    )
    assert _english_labels("<проверка>", ok) == []


def test_mini_app_has_no_english_labels():
    bad = []
    for name, src in _sources():
        for line, key, val in _english_labels(name, src):
            bad.append(f"{name}:{line}  {key} = {val!r}")
    assert not bad, (
        "английские подписи в интерфейсе — владелец их не читает:\n  "
        + "\n  ".join(bad)
    )


def test_labels_that_were_fixed_stay_fixed():
    """Точечно по тем подписям, которые уже просачивались."""
    html = open(os.path.join(ROOT, "mini_app", "index.html"), encoding="utf-8").read()
    for gone in ("🌱 Gentle", "🌿 Standard", "🔥 Intensive", "😊 Casual",
                 "⛔ PeerFlood", "🀄 CJK", "🚪 Entry", "💰 Conversion",
                 ">Username<", "Настроить welcome"):
        assert gone not in html, f"вернулась английская подпись: {gone}"
    for ru in ("🌱 Мягкий", "🌿 Обычный", "🔥 Интенсивный", "😊 Непринуждённый",
               "🀄 Иероглифы", "Имя в Telegram"):
        assert ru in html, f"пропала русская подпись: {ru}"
