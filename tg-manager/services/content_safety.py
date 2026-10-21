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


_TO_CYR = {v: k for k, v in _HOMOGLYPHS.items() if k not in ("і", "ї", "ё")}
_LEET_CYR = {"0": "о", "3": "е", "4": "а", "@": "а", "6": "б"}
_LATIN_RE = re.compile(r"[a-z]")
_CYR_RE = re.compile(r"[а-яё]")
_TOKEN_RE = re.compile(r"\S+")


def _is_masked(token: str) -> bool:
    """Токен похож на маскировку: смесь алфавитов или буквы вперемешку с цифрами.

    Обычное русское слово («ср», «сок», «тот») целиком из кириллицы маскировкой
    не считается — раньше его латинизировали, и «Ср 20:00 … link» превращалось
    в «cp … link» (ложный CSAM).
    """
    has_lat = bool(_LATIN_RE.search(token))
    has_cyr = bool(_CYR_RE.search(token))
    has_digit = any(ch in _LEET for ch in token)
    return (has_lat and has_cyr) or ((has_lat or has_cyr) and has_digit)


def _latinize(text: str) -> str:
    """Маппит кириллические look-alike и leet в латиницу — только в замаскированных
    токенах («сp», «p0rn») и в латинских словах; чистую кириллицу не трогает."""
    def _tok(m: re.Match) -> str:
        t = m.group(0)
        if not (_is_masked(t) or _LATIN_RE.search(t)):
            return t
        return "".join(_HOMOGLYPHS.get(ch) or _LEET.get(ch) or ch for ch in t)
    return _TOKEN_RE.sub(_tok, text)


def _cyrillize(text: str) -> str:
    """Обратная маскировка: латинские look-alike и leet внутри русского слова
    («пeдoфил», «д3ти») возвращаются в кириллицу."""
    def _tok(m: re.Match) -> str:
        t = m.group(0)
        if not (_is_masked(t) and _CYR_RE.search(t)):
            return t
        return "".join(_TO_CYR.get(ch) or _LEET_CYR.get(ch) or ch for ch in t)
    return _TOKEN_RE.sub(_tok, text)


def _haystacks(text: str) -> list[str]:
    """Четыре представления текста для устойчивого детекта:
      1. lower         — сырое (кириллица цела);
      2. collapse      — снятая разделительная обфускация, кириллица цела;
      3. latinize+coll — снятые гомоглифы/leet (латиница-маскировка);
      4. cyrillize+coll — латиница/leet внутри русских слов → кириллица."""
    base = unicodedata.normalize("NFKC", text).lower()
    collapsed = _collapse(base)
    latin = _collapse(_latinize(base))
    cyr = _collapse(_cyrillize(base))
    seen: list[str] = []
    for h in (base, collapsed, latin, cyr):
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
    r"\bдетск\w*\s+(?:порн|интим|эроти|голы|обнаж|секс)\w*",
]

# Индикаторы несовершеннолетнего — для «комбо». Только формы-существительные:
# «kid» без границы ловил kidding/kidney, «minor» — minority, а «детский
# магазин» — любую детскую рекламу рядом со словом «сексуальное бельё».
_MINOR = re.compile(
    r"\b(?:child(?:ren)?|kids?|kiddies?|minors?|preteens?|pre-teens?|toddlers?|"
    r"infants?|underage|under\s?age|"
    r"реб[её]н\w*|дет(?:и|ей|ям|ьми|ях)|малолет\w*|малолетк\w*|подрост\w*|"
    r"несовершеннолет\w*|школьниц\w*|школьник\w*|девочк\w*|мальчик\w*)\b",
    re.I,
)
# Возраст человека: «12 yo», «12 year old», «12-летняя», либо «12 лет» рядом
# с лицом («девочка 12 лет», «ей 12 лет»). Голое «15 лет» — это чаще стаж
# («15 лет помогаю парам») и возрастом не считается.
_AGE_STRICT = re.compile(
    r"\b(?:[1-9]|1[0-7])\s*(?:y\.?o\b\.?|years?[\s-]old|-?летн\w*)",
    re.I,
)
_AGE_LOOSE = re.compile(r"\b(?:[1-9]|1[0-7])\s*(?:лет|годик\w*|года?|years?)\b", re.I)
_AGE_PERSON = re.compile(
    r"\b(?:девочк|мальчик|девушк|школьн|дочк|дочь|дочер|сын|ей|ему|мне|возраст|"
    r"girl|boy|daughter|son|aged|age)\w*\W{0,3}$",
    re.I,
)


