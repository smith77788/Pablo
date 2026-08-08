"""Content Safety — жёсткий фильтр запрещённого контента (CSAM, терроризм).

Платформа Infragram НЕ обслуживает создание, публикацию и распространение
материалов сексуальной эксплуатации детей (CSAM) и террористического/
экстремистского контента. Любая попытка провести такой текст через
рассылку, пост в канал, воронку или генерацию контента — перехватывается
и блокируется на входе.

Назначение модуля — детект и блокировка. Он намеренно отделён от Strike
Module: Strike подаёт ЖАЛОБЫ на чужой нелегальный контент через официальные
каналы Telegram/NCMEC (легальная анти-абьюз функция), а content_safety не
даёт пользователю платформы СОЗДАВАТЬ такой контент.

Архитектура детекта — двухслойная:
  1. Детерминированный слой (regex/словарь) — работает всегда, без API-ключей.
     Это основной guard. Использует «сильные» паттерны (мгновенный блок) и
     «комбо» паттерны (нужно ≥2 независимых сигнала, чтобы исключить ложняк
     на безобидных словах).
  2. (Опционально) AI-классификатор — дополнительный слой, если доступен
     провайдер. Никогда не ослабляет вердикт детерминированного слоя.

Публичный API:
    verdict = scan_text(text)            # синхронно, без БД
    await enforce(pool, user_id, text, surface="broadcast")  # + аудит
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass

log = logging.getLogger(__name__)

CATEGORY_CSAM = "csam"
CATEGORY_TERROR = "terrorism"

# Единый текст отказа для пользователя — без эха совпавших терминов.
REFUSAL_TEXT = (
    "🚫 <b>Контент заблокирован</b>\n\n"
    "Платформа не обслуживает материалы, связанные с сексуальной "
    "эксплуатацией несовершеннолетних или террористической/экстремистской "
    "деятельностью. Операция отклонена и зафиксирована.\n\n"
    "Если вы считаете это ошибкой — переформулируйте текст."
)


@dataclass(frozen=True)
class SafetyVerdict:
    blocked: bool
    category: str | None = None
    rule: str | None = None  # технический id правила (для логов, не для пользователя)

    @property
    def ok(self) -> bool:
        return not self.blocked


_ALLOW = SafetyVerdict(blocked=False)


# ─── Нормализация ──────────────────────────────────────────────────────────────
# Убираем обходы: leet-замены, кириллица-маскировка под латиницу, разделители
# между буквами (c.p, c_p, c p), повтор пробелов/невидимых символов.

_HOMOGLYPHS = {
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "х": "x", "у": "y",
    "к": "k", "м": "m", "т": "t", "н": "h", "в": "b", "і": "i", "ї": "i",
    "ё": "e",
}
_LEET = {"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s"}


def _collapse(text: str) -> str:
    """Схлопывает разделители-обфускаторы между одиночными буквами:
    "c p" → "cp", "д.е.т" → "дет", "c/h/i/l/d" → "child". Кириллицу НЕ латинизирует.

    Любой не-словесный символ (пунктуация, эмодзи, спецсимволы) считается
    разделителем — раньше схлопывались только [\\s._\\-*], что оставляло
    тривиальный обход через "/", "|", ",", "#" и т.п. между буквами.
    """
    s = re.sub(r"(?<=\b\w)[\W_]+(?=\w\b)", "", text)
    return re.sub(r"\s+", " ", s)


def _latinize(text: str) -> str:
    """Маппит кириллические look-alike и leet в латиницу (для обхода-гомоглифов)."""
    out: list[str] = []
    for ch in text:
        if ch in _HOMOGLYPHS:
            out.append(_HOMOGLYPHS[ch])
        elif ch in _LEET:
            out.append(_LEET[ch])
        else:
            out.append(ch)
    return "".join(out)


def _haystacks(text: str) -> list[str]:
    """Три представления текста для устойчивого детекта:
      1. lower         — сырое (кириллица цела);
      2. collapse      — снятая разделительная обфускация, кириллица цела;
      3. latinize+coll — снятые гомоглифы/leet (латиница-маскировка)."""
    base = unicodedata.normalize("NFKC", text).lower()
    collapsed = _collapse(base)
    latin = _collapse(_latinize(base))
    seen: list[str] = []
    for h in (base, collapsed, latin):
        if h not in seen:
            seen.append(h)
    return seen


# ─── CSAM ──────────────────────────────────────────────────────────────────────
# «Сильные» паттерны — известные кодовые обозначения CSAM. Совпадение = блок.
_CSAM_STRONG = [
    r"\bc[\W_]?s[\W_]?a[\W_]?m\b",
    r"\bchild\s*p[o0]rn\w*",
    r"\bdetsk\w*\s+porn\w*",
    r"\bdetsk\w*\s+por[no]\w*",
    r"\bp[\W_]?t[\W_]?h[\W_]?c\b",
    r"\bp[e3]d[o0]phil\w*",
    r"\bp[e3]d[o0]\s*(?:hub|mom|file|content|video|photo)\w*",
    r"\bpreteen\s+(?:sex|nud|porn|model)\w*",
    r"\bjailbait\b",
    r"\bhebephil\w*",
    r"\bl[o0]li\s*(?:con|porn|nud|sex|hentai)\w*",
    r"\bsh[o0]ta\s*(?:con|porn|nud|sex)\w*",
    r"\bпедофил\w*",
    r"\bцп\b",  # рус. сленг «детское порно»
    r"\bдетск\w*\s+(?:порн|интим|эроти)\w*",
]

# Индикаторы несовершеннолетнего и сексуального контекста — для «комбо».
_MINOR = re.compile(
    r"\b(?:child|children|kid|kids|minor|minors|preteen|pre-teen|toddler|infant|"
    r"underage|under\s?age|year[\s-]?old|y[\s.]?o\b|"
    r"ребен|ребён|дет(?:и|ей|ск)|малол|подрост|несовершенно|школьниц)\w*",
    re.I,
)
_AGE_NUM = re.compile(r"\b([1-9]|1[0-7])\s*(?:yo|y\.?o\.?|year|лет|год)", re.I)
_SEXUAL = re.compile(
    r"\b(?:sex|sexy|nude|naked|nud[ie]|porn|xxx|erotic|fuck|rape|molest|"
    r"intercourse|genital|undress|stripping|"
    r"секс|порн|голы|обнаж|интим|эроти|изнасил|совращ|раздет)\w*",
    re.I,
)

# ─── Терроризм / экстремизм ─────────────────────────────────────────────────────
# Известные террористические организации (запрещены в РФ и/или признаны
# террористическими международно).
_TERROR_ORG = re.compile(
    r"\b(?:isis|isil|daesh|al[\s-]?qaeda|al[\s-]?qaida|taliban|boko\s?haram|"
    r"hamas|hezbollah|al[\s-]?shabaab|"
    r"игил|игиш|даиш|аль[\s-]?каида|аль[\s-]?каеда|талибан|"
    r"джабхат|ан[\s-]?нусра|хизб|вилаят)\w*",
    re.I,
)
# Намерение примкнуть/вербовать/прославлять.
_TERROR_INTENT = re.compile(
    r"\b(?:join|recruit|pledge|allegiance|bay[ae]t|jihad|martyr|shahid|caliphate|"
    r"вступ|вербов|присяг|джихад|шахид|халифат|муджахед|газават|"
    r"глориф|прославл|во\s?славу)\w*",
    re.I,
)
# Изготовление оружия массового поражения / подготовка атаки.
_WEAPON_MAKE = re.compile(
    r"\b(?:make|build|assemble|construct|how\s+to\s+(?:make|build)|recipe\s+for|"
    r"сделать|изготов|собрать|смастер|инструкц\w*\s+по|как\s+сделать)\b",
    re.I,
)
_WEAPON_OBJ = re.compile(
    r"\b(?:bomb|explosive|ied|detonator|c4|c-4|tatp|semtex|pipe\s?bomb|"
    r"suicide\s?(?:vest|belt|bomb)|nerve\s?agent|sarin|anthrax|dirty\s?bomb|"
    r"бомб|взрывчат|взрывн\w*\s+устройст|сву|детонат|пояс\s?шахид|"
    r"отравляющ\w*\s+вещест|зарин|нервно[\s-]?паралит)\w*",
    re.I,
)
# Призывы к массовому насилию / терактам.
_TERROR_CALL = re.compile(
    r"\b(?:terror\s?attack|mass\s?(?:shooting|killing|murder)|massacre|"
    r"behead\w*|ethnic\s?cleansing|genocide|"
    r"теракт|террористическ\w*\s+акт|массов\w*\s+(?:убийств|расстрел|казн)|"
    r"взорв\w*\s+(?:толп|людей|здани|метро|вокзал)|"
    r"геноцид|этническ\w*\s+чистк|обезглав)\w*",
    re.I,
)


# «cp» / «pedo» как аббревиатуры — только рядом со словами распространения,
# чтобы не ловить безобидное "cp" (copy, cost-per, checkpoint).
_CSAM_ABBR = re.compile(r"\b(?:cp|pedo)\b", re.I)
_DISTRIB = re.compile(
    r"\b(?:collection|colection|pack|vid|video|vids|link|links|set|mega|folder|"
    r"archive|leak|dump|gallery|preview|trade|sell|buy|"
    r"коллекц|подборк|архив|ссылк|слив|купить|продам|обмен)\w*",
    re.I,
)


def _near(a: re.Match, b: re.Match, window: int = 60) -> bool:
    """True, если два совпадения находятся в пределах window символов."""
    return abs(a.start() - b.start()) <= window


def _combo(pat_a: re.Pattern, pat_b: re.Pattern, hay: str, window: int) -> bool:
    """True, если в строке есть совпадение pat_a рядом с pat_b."""
    a_hits = list(pat_a.finditer(hay))
    if not a_hits:
        return False
    b_hits = list(pat_b.finditer(hay))
    for a in a_hits:
        for b in b_hits:
            if _near(a, b, window):
                return True
    return False


def scan_text(text: str | None) -> SafetyVerdict:
    """Синхронный детект. Никогда не бросает исключений.

    Каждый паттерн проверяется и по сырому тексту (для кириллицы), и по
    нормализованному (для латиницы-обфускации/гомоглифов), т.к. нормализация
    намеренно латинизирует кириллические look-alike символы.
    """
    if not text or not text.strip():
        return _ALLOW
    try:
        hays = _haystacks(text)

        # ── CSAM: сильные паттерны ────────────────────────────────────────────
        for hay in hays:
            for pat in _CSAM_STRONG:
                if re.search(pat, hay, re.I):
                    return SafetyVerdict(True, CATEGORY_CSAM, "csam_strong")

        # ── CSAM: аббревиатура (cp/pedo) рядом со словом распространения ───────
        for hay in hays:
            if _combo(_CSAM_ABBR, _DISTRIB, hay, window=40):
                return SafetyVerdict(True, CATEGORY_CSAM, "csam_abbr")

        # ── CSAM: комбо (минор + сексуальный контекст рядом) ──────────────────
        for hay in hays:
            sexual = list(_SEXUAL.finditer(hay))
            if not sexual:
                continue
            minor_hits = list(_MINOR.finditer(hay)) + list(_AGE_NUM.finditer(hay))
            for m in minor_hits:
                for s in sexual:
                    if _near(m, s, window=80):
                        return SafetyVerdict(True, CATEGORY_CSAM, "csam_combo")

        # ── Терроризм: инструкция по изготовлению оружия ──────────────────────
        for hay in hays:
            if _combo(_WEAPON_MAKE, _WEAPON_OBJ, hay, window=80):
                return SafetyVerdict(True, CATEGORY_TERROR, "terror_weapon")

        # ── Терроризм: призыв к массовому насилию / теракту ───────────────────
        for hay in hays:
            if _TERROR_CALL.search(hay):
                return SafetyVerdict(True, CATEGORY_TERROR, "terror_call")

        # ── Терроризм: террор-организация + намерение примкнуть/славить ────────
        for hay in hays:
            if _combo(_TERROR_ORG, _TERROR_INTENT, hay, window=100):
                return SafetyVerdict(True, CATEGORY_TERROR, "terror_org_intent")

        return _ALLOW
    except Exception as e:  # детект никогда не должен ронять основной поток
        log.debug("content_safety.scan_text error: %s", e)
        return _ALLOW


def scan_many(*texts: str | None) -> SafetyVerdict:
    """Проверяет несколько полей (заголовок, описание, текст поста). Блок при первом."""
    for t in texts:
        v = scan_text(t)
        if v.blocked:
            return v
    return _ALLOW


async def enforce(
    pool,
    user_id: int | None,
    *texts: str | None,
    surface: str = "content",
) -> SafetyVerdict:
    """Проверяет контент и при блокировке пишет запись в compliance-аудит.

    `surface` — где сработало (broadcast/funnel/post/...), идёт в лог операции.
    Возвращает вердикт; решение (показать REFUSAL_TEXT и прервать) — на вызывающем.
    """
    verdict = scan_many(*texts)
    if verdict.blocked:
        log.warning(
            "content_safety BLOCK user=%s surface=%s category=%s rule=%s",
            user_id,
            surface,
            verdict.category,
            verdict.rule,
        )
        try:
            from services import compliance_engine

            await compliance_engine.record(
                pool,
                user_id,
                None,
                op_type=f"content_block:{surface}",
                outcome="blocked",
                params={"category": verdict.category, "rule": verdict.rule},
            )
        except Exception as e:
            log.debug("content_safety.enforce audit failed: %s", e)
    return verdict