def _age_hits(hay: str) -> list[re.Match]:
    """Числа, которые по контексту — возраст человека, а не срок/стаж/гарантия."""
    out: list[re.Match] = list(_AGE_STRICT.finditer(hay))
    for m in _AGE_LOOSE.finditer(hay):
        if _AGE_PERSON.search(hay[max(0, m.start() - 15):m.start()]):
            out.append(m)
    return out


# Сексуальный контекст делится на «откровенный» (порно, обнажёнка, насилие) и
# «бытовой» (секс, сексуальный, интим). Бытовые слова живут в рекламе белья,
# в сексологии, в половом воспитании — рядом с «ребёнком» они блокируют
# только вплотную и только вне просветительского/медицинского контекста.
_SEXUAL_EXPLICIT = re.compile(
    r"\b(?:porn\w*|nude\w*|nudes|naked|nudi\w*|xxx|erotic\w*|undress\w*|"
    r"molest\w*|rape|raped|raping|rapist\w*|intercourse|genital\w*|"
    r"порн\w*|обнаж\w*|раздет\w*|раздева\w*|изнасил\w*|совращ\w*|эроти\w*|"
    # «голы» без окончания — футбольные голы; «голые факты/голыми руками» — идиомы.
    r"гол(?:ый|ая|ое|ые|ых|ым|ыми|ой|ую|еньк\w*|ыш\w*)"
    r"(?!\s+(?:факт|правд|цифр|стен|рук|рукам|земл|степ|ветк|дерев|провод|ног)\w*))\b",
    re.I,
)
_SEXUAL_SOFT = re.compile(
    r"\b(?:sex|sexy|sexual\w*|fuck\w*|секс(?!олог|опатолог)\w*|интим\w*)\b",
    re.I,
)
_EDU_CONTEXT = re.compile(
    r"(?:воспитан|просвещ|говорить\s+о|поговорить|рассказ|объясн|урок|лекци|"
    r"гигиен|врач|педиатр|гинеколог|психолог|сексолог|защит|профилакт|"
    r"безопасност|насили\w*\s+над|education|talk\s+to|doctor|pediatric|"
    r"protect|prevent|awareness)",
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
# Намерение примкнуть/вербовать/прославлять. Только призывные/вербовочные
# формы: голое «вступ…» ловило новости «боевики ИГИЛ вступили в бой».
_TERROR_INTENT_CALL = re.compile(
    r"\b(?:join\w*|pledge\w*|allegiance|bay[ae]t|glory\s+to|support\s+(?:the\s+)?|"
    r"вступа(?:й|йте)|вступ(?:ить|айте|ай)\s+в\s+ряды|в\s+ряды|присоедин(?:яйся|яйтесь|иться)|"
    r"присяг\w*|баят|глориф\w*|прославл\w*|во\s?славу|слава|"
    r"поддерж(?:им|ите|ивай|ивайте|ать))",
    re.I,
)
# Идеологическая лексика: в новостях встречается рядом с названием группировки
# («ИГИЛ объявил джихад»), поэтому её гасит новостной/правоохранительный контекст.
_TERROR_INTENT_IDEOLOGY = re.compile(
    r"\b(?:recruit\w*|jihad\w*|martyr\w*|shahid\w*|caliphate|"
    r"вербов\w*|джихад\w*|шахид\w*|халифат\w*|муджахед\w*|газават\w*)",
    re.I,
)
# Изготовление оружия массового поражения / подготовка атаки.
_WEAPON_MAKE = re.compile(
    r"\b(?:make|build|assemble|construct|how\s+to\s+(?:make|build)|recipe\s+for|"
    r"сделать|изготов\w*|собрать|смастер\w*|инструкц\w*\s+по|как\s+сделать)\b",
    re.I,
)
# Однозначные объекты: взрывчатка, детонаторы, ОВ. Рядом с «сделать/собрать»
# в одном предложении — блок в любом порядке («самодельная бомба — как сделать»).
_WEAPON_OBJ = re.compile(
    r"\b(?:explosives|explosive\s+(?:device|material|charge|vest|belt)\w*|ied|"
    r"detonator\w*|c-?4\s+(?:explosive|charge)\w*|tatp|semtex|pipe\s?bombs?|"
    r"suicide\s?(?:vest|belt|bomb)\w*|nerve\s?agent\w*|sarin|anthrax|dirty\s?bombs?|"
    r"самодельн\w*\s+(?:бомб|взрыв)\w*|взрывчат\w*|взрывн\w*\s+устройст\w*|сву|"
    r"детонат\w*|пояс\w*\s?шахид\w*|тротил\w*|гексоген\w*|пластид\w*|"
    r"отравляющ\w*\s+вещест\w*|зарин\w*|нервно[\s-]?паралит\w*)\b",
    re.I,
)
# «Бомба» в рекламе — похвала («скидки — бомбы!», «the bomb»), «бомбер» —
# куртка, «бомбочка» — для ванны. Поэтому голая «бомба» считается оружием
# только как прямое дополнение: глагол изготовления стоит ПЕРЕД ней, близко и в
# том же предложении («сделать бомбу», «how to make a bomb»).
_WEAPON_BOMB = re.compile(
    r"\b(?:(?<!bath\s)(?<!seed\s)(?<!the\s)bombs?|бомб(?:у|ы|ой|ам|ами|ах)?)\b",
    re.I,
)
# Массовое насилие / теракт как событие. Само слово — это и новости, и история,
# и игры («Texas Chainsaw Massacre»), и маркетинг («price massacre»), поэтому
# блок только рядом с призывом/намерением в том же предложении.
_TERROR_EVENT = re.compile(
    r"\b(?:terror\s?attack\w*|mass\s?(?:shooting|killing|murder)\w*|massacre\w*|"
    r"behead\w*|ethnic\s?cleansing|genocide\w*|"
    r"теракт\w*|террористическ\w*\s+акт\w*|массов\w*\s+(?:убийств|расстрел|казн)\w*|"
    r"геноцид\w*|этническ\w*\s+чистк\w*|обезглав\w*|"
    r"взорв\w*\s+(?:толп|людей|здани|метро|вокзал|школ)\w*)",
    re.I,
)
# Призыв/намерение: 1-е лицо, императив, инфинитив цели. Прошедшее время
# («готовил», «совершили») — это уже репортаж, не призыв.
_CALL = re.compile(
    r"\b(?:устро(?:им|ить|ю|йте|й)|соверш(?:им|ить|и|ите)|готов(?:им|лю|ьте|ь)|"
    r"подготов(?:им|ить|ьте)|провед(?:ём|ем|ите)|провести|осуществ(?:им|ить)|"
    r"организу(?:ем|йте)|организовать|давайте|призыва\w*|"
    r"взорв(?:ать|ите|ём|ем|и|у)|убь(?:ём|ем)|убивайте|"
    r"let'?s|we\s+will|we'?ll|carry\s+out|commit|plan\s+(?:a|an|the|to)|"
    r"go\s+and|time\s+to|call\s+for|kill\s+(?:all|them|every)|death\s+to)\b",
    re.I,
)
# Новостной/правоохранительный контекст: сообщение О событии, а не призыв к нему.
_NEWS = re.compile(
    r"(?:предотвра|задерж|арест|осуд|осужд|приговор|обезвред|ликвидир|сообща|"
    r"по\s+данным|заяв|фсб|мвд|полици|следств|суд\b|жертв|погибш|памят|помним|"
    r"годовщин|соболезн|расследова|борьб\w*\s+с\s+террор|"
    r"prevent|arrest|foiled|convict|sentenc|police|fbi|according\s+to|"
    r"report|victims|memorial|condemn|counter-?terror)",
    re.I,
)


# «cp» / «цп» / «pedo» как аббревиатуры — только рядом со словами
# распространения, чтобы не ловить безобидное "cp" (copy, cost-per, checkpoint)
# и «ЦП» (центральный процессор).
_CSAM_ABBR = re.compile(r"\b(?:cp|цп|pedo)\b", re.I)
_DISTRIB = re.compile(
    r"\b(?:collection|colection|pack|vid|video|vids|link|links|set|mega|folder|"
    r"archive|leak|dump|gallery|preview|trade|sell|buy|"
    r"коллекц|подборк|архив|ссылк|слив|купить|продам|обмен)\w*",
    re.I,
)
_ABBR_BENIGN = re.compile(
    r"(?:intel|amd|ryzen|core\s*i\d|ghz|ггц|ядер|ядр[ао]|процессор|cpu|озу|\bram\b|"
    r"ssd|hdd|ноутбук|компьютер|\bпк\b|видеокарт|материнск|сокет|кулер|"
    r"cost|cpa|cpc|cpm|ctr|checkpoint|арбитраж|трафик)",
    re.I,
)

_SENTENCE_SPLIT = re.compile(r"[.!?…;\n]+")


def _sentences(hay: str) -> list[str]:
    return [p for p in _SENTENCE_SPLIT.split(hay) if p.strip()]


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


def _verb_before(verb: re.Pattern, obj: re.Pattern, hay: str, window: int) -> bool:
    """True, если obj стоит после verb не дальше window символов (прямое дополнение)."""
    verbs = list(verb.finditer(hay))
    if not verbs:
        return False
    for o in obj.finditer(hay):
        for v in verbs:
            if v.start() < o.start() and o.start() - v.end() <= window:
                return True
    return False


def _csam_combo(hay: str) -> bool:
    """Несовершеннолетний + сексуальный контекст рядом."""
    explicit = list(_SEXUAL_EXPLICIT.finditer(hay))
    soft = list(_SEXUAL_SOFT.finditer(hay))
    if not explicit and not soft:
        return False
    minors = list(_MINOR.finditer(hay)) + _age_hits(hay)
    if not minors:
        return False
    for m in minors:
        for s in explicit:
            if _near(m, s, window=80):
                return True
    if soft and not _EDU_CONTEXT.search(hay):
        for m in minors:
            for s in soft:
                if _near(m, s, window=30):
                    return True
    return False


def scan_text(text: str | None) -> SafetyVerdict:
    """Синхронный детект. Никогда не бросает исключений.

    Каждый паттерн проверяется и по сырому тексту (для кириллицы), и по
    нормализованному (для латиницы-обфускации/гомоглифов), т.к. нормализация
    намеренно латинизирует замаскированные токены.

    Комбо-правила (минор + сексуальный контекст, глагол + оружие, событие +
    призыв) работают в пределах близкого окна или одного предложения: случайные
    слова из разных частей поста не должны складываться в «нарушение».
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

        # ── CSAM: аббревиатура (cp/цп/pedo) рядом со словом распространения ────
        for hay in hays:
            for sent in _sentences(hay):
                if not _combo(_CSAM_ABBR, _DISTRIB, sent, window=40):
                    continue
                if _ABBR_BENIGN.search(sent) and not re.search(r"\bpedo\b", sent):
                    continue
                return SafetyVerdict(True, CATEGORY_CSAM, "csam_abbr")

        # ── CSAM: комбо (минор + сексуальный контекст рядом) ──────────────────
        for hay in hays:
            if _csam_combo(hay):
                return SafetyVerdict(True, CATEGORY_CSAM, "csam_combo")

        # ── Терроризм: инструкция по изготовлению оружия ──────────────────────
        for hay in hays:
            for sent in _sentences(hay):
                if _combo(_WEAPON_MAKE, _WEAPON_OBJ, sent, window=80) or _verb_before(
                    _WEAPON_MAKE, _WEAPON_BOMB, sent, window=40
                ):
                    return SafetyVerdict(True, CATEGORY_TERROR, "terror_weapon")

        # ── Терроризм: призыв к массовому насилию / теракту ───────────────────
        for hay in hays:
            for sent in _sentences(hay):
                if _NEWS.search(sent):
                    continue
                if _combo(_TERROR_EVENT, _CALL, sent, window=60):
                    return SafetyVerdict(True, CATEGORY_TERROR, "terror_call")

        # ── Терроризм: террор-организация + намерение примкнуть/славить ────────
        for hay in hays:
            if _combo(_TERROR_ORG, _TERROR_INTENT_CALL, hay, window=100):
                return SafetyVerdict(True, CATEGORY_TERROR, "terror_org_intent")
            for sent in _sentences(hay):
                if _NEWS.search(sent):
                    continue
                if _combo(_TERROR_ORG, _TERROR_INTENT_IDEOLOGY, sent, window=100):
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
