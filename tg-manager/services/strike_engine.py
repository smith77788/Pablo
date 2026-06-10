"""Strike Engine v2 — эшелонированная атака с адаптивным таймингом и верификацией.

Принципиальные улучшения относительно v1:
  1. Pre-flight — отсев аккаунтов в кулдауне, сортировка по trust_score
  2. Staggered execution — аккаунты атакуют волнами со случайными задержками (не ботнет)
  3. Flood intelligence — уважение FloodWait, adaptive backoff, переиспользование сессий
  4. Verification loop — проверка факта удаления/блокировки цели после атаки
  5. Network parallelization — узлы сети атакуются параллельно, не последовательно
  6. Progressive intensity — наращивание давления волнами, умная эскалация
  7. Evidence collection — сбор и сохранение доказательств для escalation

Архитектура фаз:
  Фаза 0: Pre-flight — проверка аккаунтов, сортировка, план атаки
  Фаза 1: Recon — разведка цели одним лучшим аккаунтом
  Фаза 2: Strike Wave 1 — ReportPeer + ReportSpam (все аккаунты, staggered)
  Фаза 3: Strike Wave 2 — Join + Internal Reports (аккаунты которые смогли войти)
  Фаза 4: Strike Wave 3 — Admins + Linked Resources
  Фаза 5: Network — параллельная атака связанных узлов
  Фаза 6: Escalation — внешние abuse-формы + @SpamBot
  Фаза 7: Verification — проверка результата
"""

from __future__ import annotations

import asyncio
import html
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any

import aiohttp

from services.logger import log_exc_swallow

log = logging.getLogger(__name__)

# ── Константы ──────────────────────────────────────────────────────────────────
_CONCURRENCY = 3  # макс параллельных аккаунтов в тяжёлой фазе (GetHistory+MsgReport)
_WAVE_COOLDOWN = (
    120,
    300,
)  # секунд между волнами — Telegram должен успеть принять жалобы
_VERIFY_WAIT = (30, 120)  # секунд ожидания перед верификацией
_MAX_RETRIES = 2  # макс ретраев при FloodWait на аккаунт
_FLOOD_CAP = 65.0  # кап exponential backoff
# Минимальный интервал между аккаунтами внутри волны (в секундах).
# Telegram rate-limit работает per-peer: >2 GetHistory на один канал за 60с = FLOOD_WAIT.
# Увеличено до 60-120с для снижения риска coordinated attack detection
_STAGGER_BASE = (60, 120)


def _telegram_target_display(target: str) -> str:
    try:
        from services.account_manager import format_telegram_join_ref_display

        return format_telegram_join_ref_display(str(target))
    except Exception:
        raw = str(target).strip()
        if not raw:
            return ""
        if raw.startswith(("http://", "https://", "@")):
            return raw
        if raw.startswith("+"):
            return f"https://t.me/{raw}"
        return f"@{raw}"


# ── Уникальные тексты жалоб ───────────────────────────────────────────────────
# Каждый аккаунт получает СВОЙ уникальный текст, на случайном языке.
# Расширенный пул: 25+ текстов на категорию, 7+ языков.

TEXTS: dict[str, list[str]] = {
    "drugs": [
        "Канал занимается открытой торговлей наркотиками. Требую немедленного удаления.",
        "This channel openly sells illegal narcotics. Immediate removal required.",
        "Ce canal vend des drogues illégales ouvertement. Suppression immédiate.",
        "Dieser Kanal verkauft offen illegale Drogen. Sofortige Entfernung erforderlich.",
        "Este canal vende drogas ilegales abiertamente. Eliminación inmediata requerida.",
        "Канал — открытый наркорынок. Продаются запрещённые вещества без ограничений.",
        "Active drug trafficking operation distributing controlled substances to buyers.",
        "Canal de vente de drogues avec livraison — violation grave des CGU Telegram.",
        "Здесь продают: конопля, амфетамин, кокаин, соли, МДМА, героин.",
        "Drug delivery service openly advertising — report and remove immediately.",
        "Незаконная торговля психотропными и наркотическими веществами через Telegram.",
        "Активная наркоторговля с криптооплатой и анонимной доставкой по стране.",
        "Illegal narcotics marketplace — fentanyl, meth, cocaine offered for sale openly.",
        "Наркомагазин с ценами, контактами и системой доставки прямо в описании.",
        "Open drug distribution network exploiting Telegram as its marketplace.",
        "Продажа запрещённых наркотиков с доставкой — детали в описании канала.",
        "Darknet drug vendor operating in the open on Telegram — violates all ToS.",
        "Наркотики продаются публично, система заказов через бота в описании.",
        "Канал систематически рекламирует запрещённые вещества с ценами и фото.",
        "Illegal drug sales with cryptocurrency payment — request immediate removal.",
        "Продают мефедрон, соли, зелёнку и другие запрещённые вещества.",
        "Active narcotics sales — minors may be among buyers. Act now.",
        "Систематическая наркоторговля, нарушение законодательства РФ и международного права.",
        "Drug marketplace with catalogues, prices, delivery options in plain sight.",
        "Нелегальный сбыт наркотиков через открытый Telegram-канал. Прошу заблокировать.",
        "Questo canale vende droga illegalmente. Rimozione immediata richiesta.",
        "このチャンネルは違法薬物を公然と販売しています。即時削除が必要です。",
        "هذه القناة تبيع المخدرات بشكل علني. الإزالة الفورية مطلوبة.",
    ],
    "terrorism": [
        "Канал распространяет террористическую пропаганду и призывы к массовому насилию.",
        "This channel promotes terrorist ideology and incites mass violence.",
        "Ce canal diffuse de la propagande terroriste et appelle à la violence.",
        "Dieser Kanal verbreitet Terrorpropaganda und Gewaltaufrufe gegen Zivilisten.",
        "Este canal promueve el terrorismo y la ideología extremista violenta.",
        "Пропаганда ИГИЛ/Аль-Каиды и других запрещённых террористических организаций.",
        "Terrorist recruitment and radicalization content actively distributed here.",
        "Canal de radicalisation jihadiste — appelle au meurtre et à la terreur.",
        "Активная вербовка в террористические организации через этот канал.",
        "Content glorifying and planning terrorist attacks against civilian targets.",
        "Экстремистская идеология с прямыми призывами к вооружённому джихаду.",
        "Terrorist propaganda with bomb-making instructions and attack coordination.",
        "Материалы запрещённых террористических организаций распространяются открыто.",
        "Incitement to political violence and terrorism against civilian population.",
        "Радикальный контент с призывами к убийствам и массовым атакам.",
        "Extremist channel calling for attacks on government and civilians.",
        "Координация террористической деятельности с использованием этого канала.",
        "ISIS/Al-Qaeda propaganda and active recruitment material distributed here.",
        "Вербовка и финансирование вооружённых экстремистских группировок.",
        "Violent extremism — justification and glorification of terrorist acts.",
        "Призывы к джихаду, убийствам и захвату власти силой.",
        "Terrorist ideology platform actively recruiting young vulnerable users.",
        "Экстремистские материалы, запрещённые во всех юрисдикциях.",
        "Systematic distribution of terrorist content and operational attack planning.",
        "Канал пропагандирует экстремизм, угрожает мирному населению напрямую.",
        "Questo canale diffonde propaganda terroristica. Rimozione immediata.",
        "このチャンネルはテロリズムを促進しています。即時削除が必要です。",
    ],
    "childabuse": [
        "Канал содержит незаконные материалы с участием несовершеннолетних.",
        "ILLEGAL content involving children. Immediate removal and law enforcement required.",
        "Ce canal contient du matériel illégal impliquant des mineurs — urgence absolue.",
        "Dieser Kanal enthält illegales Material mit Minderjährigen — sofort sperren.",
        "Este canal contiene material ilegal con menores — eliminación urgente.",
        "Child sexual abuse material (CSAM) detected. Platform must report to NCMEC.",
        "Материалы сексуальной эксплуатации детей — нарушение законов всех стран.",
        "CSAM content on this channel. Immediate removal and authorities notification.",
        "Illegal exploitation of minors — criminal violation in every jurisdiction.",
        "Незаконный контент с участием детей. Срочное удаление и уведомление органов.",
        "Content depicting abuse of children. Must be removed within minutes.",
        "Детский контент незаконного характера — уголовное преступление по всем законам.",
        "CSAM distribution network using this Telegram channel.",
        "Сексуальная эксплуатация несовершеннолетних — немедленное удаление обязательно.",
        "Platform must immediately remove CSAM and notify law enforcement authorities.",
        "Contenuto illegale con minori — rimozione immediata e azione penale.",
        "このチャンネルは児童虐待コンテンツを含んでいます。緊急削除が必要です。",
    ],
    "fraud": [
        "Мошеннический канал — систематический обман пользователей.",
        "This channel runs an active financial fraud and scam operation.",
        "Ce canal organise une fraude financière massive contre les utilisateurs.",
        "Dieser Kanal betreibt aktiven Finanzbetrug gegen ahnungslose Nutzer.",
        "Este canal opera una estafa financiera activa contra los usuarios.",
        "Systematic financial fraud stealing money from victims under false pretenses.",
        "Организованное мошенничество: крипто-скам, пирамиды, фишинг, кража денег.",
        "Fake investment platform defrauding users of cryptocurrency assets.",
        "Канал обманывает людей под видом инвестиций — жертвы по всей стране.",
        "Active Ponzi scheme recruiting victims through this Telegram channel.",
        "Мошенники собирают деньги и исчезают — сотни подтверждённых жертв.",
        "Coordinated fraud ring using Telegram as its primary recruitment platform.",
        "Фишинг и кража личных данных для хищения денежных средств граждан.",
        "Investment scam with fake returns, fabricated testimonials, real theft.",
        "Систематический обман — ложные обещания, реальные потери пострадавших.",
        "Financial scam with verified victims across multiple countries.",
        "Финансовая пирамида с признаками организованной преступной группы.",
        "Crypto fraud — fake exchange, fake investment returns, real theft.",
        "Мошенничество подтверждено множеством пострадавших пользователей.",
        "This channel systematically defrauds vulnerable users of their savings.",
        "Организованная мошенническая группа с историей многочисленных преступлений.",
        "Ponzi scheme targeting people seeking legitimate investment opportunities.",
        "Мошенники используют поддельные документы и фото для достоверности.",
        "Mass fraud operation — fake documents, fake reviews, real financial damage.",
        "Канал является инструментом организованной преступной мошеннической группы.",
        "Truffa finanziaria organizzata — vittime in tutta la regione.",
        "このチャンネルは組織的な金融詐欺を行っています。即時削除が必要です。",
    ],
    "weapons": [
        "Нелегальная торговля оружием через открытый Telegram-канал.",
        "Illegal firearms and weapons sales operating openly through this channel.",
        "Ce canal vend des armes illégales — violation grave des lois nationales.",
        "Dieser Kanal verkauft illegale Waffen ohne jegliche Überprüfung.",
        "Este canal vende armas ilegales — violación grave de la legislación.",
        "Underground weapons marketplace with no background checks whatsoever.",
        "Торговля незарегистрированным огнестрельным оружием и боеприпасами.",
        "Illegal arms trafficking network operating openly on Telegram platform.",
        "Продажа пистолетов, автоматов, взрывчатки — без документов, открыто.",
        "Firearms trafficking endangering public safety across entire regions.",
        "Нелегальный оружейный рынок — нарушение законодательства всех юрисдикций.",
        "Weapons and explosives for sale — immediate law enforcement action required.",
        "Торговля холодным и огнестрельным оружием абсолютно в открытом доступе.",
        "Illegal arms dealing with delivery service — active criminal network.",
        "Продажа самодельного и переделанного огнестрельного оружия без следов.",
        "Arms trafficking through encrypted Telegram channels with crypto payment.",
        "Нелегальный оборот оружия с доставкой по всей стране.",
        "Weapons marketplace — handguns, rifles, explosives offered for sale.",
        "Незаконная торговля оружием, создающая прямую угрозу общественной безопасности.",
        "Criminal arms network actively selling to completely unvetted buyers.",
        "Vendita illegale di armi da fuoco — violazione grave della legge.",
        "このチャンネルは違法な武器を販売しています。即時削除が必要です。",
    ],
    "darknet": [
        "Даркнет-сервисы и незаконные товары продаются через этот открытый канал.",
        "This channel coordinates illegal darknet marketplace activities openly.",
        "Ce canal coordonne des activités illégales de marché darknet.",
        "Dieser Kanal koordiniert illegale Darknet-Aktivitäten offen auf Telegram.",
        "Este canal coordina actividades ilegales de mercado darknet.",
        "Criminal services network using Telegram for coordination and sales.",
        "Незаконные услуги: взлом аккаунтов, слив данных, поддельные документы.",
        "Hacking-as-a-service, stolen credentials and identity theft marketplace.",
        "Поддельные паспорта, водительские права, документы любых стран на заказ.",
        "Darknet criminal services — money laundering and document forgery.",
        "Кибератаки на заказ, аренда ботнетов, DDoS-стрессеры.",
        "Money laundering and cryptocurrency mixing services advertised openly.",
        "Stolen personal data and financial credentials sold without restriction.",
        "Продажа ворованных баз данных и персональных данных граждан.",
        "Cybercrime-as-a-service — full criminal marketplace openly on Telegram.",
        "Незаконные услуги с оплатой в криптовалюте, полная анонимность клиентов.",
        "Document forgery, hacking tools, and money laundering in one place.",
        "Организованная киберпреступность использует этот канал как открытую витрину.",
        "Criminal marketplace offering hacking, fraud, and identity theft services.",
        "Даркнет в открытом Telegram — нарушение всех правил платформы.",
        "Servizi criminali darknet venduti apertamente su Telegram.",
    ],
    "spam": [
        "Канал занимается массовым спамом и нежелательной рекламой.",
        "This channel conducts mass spam campaigns violating all platform rules.",
        "Ce canal envoie du spam massif non sollicité.",
        "Dieser Kanal betreibt massives Spamming gegen Nutzer.",
        "Este canal realiza spam masivo violando las reglas de la plataforma.",
        "Systematic spam operation abusing Telegram infrastructure.",
        "Массовый спам с автоматическими инструментами и ботами.",
        "Unsolicited bulk messaging causing platform degradation.",
        "Канал используется исключительно для массовых нежелательных рассылок.",
        "Automated spam content distribution violating platform terms of service.",
        "Questo canale invia spam massivo violando i termini di servizio.",
    ],
    "other": [
        "Канал систематически нарушает правила использования Telegram.",
        "This channel repeatedly violates Telegram's Terms of Service.",
        "Ce canal viole systématiquement les CGU de Telegram.",
        "Dieser Kanal verstößt wiederholt gegen Telegramms Nutzungsbedingungen.",
        "Este canal viola sistemáticamente los términos de servicio de Telegram.",
        "Harmful and illegal content violating community guidelines and platform rules.",
        "Канал содержит контент, строго запрещённый правилами платформы.",
        "Content repeatedly violating community guidelines — action required.",
        "Нарушение правил платформы — требую немедленного рассмотрения.",
        "Channel operating in violation of Telegram's policies — remove immediately.",
        "Questo canale viola ripetutamente i termini di servizio di Telegram.",
        "このチャンネルはTelegramの利用規約に違反しています。",
        "هذه القناة تنتهك شروط خدمة تيليجرام بشكل متكرر.",
    ],
    "escort": [
        "Канал открыто рекламирует проституцию и незаконные эскорт-услуги.",
        "This channel openly advertises prostitution and illegal escort services.",
        "Ce canal fait la promotion ouverte de la prostitution et de services d'escorte illégaux.",
        "Dieser Kanal bewirbt offen Prostitution und illegale Escort-Dienstleistungen.",
        "Este canal promueve activamente la prostitución y servicios de acompañamiento ilegales.",
        "Illegal commercial sex services openly advertised — violates laws and Telegram ToS.",
        "Канал систематически публикует рекламу эскорт-услуг с ценами и контактами.",
        "Open advertisement of paid sexual services — prostitution and human trafficking.",
        "Незаконная торговля сексуальными услугами — прейскурант, фото, контакты в открытом доступе.",
        "Channel systematically advertising commercial sexual exploitation of individuals.",
        "Prostitution marketplace actively recruiting clients — violates local and international law.",
        "Канал является площадкой для организации проституции и сексуальной эксплуатации.",
        "Illegal escort and prostitution advertising with explicit pricing and contact info.",
        "Commercial sex services promoted openly — likely involves trafficking and exploitation.",
        "Канал рекламирует сексуальные услуги за деньги в нарушение законодательства.",
        "Sex trafficking and prostitution openly advertised — immediate removal required.",
        "Этот канал используется для организации платных сексуальных услуг.",
        "Platform for sexual exploitation — prostitution and escort services illegally advertised.",
        "Questo canale promuove prostituzione e servizi di escort illegali apertamente.",
        "Este canal es un mercado ilegal de servicios sexuales comerciales.",
        "Canale di promozione illegale di prostituzione — rimozione immediata necessaria.",
        "Kanal mit illegaler Prostitutionswerbung — sofortige Löschung erforderlich.",
        "Канал пропагандирует и организует незаконные сексуальные услуги за вознаграждение.",
        "Active sex services marketplace — content violates Telegram's policies entirely.",
        "Открытая реклама проституции: анкеты, фото, расценки — нарушение закона и ToS.",
    ],
}

# Маппинг пресетов на базовые причины
PRESET_TO_REASON = {
    "drugs": "other",
    "terrorism": "violence",
    "fraud": "spam",
    "csam": "childabuse",
    "weapons": "violence",
    "darknet": "other",
    "escort": "pornography",
}
PRESET_TEXTS = {
    "drugs": TEXTS["drugs"],
    "terrorism": TEXTS["terrorism"],
    "csam": TEXTS["childabuse"],
    "fraud": TEXTS["fraud"],
    "weapons": TEXTS["weapons"],
    "darknet": TEXTS["darknet"],
    "escort": TEXTS["escort"],
}

# Приоритетные причины для эскалации (наиболее эффективные)
# CSAM/drugs — мгновенная реакция, violence — быстрая, spam — медленная
_REASON_PRIORITY = {
    "childabuse": 0,  # highest priority
    "csam": 0,
    "terrorism": 1,
    "drugs": 2,
    "weapons": 3,
    "fraud": 4,
    "pornography": 5,
    "escort": 5,
    "spam": 6,
    "other": 7,
}


def assign_texts(reason_or_preset: str, count: int) -> list[str]:
    """Выдаёт `count` уникальных текстов — каждому аккаунту свой."""
    pool = (
        PRESET_TEXTS.get(reason_or_preset)
        or TEXTS.get(reason_or_preset)
        or TEXTS["other"]
    )
    if count <= len(pool):
        return random.sample(pool, count)
    result: list[str] = []
    while len(result) < count:
        needed = count - len(result)
        result.extend(random.sample(pool, min(len(pool), needed)))
    return result


# ── Data structures ────────────────────────────────────────────────────────────


@dataclass
class StrikePlan:
    """План атаки: какие аккаунты, на какие цели, в каком порядке."""

    targets: list[str]  # @usernames целей
    accounts: list[dict]  # отсортированные аккаунты
    reason: str  # базовая причина
    preset: str | None  # пресет (drugs, fraud, etc.)
    label: str  # человекочитаемая метка
    intel: dict[str, dict] = field(default_factory=dict)  # peer → intel
    waves: list[list[dict]] = field(default_factory=list)  # аккаунты по волнам
    started_at: float = 0.0
    phase: str = "init"
    mode: str = "normal"  # fast | normal | maximum
    owner_id: int = 0  # для email-эскалации (загрузка ящиков из DB)


@dataclass
class StrikeResult:
    """Результат strike-операции."""

    target: str
    total_reports: int = 0
    unique_accounts: int = 0
    peer_reported: int = 0
    multi_reason: int = 0
    msgs_reported: int = 0
    msgs_fetched: int = 0
    pinned_reported: int = 0
    photo_reported: bool = False
    admins_reported: int = 0
    spam_signaled: int = 0
    reactions: int = 0
    linked_group_reported: bool = False
    bots_reported: int = 0
    forwarded: int = 0
    blocked: int = 0
    network_nodes: int = 0
    network_reports: int = 0
    abuse_form_ok: bool = False
    verified_down: bool | None = None  # None = не проверено
    spambot_escalation: str = "skipped"  # sent | skipped | error
    emails_sent: int = 0  # кол-во успешно отправленных email
    email_escalation: dict = field(default_factory=dict)  # детали email-фазы
    errors: list[str] = field(default_factory=list)
    duration_s: float = 0.0
    phase_results: dict[str, Any] = field(default_factory=dict)


# ── Pre-flight ─────────────────────────────────────────────────────────────────


def preflight_accounts(accounts: list[dict], min_trust: float = 0.0) -> list[dict]:
    """Предполётная проверка аккаунтов.

    Фильтры:
      - is_active = False → отсев
      - cooldown_until (DB) > now → отсев
      - flood_engine in-memory cooldown → отсев
      - trust_score < min_trust → отсев

    Сортировка: composite score = trust_score + memory_score − risk_score
    (лучшие аккаунты по всем трём метрикам — в первую волну).
    """
    now = time.time()
    viable: list[dict] = []

    try:
        from services import flood_engine as _fe

        fe_available = True
    except Exception:
        fe_available = False

    try:
        from services.infra_memory import get_account_score as _mem_score

        mem_available = True
    except Exception:
        mem_available = False

    try:
        from services import op_worker as _opw

        opw_available = True
    except Exception:
        opw_available = False

    for acc in accounts:
        if not acc.get("is_active", False):
            continue
        # Забаненные/спамблок/деактивированные — никогда не в страйк
        if (acc.get("acc_status") or "active") in (
            "banned",
            "spamblock",
            "deactivated",
        ):
            continue
        # Аккаунт занят другой операцией/разогревом → не трогаем (одна сессия = один клиент)
        if opw_available and acc.get("id") and _opw.is_account_in_use(acc["id"]):
            continue
        # Проверка кулдауна из БД
        cooldown = acc.get("cooldown_until")
        if cooldown:
            try:
                cd_ts = (
                    cooldown.timestamp()
                    if hasattr(cooldown, "timestamp")
                    else float(cooldown)
                )
                if cd_ts > now:
                    continue
            except (TypeError, ValueError, OSError):
                log_exc_swallow(
                    log, "Не удалось распарсить cooldown_until аккаунта в preflight"
                )
        # Проверка in-memory cooldown в flood_engine
        if fe_available and acc.get("id") and _fe.is_account_cooling(acc["id"]):
            continue
        # Проверка trust_score
        ts = acc.get("trust_score") or 0
        if ts < min_trust:
            continue
        viable.append(acc)

    # Composite sort: trust + memory_score − risk (все в диапазоне [0,1])
    def _sort_key(a: dict) -> float:
        trust = float(a.get("trust_score") or 0)  # trust_score уже в диапазоне 0-1
        risk = (
            _fe.get_account_state(a["id"]).risk_score
            if fe_available and a.get("id")
            else 0.0
        )
        mem = _mem_score(a["id"], "strike") if mem_available and a.get("id") else 0.5
        return -(trust + mem - risk)  # minus = DESC order

    viable.sort(key=_sort_key)
    return viable


def plan_waves(accounts: list[dict], num_waves: int = 3) -> list[list[dict]]:
    """
    Распределяет аккаунты по волнам для staggered execution.
    Лучшие аккаунты идут в первую волну (основной удар),
    остальные распределяются по последующим волнам.

    Каждая последующая волна меньше предыдущей (убывающая интенсивность).
    """
    if not accounts:
        return []
    if len(accounts) <= 2:
        return [list(accounts)]

    waves: list[list[dict]] = []
    remaining = list(accounts)

    # Волна 1: 50% лучших аккаунтов (основной удар)
    w1_size = max(2, len(remaining) // 2)
    waves.append(remaining[:w1_size])
    remaining = remaining[w1_size:]

    # Волна 2: 30% (поддержка)
    if remaining and num_waves >= 2:
        w2_size = max(1, len(remaining) * 3 // 5)
        waves.append(remaining[:w2_size])
        remaining = remaining[w2_size:]

    # Волна 3: оставшиеся 20% (завершение)
    if remaining and num_waves >= 3:
        waves.append(remaining)

    return waves


# ── Core: parallel_strike v2 (staggered) ────────────────────────────────────────


async def _one_account_strike(
    acc: dict,
    peer_username: str,
    intel: dict,
    reason: str,
    preset: str | None,
    texts: list[str],
    acc_index: int,
    wave_num: int,
    sem: asyncio.Semaphore,
    mode: str = "normal",
    pool=None,
) -> dict:
    """
    Один аккаунт выполняет полную атаку.
    Режим задаёт набор векторов: fast (6), normal (12), maximum (12+).
    С задержкой на основе позиции в волне (staggered start).
    С ретраями при FloodWait.
    """
    from services import account_manager

    # Staggered start: каждый аккаунт ждёт acc_index * 25-45с.
    # acc_0 тоже получает базовую задержку (не 1.5-4с): при _CONCURRENCY>1
    # несколько acc_index==0 из разных волн иначе стартуют почти одновременно
    # и делают GetHistory+join+report по одному каналу в одном окне → FLOOD_WAIT.
    if acc_index > 0:
        stagger_delay = random.uniform(*_STAGGER_BASE) * acc_index
    else:
        stagger_delay = random.uniform(*_STAGGER_BASE)
    await asyncio.sleep(stagger_delay)

    text = texts[acc_index % len(texts)]
    msg_texts = assign_texts(preset or reason, 15)

    if mode == "fast":
        # fast: нет join, нет доп. векторов — только быстрые жалобы
        kwargs: dict = dict(
            join_first=False,
            negative_react=False,
            report_admins=False,
            report_linked_group=False,
            report_linked_bots=False,
            forward_to_bot=False,
            block_after=False,
            multi_reason=True,
            max_msg_reports=30,
            report_photo=True,
            report_pinned=True,
        )
    elif mode == "maximum":
        # maximum: join во всех волнах — нужен для msg reporting
        kwargs = dict(
            join_first=True,
            negative_react=True,
            report_admins=True,
            report_linked_group=(wave_num == 0),
            report_linked_bots=True,
            forward_to_bot=(wave_num <= 1),
            block_after=(wave_num >= 2),
            multi_reason=True,
            max_msg_reports=100,
            report_photo=True,
            report_pinned=True,
        )
    else:  # normal
        # normal: join во всех волнах — без join MsgReportRequest не работает
        kwargs = dict(
            join_first=True,
            negative_react=(wave_num <= 1),
            report_admins=(wave_num <= 1),
            report_linked_group=(wave_num == 0),
            report_linked_bots=True,
            forward_to_bot=(wave_num <= 1),
            block_after=(wave_num >= 2),
            multi_reason=True,
            max_msg_reports=60,
            report_photo=True,
            report_pinned=True,
        )

    async with sem:
        t0_strike = time.monotonic()
        for attempt in range(_MAX_RETRIES + 1):
            try:
                result = await account_manager.report_peer_deep_v2(
                    acc["session_str"],
                    peer_username,
                    reason,
                    message=text,
                    msg_messages=msg_texts,
                    wave_num=wave_num,
                    _acc=acc,
                    **kwargs,
                )
                # Добавляем метаданные аккаунта
                result["_acc_id"] = acc.get("id")
                result["_acc_trust"] = acc.get("trust_score", 0)
                result["_wave"] = wave_num

                # ── Account hit PEER_FLOOD / ban MID-RUN (surfaced in result flags).
                # report_peer_deep_v2 stops early and flags it instead of raising.
                # Treat as a restriction, NOT success: cool/deactivate, don't reuse. ──
                if result.get("_fatal"):
                    log.warning(
                        "strike acc %s wave %d: fatal signal in result — marking inactive",
                        acc.get("id"),
                        wave_num,
                    )
                    if pool is not None and acc.get("id"):
                        try:
                            await pool.execute(
                                "UPDATE tg_accounts SET is_active=FALSE WHERE id=$1",
                                acc["id"],
                            )
                        except Exception:
                            log_exc_swallow(
                                log, f"strike: mark_inactive failed acc={acc.get('id')}"
                            )
                    try:
                        from services.infra_memory import record_account_op

                        record_account_op(
                            acc["id"], "strike", success=False, error="fatal"
                        )
                    except Exception:
                        log_exc_swallow(log, "strike: record fatal failed")
                    return result
                if result.get("_peer_flood"):
                    log.warning(
                        "strike acc %s wave %d: PEER_FLOOD in result — 1h cooldown",
                        acc.get("id"),
                        wave_num,
                    )
                    if pool is not None and acc.get("id"):
                        try:
                            from services.flood_engine import record_flood

                            await record_flood(
                                pool, acc["id"], 3600, "strike_peer_flood"
                            )
                        except Exception:
                            log_exc_swallow(
                                log,
                                f"strike: record_peer_flood failed acc={acc.get('id')}",
                            )
                    try:
                        from services.infra_memory import record_account_op

                        record_account_op(
                            acc["id"], "strike", success=False, error="peer_flood"
                        )
                    except Exception:
                        log_exc_swallow(log, "strike: record peer_flood failed")
                    return result

                # Фиксируем успех в Infrastructure Memory
                try:
                    from services.infra_memory import record_account_op

                    record_account_op(
                        acc["id"],
                        "strike",
                        success=True,
                        duration_s=time.monotonic() - t0_strike,
                    )
                except Exception as e:
                    log.warning(
                        "strike: record_account_op success failed acc=%s: %s",
                        acc.get("id"),
                        e,
                    )
                return result
            except Exception as e:
                err_str = str(e)[:150]
                # Если FloodWait — записываем РЕАЛЬНОЕ значение в flood_engine,
                # затем коротко ждём+ретрай, либо (длинный flood) останавливаемся.
                if "FLOOD_WAIT" in err_str.upper() or "flood" in err_str.lower():
                    import re as _re

                    wait_match = _re.search(r"(\d+)", err_str)
                    real_wait = float(wait_match.group(1)) if wait_match else 30.0
                    log.warning(
                        "strike acc %s wave %d: FloodWait %ss (real), retry %d/%d",
                        acc.get("id"),
                        wave_num,
                        real_wait,
                        attempt + 1,
                        _MAX_RETRIES,
                    )
                    # Cool by the REAL duration. Recording a capped 65s let the account
                    # resume while Telegram still enforced a longer wait → ban escalation.
                    if pool is not None:
                        try:
                            from services.flood_engine import record_flood

                            await record_flood(
                                pool, acc["id"], int(real_wait), "strike"
                            )
                        except Exception:
                            log_exc_swallow(
                                log, f"strike: record_flood failed acc={acc.get('id')}"
                            )
                    else:
                        try:
                            from services.flood_engine import get_account_state

                            state = get_account_state(acc["id"])
                            import time as _time

                            state.consecutive_floods += 1
                            state.last_flood_at = _time.monotonic()
                            state.cooldown_until = _time.monotonic() + real_wait + 10
                            state.risk_score = min(1.0, state.risk_score + 0.15)
                        except Exception:
                            log.warning(
                                "strike: in-memory flood state update failed acc=%s wait=%s",
                                acc.get("id"),
                                real_wait,
                            )
                    # Long flood → stop this account now (already cooled), don't busy-retry.
                    if real_wait > _FLOOD_CAP:
                        log.warning(
                            "strike acc %s: long FloodWait %ss — stopping account (cooled)",
                            acc.get("id"),
                            real_wait,
                        )
                        return {
                            "peer_reported": False,
                            "error": err_str,
                            "_acc_id": acc.get("id"),
                            "_wave": wave_num,
                            "rate_limited": True,
                        }
                    await asyncio.sleep(real_wait + random.uniform(2, 8))
                    continue
                # Fatal account errors — mark inactive in DB, stop immediately
                _FATAL = (
                    "USER_DEACTIVATED_BAN",
                    "USER_DEACTIVATED",
                    "AUTH_KEY_UNREGISTERED",
                    "SESSION_REVOKED",
                )
                if any(p in err_str.upper() for p in _FATAL):
                    log.warning(
                        "strike acc %s wave %d: fatal account error, marking inactive: %s",
                        acc.get("id"),
                        wave_num,
                        err_str,
                    )
                    if pool is not None and acc.get("id"):
                        try:
                            await pool.execute(
                                "UPDATE tg_accounts SET is_active=FALSE WHERE id=$1",
                                acc["id"],
                            )
                        except Exception:
                            log_exc_swallow(
                                log, f"strike: mark_inactive failed acc={acc.get('id')}"
                            )
                    return {
                        "peer_reported": False,
                        "error": err_str,
                        "_acc_id": acc.get("id"),
                        "_wave": wave_num,
                        "_fatal": True,
                    }
                # PeerFlood — severe account-level rate limit, apply 1h cooldown, stop
                if "PEER_FLOOD" in err_str.upper():
                    log.warning(
                        "strike acc %s wave %d: PeerFlood, applying 1h cooldown",
                        acc.get("id"),
                        wave_num,
                    )
                    if pool is not None and acc.get("id"):
                        try:
                            from services.flood_engine import record_flood

                            await record_flood(
                                pool, acc["id"], 3600, "strike_peer_flood"
                            )
                        except Exception:
                            log_exc_swallow(
                                log,
                                f"strike: record_peer_flood failed acc={acc.get('id')}",
                            )
                    return {
                        "peer_reported": False,
                        "error": err_str,
                        "_acc_id": acc.get("id"),
                        "_wave": wave_num,
                        "_peer_flood": True,
                    }
                log.warning(
                    "strike acc %s wave %d: %s", acc.get("id"), wave_num, err_str
                )
                # Фиксируем ошибку в Infrastructure Memory
                try:
                    from services.infra_memory import record_account_op

                    record_account_op(acc["id"], "strike", success=False, error=err_str)
                except Exception as e:
                    log.warning(
                        "strike: record_account_op failed acc=%s: %s", acc.get("id"), e
                    )
                return {
                    "peer_reported": False,
                    "error": err_str,
                    "_acc_id": acc.get("id"),
                    "_wave": wave_num,
                }

        return {
            "peer_reported": False,
            "error": f"max retries ({_MAX_RETRIES}) exceeded",
            "_acc_id": acc.get("id"),
            "_wave": wave_num,
        }


def _safe_gather_results(raw: list) -> list[dict]:
    """Фильтрует результаты gather(return_exceptions=True): re-raise CancelledError, остальные → error dict."""
    out = []
    for r in raw:
        if isinstance(r, asyncio.CancelledError):
            raise r
        if isinstance(r, BaseException):
            log.warning("strike wave task failed: %s", r)
            out.append({"peer_reported": False, "error": str(r)[:80]})
        else:
            out.append(r)
    return out


def _reason_to_email_cat(reason: str, preset: str | None) -> dict:
    """Возвращает cat-dict для email по reason/preset из основного Strike."""
    key = (preset or reason or "").lower()
    _map: dict[str, dict] = {
        "csam": {
            "tg_reason": "childabuse",
            "severity": "CRITICAL",
            "ncmec": True,
            "email_subject": "URGENT: CSAM on Telegram",
            "label": "🚨 CSAM (детский контент)",
            "report_msg_key": "csam",
        },
        "childabuse": {
            "tg_reason": "childabuse",
            "severity": "CRITICAL",
            "ncmec": True,
            "email_subject": "URGENT: CSAM on Telegram",
            "label": "🚨 CSAM (детский контент)",
            "report_msg_key": "csam",
        },
        "drugs": {
            "tg_reason": "drugs",
            "severity": "HIGH",
            "ncmec": False,
            "email_subject": "Report: Illegal Drug Sales on Telegram",
            "label": "💊 Наркотики",
            "report_msg_key": "drugs",
        },
        "terrorism": {
            "tg_reason": "violence",
            "severity": "CRITICAL",
            "ncmec": False,
            "email_subject": "Report: Terrorism/Extremism on Telegram",
            "label": "💣 Терроризм/экстремизм",
            "report_msg_key": "terrorism",
        },
        "violence": {
            "tg_reason": "violence",
            "severity": "HIGH",
            "ncmec": False,
            "email_subject": "Report: Violent Content on Telegram",
            "label": "⚠️ Насилие",
            "report_msg_key": "terrorism",
        },
        "weapons": {
            "tg_reason": "violence",
            "severity": "HIGH",
            "ncmec": False,
            "email_subject": "Report: Illegal Weapons on Telegram",
            "label": "🔫 Оружие",
            "report_msg_key": "weapons",
        },
        "fraud": {
            "tg_reason": "spam",
            "severity": "MEDIUM",
            "ncmec": False,
            "email_subject": "Report: Fraud/Scam on Telegram",
            "label": "💸 Мошенничество",
            "report_msg_key": "fraud",
        },
        "spam": {
            "tg_reason": "spam",
            "severity": "MEDIUM",
            "ncmec": False,
            "email_subject": "Report: Spam/Scam on Telegram",
            "label": "📢 Спам",
            "report_msg_key": "fraud",
        },
        "prostitution": {
            "tg_reason": "pornography",
            "severity": "HIGH",
            "ncmec": False,
            "email_subject": "Report: Sex Trafficking on Telegram",
            "label": "🔞 Проституция/секс-трафик",
            "report_msg_key": "escort",
        },
        "escort": {
            "tg_reason": "pornography",
            "severity": "HIGH",
            "ncmec": False,
            "email_subject": "Report: Sex Trafficking on Telegram",
            "label": "🔞 Проституция/секс-трафик",
            "report_msg_key": "escort",
        },
        "pornography": {
            "tg_reason": "pornography",
            "severity": "HIGH",
            "ncmec": False,
            "email_subject": "Report: Illegal Pornography on Telegram",
            "label": "🔞 Незаконная порнография",
            "report_msg_key": "escort",
        },
        "darknet": {
            "tg_reason": "other",
            "severity": "HIGH",
            "ncmec": False,
            "email_subject": "Report: Darknet Services on Telegram",
            "label": "🕸 Даркнет-услуги",
            "report_msg_key": "darknet",
        },
    }
    return _map.get(
        key,
        {
            "tg_reason": reason or "other",
            "severity": "MEDIUM",
            "ncmec": False,
            "email_subject": f"Report: ToS Violation on Telegram ({key})",
            "label": f"⚠️ {key}",
            "report_msg_key": "other",
        },
    )


async def _run_email_escalation(
    pool,
    owner_id: int,
    target: str,
    reason: str,
    preset: str | None,
    progress_cb=None,
) -> dict:
    """
    Email-эскалация для основного Strike.
    Отправляет письма с каждого настроенного SMTP-ящика на:
      abuse@telegram.org, dmca@telegram.org, dpo@telegram.org, security@telegram.org
    + NCMEC (только для CSAM).
    Возвращает dict с total_sent, emails list, ncmec bool.
    """
    from datetime import datetime, timezone

    if not pool or not owner_id:
        return {
            "total_sent": 0,
            "emails": [],
            "ncmec": False,
            "skip_reason": "no pool/owner",
        }

    cat = _reason_to_email_cat(reason, preset)
    target_clean = target.lstrip("@")
    report_time = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    result: dict = {"total_sent": 0, "emails": [], "ncmec": False}

    # Загружаем email-аккаунты из БД
    db_emails: list[dict] = []
    try:
        rows = await pool.fetch(
            """SELECT id, email, smtp_host, smtp_port, smtp_pass,
                      COALESCE(auth_type, 'password') AS auth_type,
                      oauth_provider, oauth_refresh_token, oauth_access_token,
                      oauth_expires_at
               FROM strike_email_accounts
               WHERE owner_id=$1 AND is_active=TRUE
               ORDER BY last_used_at ASC NULLS FIRST""",
            owner_id,
        )
        db_emails = [dict(r) for r in rows]
    except Exception as e:
        log.debug("staggered_strike: email accounts fetch skipped: %s", e)
        return {
            "total_sent": 0,
            "emails": [],
            "ncmec": False,
            "skip_reason": str(e)[:80],
        }

    if not db_emails:
        return {
            "total_sent": 0,
            "emails": [],
            "ncmec": False,
            "skip_reason": "no email accounts configured",
        }

    # Resolve helpers defined later in this file via module globals (safe at call-time)
    import sys as _sys

    _mod = _sys.modules[__name__]
    _STRIKE_EMAIL_TARGETS = getattr(_mod, "_STRIKE_EMAIL_TARGETS", [])
    _build_abuse_tg_email = getattr(_mod, "_build_abuse_tg_email", None)
    _build_dmca_gdpr_email = getattr(_mod, "_build_dmca_gdpr_email", None)
    _build_ncmec_email = getattr(_mod, "_build_ncmec_email", None)
    _send_email = getattr(_mod, "_send_email", None)

    if not _STRIKE_EMAIL_TARGETS or not _send_email:
        return {
            "total_sent": 0,
            "emails": [],
            "ncmec": False,
            "skip_reason": "email helpers not loaded",
        }

    for ea in db_emails:
        any_ok = False
        for tgt in _STRIKE_EMAIL_TARGETS:
            if tgt["email_type"] == "abuse":
                body = _build_abuse_tg_email(target_clean, cat, report_time)
            else:
                body = _build_dmca_gdpr_email(
                    target_clean, cat, report_time, tgt["email_type"]
                )

            subject = (
                f"{cat['email_subject']} — @{target_clean} [{tgt['label'].upper()}]"
            )
            smtp_secret, auth_type = await _email_secret_for_account(pool, ea)
            ok, err = await _send_email(
                ea["smtp_host"],
                ea["smtp_port"],
                ea["email"],
                smtp_secret,
                ea["email"],
                tgt["addr"],
                subject,
                body,
                auth_type=auth_type,
            )
            result["emails"].append(
                {
                    "from": ea["email"],
                    "to": tgt["addr"],
                    "label": tgt["label"],
                    "ok": ok,
                    "err": err,
                }
            )
            if ok:
                result["total_sent"] += 1
                any_ok = True
                log.info(
                    "staggered_strike: email sent %s → %s", ea["email"], tgt["addr"]
                )
            else:
                log.warning(
                    "staggered_strike: email failed %s → %s: %s",
                    ea["email"],
                    tgt["addr"],
                    err,
                )

        if any_ok:
            try:
                await pool.execute(
                    "UPDATE strike_email_accounts SET last_used_at=now(), fail_count=0 WHERE id=$1",
                    ea["id"],
                )
            except Exception:
                log_exc_swallow(
                    log, f"staggered_strike: email success update id={ea.get('id')}"
                )
        else:
            try:
                await pool.execute(
                    """UPDATE strike_email_accounts
                       SET fail_count = fail_count + 1,
                           is_active = CASE WHEN fail_count + 1 >= 3 THEN FALSE ELSE is_active END
                       WHERE id=$1""",
                    ea["id"],
                )
            except Exception:
                log_exc_swallow(
                    log, f"staggered_strike: email fail_count update id={ea.get('id')}"
                )

    # NCMEC только для CSAM
    if cat.get("ncmec") and db_emails:
        ncmec_addr = "cybertipline@ncmec.org"
        body_ncmec = _build_ncmec_email(target_clean, report_time)
        for ea in db_emails:
            smtp_secret, auth_type = await _email_secret_for_account(pool, ea)
            ok_n, err_n = await _send_email(
                ea["smtp_host"],
                ea["smtp_port"],
                ea["email"],
                smtp_secret,
                ea["email"],
                ncmec_addr,
                f"URGENT CyberTip: CSAM on Telegram — t.me/{target_clean}",
                body_ncmec,
                auth_type=auth_type,
            )
            result["emails"].append(
                {
                    "from": ea["email"],
                    "to": ncmec_addr,
                    "label": "ncmec",
                    "ok": ok_n,
                    "err": err_n,
                }
            )
            if ok_n:
                result["total_sent"] += 1
                result["ncmec"] = True
                break

    return result


async def _email_secret_for_account(pool, email_account: dict) -> tuple[str, str]:
    auth_type = email_account.get("auth_type") or "password"
    if auth_type != "oauth":
        return str(email_account.get("smtp_pass") or ""), "password"

    from services import email_oauth as _email_oauth

    return await _email_oauth.get_access_token(pool, email_account), "oauth"


async def staggered_strike(
    plan: StrikePlan,
    progress_cb=None,
    pool=None,
) -> list[StrikeResult]:
    """
    Эшелонированная атака: волны аккаунтов с нарастающей интенсивностью.

    progress_cb(phase: str, detail: str) — опциональный колбэк для обновления UI.
    """
    all_results: list[StrikeResult] = []
    sem = asyncio.Semaphore(_CONCURRENCY)

    # Claim every account for the whole strike so warmup/op_worker won't drive
    # the same sessions in parallel (concurrent clients on one auth_key = ban).
    _claimed_ids = [int(a["id"]) for a in plan.accounts if a.get("id")]
    try:
        from services import op_worker as _opw

        await _opw.mark_accounts_in_use(_claimed_ids)
    except Exception:
        log_exc_swallow(log, "staggered_strike: mark_accounts_in_use failed")

    try:
        for _target_idx, target in enumerate(plan.targets):
            # Inter-target cooldown: не бить по следующей цели сразу — это копит
            # per-account объём далеко за безопасный предел и провоцирует флуд.
            if _target_idx > 0:
                _tc = random.uniform(120, 300)
                if progress_cb:
                    await progress_cb(
                        "strike_target_cooldown",
                        f"⏳ Пауза {_tc:.0f}с перед следующей целью...",
                    )
                await asyncio.sleep(_tc)
            intel = plan.intel.get(target, {})
            result = StrikeResult(target=target, unique_accounts=len(plan.accounts))
            result.phase_results["recon"] = {
                "msgs": len(intel.get("latest_msg_ids", []) or []),
                "pinned": len(intel.get("pinned_msg_ids", []) or []),
                "admins": len(intel.get("admin_ids", []) or []),
                "nodes": (
                    len(intel.get("mentioned_usernames", []) or [])
                    + len(intel.get("bot_usernames", []) or [])
                    + (1 if intel.get("linked_group_id") else 0)
                ),
            }
            t_start = time.time()

            if progress_cb:
                await progress_cb(
                    "strike_wave1",
                    f"🎯 {_telegram_target_display(target)}: Волна 1 — {len(plan.waves[0]) if plan.waves else 0} аккаунтов",
                )

            # ═══ Волна 1: ReportPeer + ReportSpam + Join + Internal ═══
            wave_results: list[dict] = []
            if plan.waves:
                texts_w1 = assign_texts(plan.preset or plan.reason, len(plan.waves[0]))
                tasks = [
                    _one_account_strike(
                        acc,
                        target,
                        intel,
                        plan.reason,
                        plan.preset,
                        texts_w1,
                        i,
                        0,
                        sem,
                        mode=plan.mode,
                        pool=pool,
                    )
                    for i, acc in enumerate(plan.waves[0])
                ]
                wave_results = _safe_gather_results(
                    await asyncio.gather(*tasks, return_exceptions=True)
                )

            # Пауза между волнами
            if len(plan.waves) > 1:
                wc = random.uniform(*_WAVE_COOLDOWN)
                if progress_cb:
                    await progress_cb(
                        "strike_cooldown", f"⏳ Пауза {wc:.0f}с перед волной 2..."
                    )
                await asyncio.sleep(wc)

            # ═══ Волна 2: Поддержка — другие причины, админы, боты ═══
            if len(plan.waves) > 1 and plan.waves[1]:
                if progress_cb:
                    await progress_cb(
                        "strike_wave2",
                        f"🎯 {_telegram_target_display(target)}: Волна 2 — {len(plan.waves[1])} аккаунтов",
                    )
                texts_w2 = assign_texts(plan.preset or plan.reason, len(plan.waves[1]))
                tasks = [
                    _one_account_strike(
                        acc,
                        target,
                        intel,
                        plan.reason,
                        plan.preset,
                        texts_w2,
                        i,
                        1,
                        sem,
                        mode=plan.mode,
                        pool=pool,
                    )
                    for i, acc in enumerate(plan.waves[1])
                ]
                w2_results = _safe_gather_results(
                    await asyncio.gather(*tasks, return_exceptions=True)
                )
                wave_results.extend(w2_results)

            # Пауза перед финальной волной
            if len(plan.waves) > 2:
                wc = random.uniform(*_WAVE_COOLDOWN)
                if progress_cb:
                    await progress_cb(
                        "strike_cooldown", f"⏳ Пауза {wc:.0f}с перед волной 3..."
                    )
                await asyncio.sleep(wc)

            # ═══ Волна 3: Завершение — блокировка, финальные жалобы ═══
            if len(plan.waves) > 2 and plan.waves[2]:
                if progress_cb:
                    await progress_cb(
                        "strike_wave3",
                        f"🎯 {_telegram_target_display(target)}: Волна 3 (финал) — {len(plan.waves[2])} аккаунтов",
                    )
                texts_w3 = assign_texts(plan.preset or plan.reason, len(plan.waves[2]))
                tasks = [
                    _one_account_strike(
                        acc,
                        target,
                        intel,
                        plan.reason,
                        plan.preset,
                        texts_w3,
                        i,
                        2,
                        sem,
                        mode=plan.mode,
                        pool=pool,
                    )
                    for i, acc in enumerate(plan.waves[2])
                ]
                w3_results = _safe_gather_results(
                    await asyncio.gather(*tasks, return_exceptions=True)
                )
                wave_results.extend(w3_results)

            # Агрегация — явный маппинг ключей aggregate_results → поля StrikeResult
            agg = aggregate_results(wave_results)
            result.peer_reported += agg.get("peer", 0)
            result.multi_reason += agg.get("multi", 0)
            result.photo_reported = result.photo_reported or bool(agg.get("photo", 0))
            result.pinned_reported += agg.get("pinned", 0)
            result.msgs_reported += agg.get("msgs", 0)
            result.msgs_fetched += agg.get("msgs_fetched", 0)
            result.spam_signaled += agg.get("spam", 0)
            result.reactions += agg.get("reacts", 0)
            result.admins_reported += agg.get("admins", 0)
            result.linked_group_reported = result.linked_group_reported or bool(
                agg.get("linked_grp", 0)
            )
            result.bots_reported += agg.get("bots", 0)
            result.forwarded += agg.get("fwd", 0)
            result.blocked += agg.get("blocked", 0)
            result.errors = [r.get("error") for r in wave_results if r.get("error")]
            for wave_result in wave_results:
                result.errors.extend(wave_result.get("errors", [])[:3])
                if wave_result.get("rate_limited"):
                    result.errors.append(
                        f"account {wave_result.get('_acc_id', '?')}: Telegram rate limit"
                    )

            # Логирование итогов векторов
            failed_accounts = agg.get("failed", 0)
            total_accounts = len(wave_results)
            if result.peer_reported > 0:
                log.info(
                    "strike_engine: target=%s peer_reports=%d multi=%d msgs=%d "
                    "spam=%d admins=%d accounts=%d/%d",
                    target,
                    result.peer_reported,
                    result.multi_reason,
                    result.msgs_reported,
                    result.spam_signaled,
                    result.admins_reported,
                    result.peer_reported,
                    total_accounts,
                )
            else:
                log.warning(
                    "strike_engine: target=%s NO peer_reports — %d/%d accounts failed, "
                    "errors=%s",
                    target,
                    failed_accounts,
                    total_accounts,
                    "; ".join(result.errors[:2])[:120] if result.errors else "none",
                )

            # ═══ Фаза: Network nodes (параллельно) ═══
            if progress_cb:
                await progress_cb(
                    "strike_network",
                    f"🌐 {_telegram_target_display(target)}: Атака сетевых узлов...",
                )
            net = await strike_network_nodes_v2(
                plan.accounts, intel, plan.reason, plan.preset
            )
            result.network_nodes = net.get("nodes_attacked", 0)
            result.network_reports = net.get("total_reports", 0)

            # ═══ Фаза: External escalation ═══
            if progress_cb:
                await progress_cb(
                    "strike_escalation",
                    f"📤 {_telegram_target_display(target)}: Внешняя эскалация...",
                )
            abuse_res = await submit_abuse_form(
                target,
                plan.preset or plan.reason,
                title=intel.get("title", ""),
                members=intel.get("members", 0),
            )
            result.abuse_form_ok = abuse_res.get("ok", False)
            log.info(
                "strike_engine: abuse_forms target=%s submitted=%d/%d ok=%s",
                target,
                abuse_res.get("submitted", 0),
                abuse_res.get("total", 0),
                result.abuse_form_ok,
            )

            # ═══ Фаза: SpamBot + anti-scam боты ═══
            # Используем лучший аккаунт (первый по trust_score после pre-flight сортировки)
            spambot_result = await _escalate_to_spambot(
                plan.accounts[0] if plan.accounts else None, target
            )
            result.spambot_escalation = (
                spambot_result.get("status", "unknown")
                if isinstance(spambot_result, dict)
                else "skipped"
            )
            log.info(
                "strike_engine: spambot_escalation target=%s status=%s bots=%s",
                target,
                result.spambot_escalation,
                spambot_result.get("bots", {}) if isinstance(spambot_result, dict) else {},
            )

            # ═══ Фаза: Email-эскалация ═══
            if pool and plan.owner_id:
                if progress_cb:
                    await progress_cb(
                        "strike_email",
                        f"📧 {_telegram_target_display(target)}: Email-эскалация (abuse/dmca/dpo/security)...",
                    )
                try:
                    email_res = await _run_email_escalation(
                        pool, plan.owner_id, target, plan.reason, plan.preset
                    )
                    result.emails_sent = email_res.get("total_sent", 0)
                    result.email_escalation = email_res
                    log.info(
                        "strike_engine: email_escalation target=%s sent=%d ncmec=%s",
                        target,
                        result.emails_sent,
                        email_res.get("ncmec", False),
                    )
                except Exception:
                    log_exc_swallow(
                        log, f"staggered_strike: email_escalation failed target={target}"
                    )

            result.duration_s = time.time() - t_start
            log.info(
                "strike_engine: staggered_strike done target=%s duration=%.1fs "
                "peer=%d msgs=%d network_nodes=%d abuse=%s spambot=%s emails=%d",
                target,
                result.duration_s,
                result.peer_reported,
                result.msgs_reported,
                result.network_nodes,
                result.abuse_form_ok,
                result.spambot_escalation,
                result.emails_sent,
            )
            all_results.append(result)
    finally:
        # Release the account claims (success or error).
        try:
            from services import op_worker as _opw

            await _opw.release_accounts(_claimed_ids)
        except Exception:
            log_exc_swallow(log, "staggered_strike: release_accounts failed")

    return all_results


# ── Network node attack v2 (parallel) ──────────────────────────────────────────


async def _attack_single_node(
    acc: dict,
    node: str,
    reason: str,
    preset: str | None,
    sem: asyncio.Semaphore,
) -> dict:
    """Атакует один узел сети одним аккаунтом."""
    from services import account_manager

    label = preset or reason
    text = assign_texts(label, 1)[0]

    async with sem:
        try:
            res = await account_manager.report_peer_deep_v2(
                acc["session_str"],
                node,
                reason,
                message=text,
                msg_messages=assign_texts(label, 8),
                max_msg_reports=30,
                block_after=False,
                multi_reason=True,
                join_first=False,
                negative_react=True,
                report_admins=False,
                report_linked_bots=False,
                forward_to_bot=False,
                report_photo=True,
                report_pinned=True,
                report_linked_group=False,
                wave_num=99,  # network node — отдельная волна
                _acc=acc,
            )
            return {
                "node": node,
                "ok": res.get("peer_reported", False),
                "reports": 1 + res.get("multi_reason_sent", 0),
            }
        except Exception as e:
            log.warning("strike_network_node %s: %s", node, e)
            return {"node": node, "ok": False, "error": str(e)[:100]}


async def strike_network_nodes_v2(
    accounts: list[dict],
    intel: dict,
    reason: str,
    preset: str | None,
) -> dict[str, int]:
    """
    Параллельная атака всех обнаруженных узлов сети.
    Использует ротацию аккаунтов.
    """
    nodes: list[str] = []
    if intel.get("linked_group_id"):
        nodes.append(str(intel["linked_group_id"]))
    nodes.extend(intel.get("mentioned_usernames", [])[:8])
    nodes.extend(intel.get("bot_usernames", [])[:5])

    if not nodes or not accounts:
        return {"nodes_attacked": 0, "total_reports": 0}

    sem = asyncio.Semaphore(min(_CONCURRENCY, len(nodes)))
    tasks = []
    for i, node in enumerate(nodes):
        acc = accounts[i % len(accounts)]
        tasks.append(_attack_single_node(acc, node, reason, preset, sem))

    results_raw = await asyncio.gather(*tasks, return_exceptions=True)
    results = []
    for r in results_raw:
        if isinstance(r, asyncio.CancelledError):
            raise r
        if isinstance(r, BaseException):
            log.warning("strike_network_node task failed: %s", r)
            results.append({"ok": False, "reports": 0})
        else:
            results.append(r)
    nodes_hit = sum(1 for r in results if r.get("ok"))
    total_rep = sum(r.get("reports", 0) for r in results)
    return {"nodes_attacked": nodes_hit, "total_reports": total_rep}


# ── External escalation ────────────────────────────────────────────────────────


async def submit_abuse_form(
    target_username: str,
    reason: str,
    title: str = "",
    members: int = 0,
) -> dict[str, Any]:
    """Отправляет жалобы сразу в несколько официальных каналов Telegram."""
    clean = target_username.lstrip("@")
    reason_map = {
        "drugs": (
            "illegal drug sales and drug trafficking",
            "Незаконная торговля наркотиками и психоактивными веществами",
        ),
        "terrorism": (
            "terrorism, extremism and incitement to violence",
            "Террористический контент, экстремизм, призывы к насилию",
        ),
        "childabuse": (
            "child sexual abuse material (CSAM) — urgent",
            "Материалы сексуальной эксплуатации детей (CSAM) — срочно",
        ),
        "csam": (
            "child sexual abuse material (CSAM) — urgent",
            "Материалы сексуальной эксплуатации детей — требует немедленных действий",
        ),
        "fraud": (
            "financial fraud, scam and money theft",
            "Финансовое мошенничество, скам, кража денег",
        ),
        "weapons": (
            "illegal weapons trafficking",
            "Незаконный оборот оружия и взрывчатых веществ",
        ),
        "darknet": (
            "darknet criminal marketplace distributing illegal goods",
            "Даркнет-маркетплейс — незаконная торговля запрещёнными товарами",
        ),
        "violence": (
            "graphic violence and promotion of violent acts",
            "Пропаганда насилия и жестокости",
        ),
        "spam": (
            "coordinated spam and platform manipulation",
            "Координированный спам и манипуляции с платформой",
        ),
        "pornography": (
            "illegal pornographic content distributed without age verification",
            "Незаконный порнографический контент без проверки возраста",
        ),
        "escort": (
            "prostitution and sexual exploitation services",
            "Проституция, эскорт и сексуальная эксплуатация",
        ),
        "other": (
            "systematic Terms of Service violation",
            "Систематическое нарушение Условий использования",
        ),
    }
    en_text, ru_text = reason_map.get(reason, reason_map["other"])

    bodies = [
        # Версия 1 — официальный английский запрос
        (
            f"ABUSE REPORT — {en_text.upper()}\n\n"
            f"Channel: https://t.me/{clean}\n"
            f"Username: @{clean}\n"
            f"Title: {title}\n"
            f"Subscribers: {members:,}\n\n"
            f"This Telegram channel systematically violates Telegram ToS through {en_text}. "
            f"The violations are active, ongoing and publicly accessible to all users including minors. "
            f"The channel is not merely borderline — it explicitly and openly engages in {en_text}. "
            f"Multiple independent users have already reported this channel. "
            f"We request immediate review and removal of this channel."
        ),
        # Версия 2 — русский технический запрос
        (
            f"ЖАЛОБА НА НАРУШЕНИЕ — {ru_text}\n\n"
            f"Канал: https://t.me/{clean}\n"
            f"Юзернейм: @{clean}\n"
            f"Название: {title}\n"
            f"Подписчиков: {members:,}\n\n"
            f"Данный Telegram-канал систематически нарушает Условия использования: {ru_text}. "
            f"Нарушение носит публичный, постоянный характер и доступно всем пользователям. "
            f"Требуем немедленного рассмотрения и удаления."
        ),
    ]

    _ua_pool = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_3) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
        "Mozilla/5.0 (X11; Linux x86_64; rv:124.0) Gecko/20100101 Firefox/124.0",
    ]

    results = []
    for idx, body in enumerate(bodies):
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=25),
                headers={
                    "User-Agent": _ua_pool[idx % len(_ua_pool)],
                    "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
                },
            ) as sess:
                await asyncio.sleep(random.uniform(2.0, 5.0))  # между отправками
                async with sess.post(
                    "https://telegram.org/support",
                    data={
                        "message": body,
                        "technical_info": f"@{clean}",
                        "question_type": "report",
                    },
                    allow_redirects=True,
                ) as resp:
                    ok = resp.status in (200, 201, 302)
                    results.append(ok)
                    log.info(
                        "abuse_form[%d] status=%d ok=%s target=%s",
                        idx,
                        resp.status,
                        ok,
                        clean,
                    )
        except Exception as e:
            log.warning("submit_abuse_form[%d]: %s", idx, e)
            results.append(False)

    return {"ok": any(results), "submitted": sum(results), "total": len(results)}


async def _escalate_to_spambot(acc: dict | None, target_username: str) -> dict:
    """Эскалация через официальные Telegram-боты: @SpamBot, @notoscam, @stopCA.
    Пересылает несколько последних сообщений канала в каждый бот.
    Возвращает dict со статусом по каждому боту.
    """
    if not acc:
        return {"status": "no_account"}
    from services import account_manager

    clean = target_username.lstrip("@")
    # Официальные боты приёма жалоб: SpamBot + anti-scam инстанции
    escalation_bots = ["SpamBot", "notoscam", "stopCA"]
    results: dict[str, str] = {}

    def _is_stop_error(err_text: str) -> bool:
        up = err_text.upper()
        return (
            "PEER_FLOOD" in up
            or "FLOOD_WAIT" in up
            or "USER_DEACTIVATED" in up
            or "AUTH_KEY_UNREGISTERED" in up
            or "SESSION_REVOKED" in up
        )

    # Аккаунт только что отработал полный страйк — даём ему остыть перед
    # эскалацией, иначе серия forward сразу после рейда = риск PEER_FLOOD.
    await asyncio.sleep(random.uniform(20, 45))

    client = account_manager._make_client(acc["session_str"], acc)
    try:
        await asyncio.wait_for(client.connect(), timeout=15)
        msgs = []
        try:
            msgs = await asyncio.wait_for(
                client.get_messages(clean, limit=5), timeout=15
            )
            msgs = [m for m in (msgs or []) if m and not m.service]
        except Exception:
            pass
        for bot_name in escalation_bots:
            try:
                bot_entity = await asyncio.wait_for(
                    client.get_entity(bot_name), timeout=8
                )
                await client.send_message(bot_entity, "/start")
                await asyncio.sleep(random.uniform(1.5, 3.0))
                fwd_count = 0
                _stop = False
                for m in msgs[:3]:
                    try:
                        await client.forward_messages(bot_entity, m)
                        fwd_count += 1
                        await asyncio.sleep(random.uniform(0.8, 1.8))
                    except Exception as _fe2:
                        # PEER_FLOOD/FloodWait/ban during forward → прекратить эскалацию
                        if _is_stop_error(str(_fe2)):
                            log.warning(
                                "escalate_spambot: stop signal acc=%s: %s",
                                acc.get("id"),
                                str(_fe2)[:80],
                            )
                            _stop = True
                            break
                if _stop:
                    results[bot_name] = "stopped_flood"
                    break
                # SpamBot принимает /report после пересылки
                if bot_name == "SpamBot" and fwd_count > 0:
                    try:
                        await asyncio.sleep(1.5)
                        await client.send_message(bot_entity, "/report")
                    except Exception:
                        pass
                results[bot_name] = "sent" if fwd_count > 0 else "no_msgs"
                log.info(
                    "escalate_spambot: %s → %s fwd=%d target=%s",
                    bot_name,
                    results[bot_name],
                    fwd_count,
                    clean,
                )
                await asyncio.sleep(random.uniform(2.0, 4.0))
            except Exception as e:
                results[bot_name] = f"err:{str(e)[:40]}"
                log.warning(
                    "escalate_spambot[%s] acc=%s: %s",
                    bot_name,
                    acc.get("id"),
                    str(e)[:80],
                )
    except Exception as e:
        log.warning("_escalate_to_spambot connect: %s", e)
        results["connect"] = f"err:{str(e)[:40]}"
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
    sent = sum(1 for v in results.values() if v == "sent")
    return {
        "status": "sent" if sent > 0 else "failed",
        "bots": results,
        "sent_count": sent,
    }


# ── Verification loop ──────────────────────────────────────────────────────────


async def verify_target_takedown(
    acc: dict,
    target_username: str,
    max_attempts: int = 6,
    delay_range: tuple = (30, 90),
) -> bool:
    """
    Проверяет, был ли целевой канал удалён/заблокирован.
    Делает до max_attempts попыток с растущими задержками.

    Признаки удаления:
      - entity не найден (ChannelPrivate или ValueError)
      - канал недоступен (username not found)
      - канал забанен (restricted)

    Возвращает True если канал недоступен (вероятно удалён).
    """
    from services import account_manager

    clean = target_username.lstrip("@")

    for attempt in range(max_attempts):
        delay = random.uniform(*delay_range) * (1 + attempt * 0.5)
        await asyncio.sleep(delay)

        client = account_manager._make_client(acc["session_str"], acc)
        try:
            await asyncio.wait_for(client.connect(), timeout=15)
            entity = await client.get_entity(clean)

            # Проверка на бан/ограничение
            if hasattr(entity, "restricted") and entity.restricted:
                await client.disconnect()
                return True
            if hasattr(entity, "restriction_reason") and entity.restriction_reason:
                await client.disconnect()
                return True

            await client.disconnect()
            # Канал всё ещё жив — ждём и пробуем снова
        except Exception as e:
            try:
                await client.disconnect()
            except Exception:
                log_exc_swallow(log, "Сбой disconnect клиента в verify_target_takedown")
            err_str = str(e).lower()
            # СНАЧАЛА исключаем ошибки НАШЕГО аккаунта/рейтлимита — это НЕ признак
            # удаления цели. Иначе FloodWait или бан нашей сессии ложно зачтётся
            # как "канал удалён" (фейковый успех, Survival Contract §24).
            if any(
                kw in err_str
                for kw in (
                    "flood",
                    "wait of",
                    "too many",
                    "auth_key",
                    "session_revoked",
                    "session_expired",
                    "user_deactivated",
                    "phone_number_banned",
                )
            ):
                log.info(
                    "verify_takedown: own-account/flood error, not a takedown: %s",
                    err_str[:80],
                )
                continue
            # Эти ошибки означают что канал удалён/недоступен
            if any(
                kw in err_str
                for kw in (
                    "not found",
                    "no user",
                    "channel private",
                    "username not found",
                    "username_invalid",
                    "username_not_occupied",
                    "cannot get entity",
                    "peer not found",
                )
            ):
                return True
            # Другие ошибки — возможно временные, пробуем ещё
            log.info(
                "verify_takedown attempt %d/%d for %s: %s",
                attempt + 1,
                max_attempts,
                clean,
                err_str[:80],
            )

    return False  # цель всё ещё жива после всех попыток


# ── Aggregate helper ───────────────────────────────────────────────────────────


def aggregate_results(results: list[dict]) -> dict:
    """Суммирует результаты атаки."""
    s = {
        "peer": 0,
        "multi": 0,
        "photo": 0,
        "pinned": 0,
        "msgs": 0,
        "msgs_fetched": 0,
        "spam": 0,
        "reacts": 0,
        "admins": 0,
        "linked_grp": 0,
        "bots": 0,
        "fwd": 0,
        "blocked": 0,
        "failed": 0,
    }
    for r in results:
        if not r:
            s["failed"] += 1
            continue
        if r.get("peer_reported"):
            s["peer"] += 1
        else:
            s["failed"] += 1
        s["multi"] += r.get("multi_reason_sent", 0)
        s["photo"] += 1 if r.get("photo_reported") else 0
        s["pinned"] += r.get("pinned_reported", 0)
        s["msgs"] += r.get("msg_reported", 0)
        s["msgs_fetched"] += r.get("msgs_fetched", 0)
        s["spam"] += r.get("spam_signaled", 0)
        s["reacts"] += r.get("reactions_sent", 0)
        s["admins"] += r.get("admins_reported", 0)
        s["linked_grp"] += 1 if r.get("linked_group_reported") else 0
        s["bots"] += r.get("bots_reported", 0)
        s["fwd"] += r.get("forwarded", 0)
        s["blocked"] += 1 if r.get("blocked") else 0
    return s


def _build_vector_diagnostics(r: StrikeResult) -> list[str]:
    """Build compact diagnostics for vectors that produced zero effect."""
    notes: list[str] = []
    err_blob = " ".join(r.errors or [])
    recon = r.phase_results.get("recon", {})
    recon_msgs = int(recon.get("msgs", 0) or 0)

    if r.msgs_fetched == 0:
        if recon_msgs > 0:
            notes.append("history_access_mismatch")
        notes.append("history_unavailable")
    elif r.msgs_reported == 0:
        notes.append("history_loaded_but_no_actions")

    if r.pinned_reported == 0:
        notes.append("no_pinned_actions")
    if r.admins_reported == 0:
        notes.append("no_admin_actions")
    if r.network_nodes == 0:
        notes.append("no_network_nodes")
    if r.bots_reported == 0 and r.forwarded == 0:
        notes.append("no_bot_vectors")

    if "ReportResultChooseOption" in err_blob:
        notes.append("telegram_dynamic_report_flow")
    if "FLOOD" in err_blob.upper() or "TOO_MUCH" in err_blob.upper():
        notes.append("rate_limited")

    return notes[:5]


def format_strike_summary(results: list[StrikeResult]) -> str:
    """Форматирует итоговый отчёт об атаке в HTML."""
    lines = ["⚔️ <b>Strike — Итоговый отчёт</b>\n"]
    for r in results:
        # Приоритет: если подтверждено удаление → 🟢, иначе оцениваем эффективность удара.
        # 🔴 только если peer_reported=0 (удар вообще не прошёл).
        if r.verified_down:
            status = "🟢"  # подтверждено удаление
        elif r.peer_reported > 0 and (
            r.msgs_reported > 0 or r.pinned_reported > 0 or r.msgs_fetched > 0
        ):
            status = "⚔️"  # полноценный удар (жалобы + сообщения)
        elif r.peer_reported > 0:
            status = "🟡"  # частичный удар (только ReportPeer, без сообщений)
        else:
            status = "🔴"  # удар не прошёл (нет даже базовой жалобы)
        lines.append(
            f"{status} <code>{r.target}</code>\n"
            f"  ├ Жалоб на канал: <b>{r.peer_reported}</b> "
            f"(+{r.multi_reason} доп. причин)\n"
            f"  ├ Сообщений: <b>{r.msgs_reported}</b>"
            f"{'/' + str(r.msgs_fetched) if r.msgs_fetched > r.msgs_reported else ''}"
            f" · Закреплённых: <b>{r.pinned_reported}</b> · "
            f"Фото: <b>{'✅' if r.photo_reported else '❌'}</b>\n"
            f"  ├ Админов: <b>{r.admins_reported}</b> · "
            f"Реакций: <b>{r.reactions}</b> · "
            f"Спам-сигналов: <b>{r.spam_signaled}</b>\n"
            f"  ├ Сетевых узлов: <b>{r.network_nodes}</b> "
            f"(+{r.network_reports} жалоб)\n"
            f"  ├ Группа: <b>{'✅' if r.linked_group_reported else '❌'}</b> · "
            f"Боты: <b>{r.bots_reported}</b> · "
            f"Forward: <b>{r.forwarded}</b>\n"
            f"  ├ Abuse форма: <b>{'✅' if r.abuse_form_ok else '❌'}</b> · "
            f"SpamBot: <b>{'✅' if r.spambot_escalation == 'sent' else '❌'}</b> · "
            f"Заблокирован: <b>{r.blocked}</b>\n"
            f"  ├ 📧 Email: <b>{r.emails_sent}</b> отправлено"
            + (" · NCMEC: ✅" if r.email_escalation.get("ncmec") else "")
            + (
                f" (<i>{r.email_escalation.get('skip_reason', 'не настроен')}</i>)"
                if r.emails_sent == 0
                and r.email_escalation.get("skip_reason") not in (None, "no pool/owner")
                else ""
            )
            + "\n"
            f"  └ Длительность: <b>{r.duration_s:.0f}с</b> · "
            f"Аккаунтов: <b>{r.unique_accounts}</b>"
        )
        recon = r.phase_results.get("recon", {})
        lines.append(
            "  🧭 Разведка: "
            f"сообщений <b>{int(recon.get('msgs', 0))}</b> · "
            f"закрепов <b>{int(recon.get('pinned', 0))}</b> · "
            f"админов <b>{int(recon.get('admins', 0))}</b> · "
            f"узлов <b>{int(recon.get('nodes', 0))}</b>"
        )
        vector_hits = sum(
            [
                1 if r.msgs_reported > 0 else 0,
                1 if r.pinned_reported > 0 else 0,
                1 if r.admins_reported > 0 else 0,
                1 if r.spam_signaled > 0 else 0,
                1 if r.network_nodes > 0 else 0,
                1 if (r.bots_reported > 0 or r.forwarded > 0) else 0,
            ]
        )
        lines.append(f"  📊 Покрытие векторов: <b>{vector_hits}/6</b>")
        if r.errors:
            sample = html.escape("; ".join(r.errors[:3])[:220])
            lines.append(f"  ⚠️ Ошибок: {len(r.errors)} · <code>{sample}</code>")
        diagnostics = _build_vector_diagnostics(r)
        if diagnostics:
            diag_text = html.escape(", ".join(diagnostics))
            lines.append(f"  🧪 Диагностика: <code>{diag_text}</code>")
        if r.verified_down is not None:
            lines.append(
                f"  🔍 Проверка: {'✅ УДАЛЁН' if r.verified_down else '⚠️ Всё ещё активен'}"
            )
    return "\n".join(lines)


# ── Backward-compatible API (для channel_ops.py) ────────────────────────────────


async def parallel_strike(
    accounts: list[dict],
    peer_username: str,
    intel: dict,
    reason: str,
    preset: str | None,
) -> list[dict]:
    """
    Обратно-совместимый вызов — запускает report_peer_deep параллельно.
    Использует v2 engine если доступен.
    """
    from services import account_manager

    label = preset or reason
    texts = assign_texts(label, len(accounts))
    sem = asyncio.Semaphore(_CONCURRENCY)

    async def _one(acc: dict, text: str) -> dict:
        async with sem:
            try:
                return await account_manager.report_peer_deep_v2(
                    acc["session_str"],
                    peer_username,
                    reason,
                    message=text,
                    msg_messages=assign_texts(label, 10),
                    max_msg_reports=50,
                    block_after=True,
                    multi_reason=True,
                    join_first=True,
                    negative_react=True,
                    report_admins=True,
                    report_linked_bots=True,
                    forward_to_bot=True,
                    report_photo=True,
                    report_pinned=True,
                    report_linked_group=True,
                    wave_num=0,
                    _acc=acc,
                )
            except Exception as e:
                log.warning("parallel_strike acc %s: %s", acc.get("id"), e)
                return {"peer_reported": False, "error": str(e)[:100]}

    results = _safe_gather_results(
        await asyncio.gather(
            *[_one(acc, txt) for acc, txt in zip(accounts, texts)],
            return_exceptions=True,
        )
    )
    return list(results)


async def strike_network_nodes(
    accounts: list[dict],
    intel: dict,
    reason: str,
    preset: str | None,
) -> dict[str, int]:
    """Обратно-совместимый вызов — использует v2 (параллельная атака узлов)."""
    return await strike_network_nodes_v2(accounts, intel, reason, preset)


# ══════════════════════════════════════════════════════════════════════════════
# MINI-STRIKE — одиночный удар с одного аккаунта + email + внешние организации
# ══════════════════════════════════════════════════════════════════════════════

import base64
import smtplib
import ssl as _ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# Категории для мини-страйка (один аккаунт, ручное обнаружение)
MINI_CATEGORIES: dict[str, dict] = {
    "csam": {
        "label": "🚨 CSAM (детский контент)",
        "tg_reason": "childabuse",
        "severity": "CRITICAL",
        "ncmec": True,
        "email_subject": "URGENT: CSAM on Telegram",
        "report_msg_key": "childabuse",
    },
    "drugs": {
        "label": "💊 Наркотики",
        "tg_reason": "drugs",
        "severity": "HIGH",
        "ncmec": False,
        "email_subject": "Report: Illegal Drug Sales on Telegram",
        "report_msg_key": "drugs",
    },
    "weapons": {
        "label": "🔫 Оружие",
        "tg_reason": "violence",
        "severity": "HIGH",
        "ncmec": False,
        "email_subject": "Report: Illegal Weapons on Telegram",
        "report_msg_key": "weapons",
    },
    "terrorism": {
        "label": "💣 Терроризм/экстремизм",
        "tg_reason": "violence",
        "severity": "CRITICAL",
        "ncmec": False,
        "email_subject": "Report: Terrorism/Extremism on Telegram",
        "report_msg_key": "terrorism",
    },
    "prostitution": {
        "label": "🔞 Проституция/секс-трафик",
        "tg_reason": "pornography",
        "severity": "HIGH",
        "ncmec": False,
        "email_subject": "Report: Sex Trafficking on Telegram",
        "report_msg_key": "escort",
    },
    "fraud": {
        "label": "💸 Мошенничество",
        "tg_reason": "spam",
        "severity": "MEDIUM",
        "ncmec": False,
        "email_subject": "Report: Fraud/Scam on Telegram",
        "report_msg_key": "fraud",
    },
    "darknet": {
        "label": "🕸 Даркнет-услуги",
        "tg_reason": "other",
        "severity": "HIGH",
        "ncmec": False,
        "email_subject": "Report: Darknet Services on Telegram",
        "report_msg_key": "darknet",
    },
}


def _build_abuse_tg_email(target: str, cat: dict, report_time: str) -> str:
    chan_url = f"https://t.me/{target}"
    ref_id = f"MINI-{target.upper()}-{report_time[:10].replace('-', '')}"

    # Правовые ссылки в зависимости от категории
    legal_refs: dict[str, str] = {
        "csam": (
            "Legal basis:\n"
            "• EU Digital Services Act (DSA) Article 16 — Notice and Takedown obligation\n"
            "• UNCRC Optional Protocol on the Sale of Children (Article 3)\n"
            "• Russian Federal Law No. 436-FZ (protection of children from harmful information)\n"
            "• Russian Federal Law No. 149-FZ Article 15.1 (mandatory blocking of CSAM)\n"
            "• US PROTECT Our Children Act (18 U.S.C. § 2258A) — mandatory NCMEC report"
        ),
        "drugs": (
            "Legal basis:\n"
            "• EU Digital Services Act (DSA) Article 16 — Notice and Takedown for illegal content\n"
            "• Russian Federal Law No. 149-FZ (Information, Information Technologies and "
            "Information Protection) — Article 15.1: mandatory blocking of drug-related content\n"
            "• UN Convention against Illicit Traffic in Narcotic Drugs (1988)\n"
            "• Russian Federal Law No. 3-FZ on Narcotic Drugs and Psychotropic Substances"
        ),
        "terrorism": (
            "Legal basis:\n"
            "• EU Digital Services Act (DSA) Article 16 — Notice and Takedown for terrorist content\n"
            "• EU Regulation 2021/784 on Terrorist Content Online (TCO Regulation) — "
            "1-hour removal deadline upon competent authority order\n"
            "• Russian Federal Law No. 149-FZ Article 15.3 — immediate blocking of "
            "calls for mass disorder, extremism, terrorism\n"
            "• Russian Federal Law No. 35-FZ on Combating Terrorism\n"
            "• UN Security Council Resolution 1373 (2001) — counter-terrorism obligations"
        ),
        "weapons": (
            "Legal basis:\n"
            "• EU Digital Services Act (DSA) Article 16 — Notice and Takedown for illegal content\n"
            "• Russian Federal Law No. 150-FZ on Weapons — Article 6 (prohibition of illegal trade)\n"
            "• Russian Federal Law No. 149-FZ Article 15.1 — mandatory blocking\n"
            "• UN Arms Trade Treaty (ATT) — illegal arms trafficking"
        ),
        "fraud": (
            "Legal basis:\n"
            "• EU Digital Services Act (DSA) Article 16 — Notice and Takedown\n"
            "• GDPR Article 17 (Right to Erasure) — fraudulent processing of personal data\n"
            "• Russian Federal Law No. 149-FZ on Information Technologies\n"
            "• Russian Federal Law No. 152-FZ on Personal Data — Article 21 "
            "(obligation to cease unlawful processing)\n"
            "• Directive 2013/40/EU on attacks against information systems"
        ),
        "prostitution": (
            "Legal basis:\n"
            "• EU Digital Services Act (DSA) Article 16 — Notice and Takedown\n"
            "• UN Protocol to Prevent, Suppress and Punish Trafficking in Persons (Palermo Protocol)\n"
            "• Russian Federal Law No. 149-FZ Article 15.1 — mandatory blocking\n"
            "• Russian Criminal Code Article 240–242 (promotion of prostitution)"
        ),
        "darknet": (
            "Legal basis:\n"
            "• EU Digital Services Act (DSA) Article 16 — Notice and Takedown\n"
            "• GDPR Article 17 — Right to Erasure of stolen personal data\n"
            "• Russian Federal Law No. 149-FZ on Information Technologies\n"
            "• Directive 2013/40/EU on cyber attacks and illegal marketplace operations"
        ),
    }
    severity_map: dict[str, str] = {
        "csam": "CRITICAL — CSAM / Child Exploitation",
        "drugs": "HIGH — Illegal Narcotics Trafficking",
        "terrorism": "CRITICAL — Terrorism / Extremist Incitement",
        "weapons": "HIGH — Illegal Weapons Trade",
        "fraud": "MEDIUM — Financial Fraud / Scam Operation",
        "prostitution": "HIGH — Sex Trafficking / Prostitution",
        "darknet": "HIGH — Darknet Criminal Services",
    }
    category_key = cat.get("report_msg_key", "other")
    legal_block = legal_refs.get(
        category_key,
        (
            "Legal basis:\n"
            "• EU Digital Services Act (DSA) Article 16 — Notice and Takedown obligation\n"
            "• GDPR Article 17 — Right to Erasure of unlawfully processed data\n"
            "• Russian Federal Law No. 149-FZ (Information, Information Technologies "
            "and Information Protection)\n"
            "• Telegram Terms of Service — Community Guidelines"
        ),
    )
    severity_str = severity_map.get(category_key, f"{cat['severity']} — {cat['label']}")

    return (
        f"FORMAL NOTICE OF ILLEGAL CONTENT — NOTICE AND TAKEDOWN REQUEST\n"
        f"{'=' * 65}\n\n"
        f"Dear Telegram Trust & Safety Team,\n\n"
        f"This communication constitutes a formal notice under applicable law "
        f"requesting the immediate removal of illegal content hosted on the Telegram platform.\n\n"
        f"REPORTED CONTENT:\n"
        f"  Channel URL : {chan_url}\n"
        f"  Channel ID  : @{target}\n"
        f"  Category    : {cat['label']}\n"
        f"  Severity    : {severity_str}\n"
        f"  Reported at : {report_time} UTC\n"
        f"  Reference   : {ref_id}\n\n"
        f"DESCRIPTION OF VIOLATION:\n"
        f"The above Telegram channel is openly distributing illegal content falling "
        f"under the category of {cat['label']}. The content is systematic, ongoing, "
        f"and publicly discoverable via Telegram search — including by minors and "
        f"vulnerable users. Multiple independent reports have been filed via the "
        f"Telegram MTProto Report API prior to this email.\n\n"
        f"{legal_block}\n\n"
        f"REQUESTED ACTIONS:\n"
        f"  1. Immediate takedown / suspension of the reported channel\n"
        f"  2. Removal from Telegram search index\n"
        f"  3. Preservation of channel data for potential law enforcement referral\n"
        f"  4. Suspension of associated accounts and infrastructure\n\n"
        f"This notice has been filed simultaneously via:\n"
        f"  • Telegram MTProto Report API (all applicable report reasons)\n"
        f"  • This email to the Trust & Safety team\n"
        f"  • Official Telegram abuse form at https://telegram.org/support\n\n"
        f"We expect acknowledgement and action within the timeframes required by "
        f"applicable law (24 hours for critical categories; 72 hours for others).\n\n"
        f"Reference: {ref_id}\n"
    )


def _build_ncmec_email(target: str, report_time: str) -> str:
    chan_url = f"https://t.me/{target}"
    ref_id = f"NCMEC-{target.upper()}-{report_time[:10].replace('-', '')}"
    return (
        f"EMERGENCY CSAM REPORT — CYBERTIPLINE SUBMISSION\n"
        f"{'=' * 55}\n\n"
        f"Dear NCMEC CyberTipline Team,\n\n"
        f"This is an emergency report of suspected Child Sexual Abuse Material (CSAM) "
        f"actively distributed on the Telegram platform. Immediate action and "
        f"coordination with Telegram is urgently requested.\n\n"
        f"PLATFORM DETAILS:\n"
        f"  Platform     : Telegram Messenger\n"
        f"  Platform URL : https://telegram.org\n"
        f"  ESP          : Telegram FZ-LLC / Telegram Messenger LLP\n\n"
        f"REPORTED CONTENT:\n"
        f"  Channel URL  : {chan_url}\n"
        f"  Channel ID   : @{target}\n"
        f"  Category     : Child Sexual Abuse Material (CSAM)\n"
        f"  Reported at  : {report_time} UTC\n"
        f"  Reference    : {ref_id}\n\n"
        f"DESCRIPTION:\n"
        f"The above Telegram public channel appears to distribute child sexual abuse "
        f"material. The channel is indexed in Telegram's public search and is "
        f"discoverable by any user including minors without age verification.\n\n"
        f"LEGAL BASIS:\n"
        f"• US PROTECT Our Children Act — 18 U.S.C. § 2258A (mandatory ESP reporting)\n"
        f"• EU Digital Services Act (DSA) Article 16 — Notice and Takedown\n"
        f"• UNCRC Optional Protocol on the Sale of Children (Article 3)\n"
        f"• Russian Federal Law No. 149-FZ Article 15.1 (mandatory CSAM blocking)\n"
        f"• Russian Federal Law No. 436-FZ (protection of minors)\n\n"
        f"ACTIONS ALREADY TAKEN:\n"
        f"  • Reported to Telegram via MTProto API (all available report reasons)\n"
        f"  • Formal notice sent to abuse@telegram.org\n"
        f"  • Formal notice sent to dpo@telegram.org (GDPR / data protection)\n"
        f"  • Formal notice sent to security@telegram.org\n"
        f"  • Official Telegram abuse form submitted at https://telegram.org/support\n\n"
        f"Please coordinate with Telegram (abuse@telegram.org) to ensure expedited "
        f"removal and preservation of evidence for law enforcement referral. "
        f"Telegram has obligations under 18 U.S.C. § 2258A to report CSAM to NCMEC.\n\n"
        f"Reference: {ref_id}\n"
    )


def _smtp_auth(
    srv: smtplib.SMTP | smtplib.SMTP_SSL,
    smtp_user: str,
    smtp_secret: str,
    auth_type: str,
) -> None:
    if auth_type == "oauth":
        token = base64.b64encode(
            f"user={smtp_user}\x01auth=Bearer {smtp_secret}\x01\x01".encode()
        ).decode()
        code, response = srv.docmd("AUTH", "XOAUTH2 " + token)
        if code != 235:
            raise smtplib.SMTPAuthenticationError(code, response)
        return
    srv.login(smtp_user, smtp_secret)


def _smtp_send_sync(
    smtp_host: str,
    smtp_port: int,
    smtp_user: str,
    smtp_secret: str,
    from_addr: str,
    to_addr: str,
    subject: str,
    body: str,
    auth_type: str = "password",
) -> None:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg.attach(MIMEText(body, "plain", "utf-8"))
    ctx = _ssl.create_default_context()
    if smtp_port == 465:
        with smtplib.SMTP_SSL(smtp_host, smtp_port, context=ctx, timeout=20) as srv:
            _smtp_auth(srv, smtp_user, smtp_secret, auth_type)
            srv.sendmail(from_addr, to_addr, msg.as_string())
    else:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=20) as srv:
            srv.ehlo()
            srv.starttls(context=ctx)
            srv.ehlo()
            _smtp_auth(srv, smtp_user, smtp_secret, auth_type)
            srv.sendmail(from_addr, to_addr, msg.as_string())


async def _send_email(
    smtp_host: str,
    smtp_port: int,
    smtp_user: str,
    smtp_secret: str,
    from_addr: str,
    to_addr: str,
    subject: str,
    body: str,
    auth_type: str = "password",
) -> tuple[bool, str]:
    """Async email via thread executor. Returns (ok, error)."""
    try:
        await asyncio.to_thread(
            _smtp_send_sync,
            smtp_host,
            smtp_port,
            smtp_user,
            smtp_secret,
            from_addr,
            to_addr,
            subject,
            body,
            auth_type,
        )
        log.info("mini_strike: email sent → %s", to_addr)
        return True, ""
    except Exception as e:
        log.warning("mini_strike: email failed → %s: %s", to_addr, e)
        return False, str(e)[:120]


def _build_dmca_gdpr_email(
    target: str, cat: dict, report_time: str, email_type: str
) -> str:
    """Строит письмо для dpo@telegram.org (GDPR) или dmca@telegram.org или security@telegram.org."""
    chan_url = f"https://t.me/{target}"
    ref_id = (
        f"{email_type.upper()}-{target.upper()}-{report_time[:10].replace('-', '')}"
    )

    if email_type == "dmca":
        subject_prefix = "DMCA Takedown Notice"
        addressee = "Telegram DMCA Agent"
        body_intro = (
            "This letter constitutes a formal DMCA takedown notice pursuant to "
            "17 U.S.C. § 512(c)(3) and the EU Copyright Directive (Directive 2019/790). "
            "The reported channel is engaged in the illegal distribution of content "
            "that violates applicable law and Telegram's Terms of Service."
        )
        legal_block = (
            "LEGAL BASIS:\n"
            "• Digital Millennium Copyright Act (DMCA) — 17 U.S.C. § 512\n"
            "• EU Directive 2019/790 on Copyright in the Digital Single Market\n"
            "• EU Digital Services Act (DSA) Article 16 — Notice and Takedown\n"
            "• Telegram Terms of Service — §4 Rights"
        )
    elif email_type == "dpo":
        subject_prefix = "GDPR Article 17 — Right to Erasure / Data Protection Notice"
        addressee = "Telegram Data Protection Officer"
        body_intro = (
            "This letter constitutes a formal data protection notice pursuant to "
            "GDPR Article 17 (Right to Erasure) and GDPR Article 79 (judicial remedy). "
            "The reported channel is unlawfully processing and distributing personal data "
            "and content, causing ongoing harm to data subjects and society."
        )
        legal_block = (
            "LEGAL BASIS:\n"
            "• GDPR Article 17 — Right to Erasure ('Right to be Forgotten')\n"
            "• GDPR Article 5(1)(f) — Integrity and Confidentiality principle\n"
            "• GDPR Article 83 — Administrative fines for non-compliance\n"
            "• Russian Federal Law No. 152-FZ on Personal Data — Articles 19, 21\n"
            "• EU Digital Services Act (DSA) Article 16 — Notice and Takedown obligation"
        )
    else:  # security
        subject_prefix = "Security Incident Report — Illegal Content Distribution"
        addressee = "Telegram Security Team"
        body_intro = (
            "This letter constitutes a formal security incident report. "
            "The reported Telegram channel is actively involved in illegal activity "
            "that poses a direct security threat to platform users and the public. "
            "Immediate security review and remediation is requested."
        )
        legal_block = (
            "LEGAL BASIS:\n"
            "• EU Digital Services Act (DSA) Article 16 — Notice and Takedown\n"
            "• EU Network and Information Security (NIS2) Directive — Article 21\n"
            "• Russian Federal Law No. 149-FZ on Information Technologies — Article 16\n"
            "• Telegram Terms of Service — §10 Security"
        )

    return (
        f"{subject_prefix.upper()}\n"
        f"{'=' * 65}\n\n"
        f"Dear {addressee},\n\n"
        f"{body_intro}\n\n"
        f"REPORTED CONTENT:\n"
        f"  Channel URL : {chan_url}\n"
        f"  Channel ID  : @{target}\n"
        f"  Category    : {cat['label']}\n"
        f"  Severity    : {cat['severity']}\n"
        f"  Reported at : {report_time} UTC\n"
        f"  Reference   : {ref_id}\n\n"
        f"DESCRIPTION:\n"
        f"The above Telegram channel is openly distributing illegal content "
        f"of category '{cat['label']}'. The content is systematic, ongoing, "
        f"and publicly accessible without age verification or content moderation.\n\n"
        f"{legal_block}\n\n"
        f"REQUESTED ACTIONS:\n"
        f"  1. Immediate suspension/takedown of the reported channel\n"
        f"  2. Preservation of data for law enforcement\n"
        f"  3. Review of associated accounts and infrastructure\n"
        f"  4. Confirmation of action taken (required by DSA Article 16(5))\n\n"
        f"This notice has been filed simultaneously via:\n"
        f"  • Telegram MTProto API (all applicable report reasons)\n"
        f"  • abuse@telegram.org\n"
        f"  • dpo@telegram.org\n"
        f"  • dmca@telegram.org\n"
        f"  • security@telegram.org\n"
        f"  • https://telegram.org/support (official abuse form)\n\n"
        f"Reference: {ref_id}\n"
    )


# Целевые email-адреса для репортов (отправляем на ВСЕ при каждом страйке)
_STRIKE_EMAIL_TARGETS: list[dict] = [
    {
        "addr": "abuse@telegram.org",
        "label": "abuse",
        "description": "Trust & Safety / General Abuse",
        "email_type": "abuse",
    },
    {
        "addr": "dmca@telegram.org",
        "label": "dmca",
        "description": "DMCA / Copyright & Illegal Content",
        "email_type": "dmca",
    },
    {
        "addr": "dpo@telegram.org",
        "label": "dpo",
        "description": "DPO / GDPR / Privacy Violations",
        "email_type": "dpo",
    },
    {
        "addr": "security@telegram.org",
        "label": "security",
        "description": "Security Team / Threat Reports",
        "email_type": "security",
    },
]


async def execute_mini_strike(
    pool,
    session_str: str,
    acc: dict,
    target: str,
    category: str,
    owner_id: int,
    progress_cb=None,
) -> dict:
    """
    Одиночный удар с одного аккаунта.

    Phase 1 — Telethon report_peer_deep_v2 (12 векторов, все причины по кругу)
    Phase 2 — Email abuse@telegram.org + dmca@telegram.org + dpo@telegram.org + security@telegram.org
    Phase 3 — Email NCMEC (только category=csam)
    Phase 4 — Abuse-форма telegram.org/support
    Phase 5 — Сохранение в strike_reports

    progress_cb(text) — если передан, вызывается на каждой фазе.
    """
    import json
    from datetime import datetime, timezone
    from services import account_manager

    cat = MINI_CATEGORIES.get(category, MINI_CATEGORIES["fraud"])
    target_clean = (
        target.strip()
        .lstrip("@")
        .replace("https://t.me/", "")
        .replace("http://t.me/", "")
        .split("?")[0]
        .split("/")[0]
        .strip()
    )
    report_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    result: dict = {
        "target": target_clean,
        "category": category,
        "category_label": cat["label"],
        "severity": cat["severity"],
        "tg": {},
        "emails": [],
        "abuse_form": {},
        "total_tg_reports": 0,
        "total_emails": 0,
        "errors": [],
    }

    async def _prog(text: str) -> None:
        if progress_cb:
            try:
                await progress_cb(text)
            except Exception:
                log_exc_swallow(log, "mini_strike: progress_cb failed")

    # ── Phase 1: Telethon 12-vector ──────────────────────────────────────────
    await _prog("⚙️ <b>Фаза 1/4:</b> Telethon MTProto — 12 векторов + все причины...")
    log.info("mini_strike: phase1 telethon target=%s cat=%s", target_clean, category)
    try:
        msg_texts = assign_texts(cat["report_msg_key"], 20)
        tg = await account_manager.report_peer_deep_v2(
            session_string=session_str,
            peer_username=target_clean,
            reason=cat["tg_reason"],
            message=msg_texts[0],
            msg_messages=msg_texts,
            max_msg_reports=100,
            block_after=True,
            multi_reason=True,
            join_first=True,
            negative_react=True,
            report_admins=True,
            report_linked_bots=True,
            forward_to_bot=True,
            report_photo=True,
            report_pinned=True,
            report_linked_group=True,
            wave_num=0,
            _acc=acc,
        )
        result["tg"] = tg
        result["total_tg_reports"] = (
            (1 if tg.get("peer_reported") else 0)
            + tg.get("multi_reason_sent", 0)
            + (1 if tg.get("photo_reported") else 0)
            + tg.get("pinned_reported", 0)
            + tg.get("msg_reported", 0)
            + tg.get("spam_signaled", 0)
            + tg.get("reactions_sent", 0)
            + tg.get("admins_reported", 0)
            + (1 if tg.get("linked_group_reported") else 0)
            + tg.get("bots_reported", 0)
            + tg.get("forwarded", 0)
        )
        # Account hit PEER_FLOOD / ban / long flood mid-run → cool/deactivate,
        # do NOT record success (would decay risk_score on a flagged account).
        if tg.get("_fatal"):
            log.warning("mini_strike: fatal signal in result acc=%s — inactive", acc.get("id"))
            if acc.get("id"):
                try:
                    await pool.execute(
                        "UPDATE tg_accounts SET is_active=FALSE WHERE id=$1", acc["id"]
                    )
                except Exception:
                    log_exc_swallow(log, "mini_strike: deactivate on fatal failed")
            result["errors"].append("Аккаунт заблокирован/сессия отозвана — деактивирован")
        elif tg.get("_peer_flood") or tg.get("_long_flood"):
            log.warning(
                "mini_strike: PEER_FLOOD/long-flood acc=%s — 1h cooldown", acc.get("id")
            )
            if acc.get("id"):
                try:
                    from services.flood_engine import record_flood

                    await record_flood(pool, acc["id"], 3600, "mini_strike_peer_flood")
                except Exception:
                    log_exc_swallow(log, "mini_strike: record peer_flood failed")
            result["errors"].append("Аккаунт получил PEER_FLOOD — поставлен на остывание 1ч")
        else:
            # Register success so flood_engine can decay risk_score
            try:
                from services.flood_engine import record_success

                await record_success(acc["id"], "strike")
            except Exception:
                log_exc_swallow(log, "mini_strike: record_success flood_engine failed")
        log.info("mini_strike: telethon done tg_total=%d", result["total_tg_reports"])
    except Exception as e:
        err_str = str(e)
        result["errors"].append(f"Telethon: {err_str[:100]}")
        # Register FloodWait so flood_engine sets proper cooldown on the account
        if "FLOOD_WAIT" in err_str.upper() or "flood" in err_str.lower():
            import re as _re

            _m = _re.search(r"(\d+)", err_str)
            flood_wait = int(_m.group(1)) if _m else 60
            try:
                from services.flood_engine import record_flood

                await record_flood(pool, acc["id"], flood_wait, "strike")
            except Exception:
                log_exc_swallow(log, "mini_strike: record_flood flood_engine failed")
        # Fatal account errors — mark is_active=False immediately
        _FATAL = (
            "USER_DEACTIVATED_BAN",
            "USER_DEACTIVATED",
            "AUTH_KEY_UNREGISTERED",
            "SESSION_REVOKED",
        )
        if any(p in err_str.upper() for p in _FATAL):
            log.warning(
                "mini_strike: fatal account error for acc=%s, marking inactive",
                acc.get("id"),
            )
            if acc.get("id"):
                try:
                    await pool.execute(
                        "UPDATE tg_accounts SET is_active=FALSE WHERE id=$1",
                        acc["id"],
                    )
                except Exception:
                    log_exc_swallow(log, "mini_strike: mark_inactive failed")
        # PeerFlood — apply 1h cooldown via flood_engine
        elif "PEER_FLOOD" in err_str.upper():
            log.warning(
                "mini_strike: PeerFlood for acc=%s, applying 1h cooldown", acc.get("id")
            )
            if acc.get("id"):
                try:
                    from services.flood_engine import record_flood

                    await record_flood(pool, acc["id"], 3600, "mini_strike_peer_flood")
                except Exception:
                    log_exc_swallow(log, "mini_strike: record_peer_flood failed")
        log.exception("mini_strike: telethon failed target=%s", target_clean)

    # ── Phase 2+3: Email из всех настроенных ящиков → все 4 адреса Telegram ──
    await _prog(
        f"📧 <b>Фаза 2/4:</b> Email → abuse / dmca / dpo / security @telegram.org...\n"
        f"   Telethon: <b>{result['total_tg_reports']}</b> репортов отправлено"
    )

    # Загружаем email-аккаунты из БД (добавляются через Settings → Email аккаунты)
    db_emails: list[dict] = []
    try:
        rows = await pool.fetch(
            """SELECT id, email, smtp_host, smtp_port, smtp_pass,
                      COALESCE(auth_type, 'password') AS auth_type,
                      oauth_provider, oauth_refresh_token, oauth_access_token,
                      oauth_expires_at
               FROM strike_email_accounts
               WHERE owner_id=$1 AND is_active=TRUE
               ORDER BY last_used_at ASC NULLS FIRST""",
            owner_id,
        )
        db_emails = [dict(r) for r in rows]
    except Exception as e:
        log.debug("mini_strike: email accounts fetch skipped: %s", e)

    # Сохраняем информацию о использованных ящиках для отчёта
    result["email_accounts_used"] = [ea["email"] for ea in db_emails]

    if db_emails:
        # Отправляем на все целевые адреса Telegram с каждого ящика
        for ea in db_emails:
            any_ok = False
            for tgt in _STRIKE_EMAIL_TARGETS:
                # Строим тело письма по типу получателя
                if tgt["email_type"] == "abuse":
                    body = _build_abuse_tg_email(target_clean, cat, report_time)
                else:
                    body = _build_dmca_gdpr_email(
                        target_clean, cat, report_time, tgt["email_type"]
                    )

                subject = (
                    f"{cat['email_subject']} — @{target_clean} [{tgt['label'].upper()}]"
                )
                smtp_secret, auth_type = await _email_secret_for_account(pool, ea)
                ok, err = await _send_email(
                    ea["smtp_host"],
                    ea["smtp_port"],
                    ea["email"],
                    smtp_secret,
                    ea["email"],
                    tgt["addr"],
                    subject,
                    body,
                    auth_type=auth_type,
                )
                result["emails"].append(
                    {
                        "from": ea["email"],
                        "to": tgt["addr"],
                        "label": tgt["label"],
                        "description": tgt["description"],
                        "ok": ok,
                        "err": err,
                    }
                )
                if ok:
                    result["total_emails"] += 1
                    any_ok = True
                    log.info(
                        "mini_strike: email sent %s → %s", ea["email"], tgt["addr"]
                    )
                else:
                    log.warning(
                        "mini_strike: email failed %s → %s: %s",
                        ea["email"],
                        tgt["addr"],
                        err,
                    )

            # Обновляем статус ящика по итогу отправки хотя бы одного письма
            if any_ok:
                try:
                    await pool.execute(
                        "UPDATE strike_email_accounts SET last_used_at=now(), fail_count=0 WHERE id=$1",
                        ea["id"],
                    )
                except Exception:
                    log_exc_swallow(
                        log,
                        f"mini_strike: failed to update email account success status id={ea.get('id')}",
                    )
            else:
                try:
                    await pool.execute(
                        """UPDATE strike_email_accounts
                           SET fail_count = fail_count + 1,
                               is_active = CASE WHEN fail_count + 1 >= 3 THEN FALSE ELSE is_active END
                           WHERE id=$1""",
                        ea["id"],
                    )
                except Exception:
                    log_exc_swallow(
                        log,
                        f"mini_strike: failed to update email account fail_count id={ea.get('id')}",
                    )

        # Phase 3: NCMEC (только CSAM, отправляем с первого рабочего ящика)
        if cat.get("ncmec"):
            await _prog(
                "📧 <b>Фаза 3/4:</b> NCMEC CyberTipline — экстренный CSAM-репорт..."
            )
            ncmec_addr = "cybertipline@ncmec.org"
            body_ncmec = _build_ncmec_email(target_clean, report_time)
            for ea in db_emails:
                smtp_secret, auth_type = await _email_secret_for_account(pool, ea)
                ok_n, err_n = await _send_email(
                    ea["smtp_host"],
                    ea["smtp_port"],
                    ea["email"],
                    smtp_secret,
                    ea["email"],
                    ncmec_addr,
                    f"URGENT CyberTip: CSAM on Telegram — t.me/{target_clean}",
                    body_ncmec,
                    auth_type=auth_type,
                )
                result["emails"].append(
                    {
                        "from": ea["email"],
                        "to": ncmec_addr,
                        "label": "ncmec",
                        "description": "NCMEC CyberTipline",
                        "ok": ok_n,
                        "err": err_n,
                    }
                )
                if ok_n:
                    result["total_emails"] += 1
                    break  # Достаточно одного NCMEC-репорта
    else:
        result["emails"].append(
            {
                "to": "abuse@telegram.org",
                "label": "abuse",
                "ok": False,
                "err": "Email не настроены. Добавьте в Strike → ⚙️ Настройки → 📧 Email аккаунты",
            }
        )

    # ── Phase 4: Abuse form telegram.org/support ──────────────────────────────
    await _prog("🌐 <b>Фаза 4/4:</b> Форма telegram.org/support...")
    try:
        abuse_res = await submit_abuse_form(
            target_clean,
            cat["tg_reason"],
            title="",
            members=0,
        )
        result["abuse_form"] = abuse_res
    except Exception as e:
        result["abuse_form"] = {"ok": False, "error": str(e)[:80]}

    # ── Phase 5: Save to DB ───────────────────────────────────────────────────
    tg_data = result.get("tg", {})
    try:
        await pool.execute(
            """INSERT INTO strike_reports
               (owner_id, target, category, tg_reports_sent, emails_sent,
                total_reports, details)
               VALUES ($1, $2, $3, $4, $5, $6, $7)""",
            owner_id,
            target_clean,
            category,
            result["total_tg_reports"],
            result["total_emails"],
            result["total_tg_reports"] + result["total_emails"],
            json.dumps(
                {
                    "tg": result["tg"],
                    "emails": result["emails"],
                    "abuse_form": result["abuse_form"],
                    "errors": result["errors"],
                },
                ensure_ascii=False,
            ),
        )
    except Exception as e:
        log.debug("mini_strike: DB save strike_reports skipped: %s", e)

    # Также пишем в strike_history чтобы история в UI отображала mini-strike результаты
    try:
        await pool.execute(
            """INSERT INTO strike_history
               (owner_id, target, reason, preset, accounts_used,
                peer_reported, msgs_reported, msgs_fetched,
                pinned_reported, admins_reported, network_nodes, network_reports,
                blocked, verified_down, duration_s, abuse_form_ok, spambot_escalation)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17)""",
            owner_id,
            target_clean,
            category,  # reason = category label
            None,  # preset (mini-strike не использует)
            1,  # accounts_used
            1 if tg_data.get("peer_reported") else 0,
            int(tg_data.get("msg_reported") or 0),
            0,  # msgs_fetched
            int(tg_data.get("pinned_reported") or 0),
            int(tg_data.get("admins_reported") or 0),
            0,  # network_nodes
            0,  # network_reports
            1 if tg_data.get("blocked") else 0,
            None,  # verified_down
            0.0,  # duration_s (not tracked per mini-strike)
            bool(result.get("abuse_form", {}).get("ok")),
            "skipped",
        )
    except Exception as e:
        log.debug("mini_strike: DB save strike_history skipped: %s", e)

    log.info(
        "mini_strike: DONE target=%s tg=%d emails=%d form=%s errors=%d",
        target_clean,
        result["total_tg_reports"],
        result["total_emails"],
        result["abuse_form"].get("ok"),
        len(result["errors"]),
    )
    return result


def format_mini_result(r: dict) -> str:
    """Форматировать финальный отчёт мини-страйка для Telegram HTML."""
    import html as _html

    tg = r.get("tg", {})
    emails = r.get("emails", [])
    af = r.get("abuse_form", {})
    email_accounts_used: list[str] = r.get("email_accounts_used", [])

    # Подсчёт успешных MTProto-векторов
    vectors_ok = sum(
        [
            bool(tg.get("peer_reported")),
            bool(tg.get("photo_reported")),
            bool(tg.get("joined")),
            tg.get("pinned_reported", 0) > 0,
            tg.get("msg_reported", 0) > 0,
            tg.get("spam_signaled", 0) > 0,
            tg.get("reactions_sent", 0) > 0,
            tg.get("admins_reported", 0) > 0,
            bool(tg.get("linked_group_reported")),
            tg.get("bots_reported", 0) > 0,
            tg.get("forwarded", 0) > 0,
            bool(tg.get("blocked")),
        ]
    )
    vectors_total = 12

    # Подсчёт email по получателям
    email_ok_count = sum(1 for e in emails if e.get("ok"))
    email_fail_count = sum(1 for e in emails if not e.get("ok"))
    email_total = len(emails)

    # Вердикт на основе векторов и email
    af_ok = bool(af.get("ok"))
    tg_ok = bool(tg.get("peer_reported"))
    if vectors_ok >= 8 and email_ok_count > 0:
        header_emoji = "✅"
        verdict = "Удар нанесён эффективно"
        verdict_detail = "MTProto + Email отправлены успешно"
    elif vectors_ok >= 4 or (tg_ok and email_ok_count > 0):
        header_emoji = "🟡"
        verdict = "Частичный успех"
        parts = []
        if vectors_ok >= 4:
            parts.append(f"{vectors_ok}/{vectors_total} MTProto-векторов")
        if email_ok_count > 0:
            parts.append(f"{email_ok_count} email отправлено")
        verdict_detail = " · ".join(parts) if parts else "часть операций выполнена"
    elif tg_ok or email_ok_count > 0:
        header_emoji = "🟡"
        verdict = "Минимальный результат"
        verdict_detail = "только базовые операции выполнены"
    else:
        header_emoji = "🔴"
        verdict = "Операция не выполнена"
        verdict_detail = "цель недоступна или все каналы заблокированы"

    def _vb(flag: bool, label: str) -> str:
        return f"  {'✅' if flag else '—'} {label}"

    def _vn(n: int, label: str, unit: str = " шт.") -> str:
        return (
            f"  {'✅' if n > 0 else '—'} {label}{': ' + str(n) + unit if n > 0 else ''}"
        )

    lines = [
        f"{header_emoji} <b>Мини-страйк завершён — {_html.escape(_telegram_target_display(str(r['target'])))}</b>",
        f"<b>{verdict}</b>  <i>{verdict_detail}</i>",
        f"Категория: {_html.escape(r.get('category_label', ''))} · Уровень: <b>{r.get('severity', '?')}</b>",
        f"MTProto-векторов: <b>{vectors_ok}/{vectors_total}</b>",
        "",
        "<b>📡 Telegram MTProto:</b>",
        _vb(
            bool(tg.get("peer_reported")),
            f"ReportPeer + {tg.get('multi_reason_sent', 0)} доп. причин",
        ),
        _vb(bool(tg.get("photo_reported")), "Жалоба на фото профиля"),
        _vb(bool(tg.get("joined")), "Вступление в канал изнутри"),
        _vn(tg.get("pinned_reported", 0), "Закреплённые посты"),
        _vn(tg.get("msg_reported", 0), "Сообщения"),
        _vn(tg.get("spam_signaled", 0), "ReportSpam сигналы"),
        _vn(tg.get("reactions_sent", 0), "Реакции 👎💩"),
        _vn(tg.get("admins_reported", 0), "Администраторы"),
        _vb(bool(tg.get("linked_group_reported")), "Связанная группа"),
        _vn(tg.get("bots_reported", 0), "Боты в описании"),
        _vn(tg.get("forwarded", 0), "Пересылка в @SpamBot/@stopCA"),
        _vb(bool(tg.get("blocked")), "Заблокирован + выход"),
        "",
        "<b>📧 Email-репорты:</b>",
    ]

    if email_accounts_used:
        accs_escaped = ", ".join(_html.escape(a) for a in email_accounts_used)
        lines.append(f"  Ящики: <code>{accs_escaped}</code>")
        lines.append("")

    if emails:
        # Группируем по получателю (addr) для компактного отображения
        by_target: dict[str, list[dict]] = {}
        for e in emails:
            to = e.get("to", "?")
            by_target.setdefault(to, []).append(e)

        for to_addr, sends in by_target.items():
            ok_sends = [s for s in sends if s.get("ok")]
            fail_sends = [s for s in sends if not s.get("ok")]
            sends[0].get("label", "")
            desc = sends[0].get("description", to_addr)

            if ok_sends:
                from_list = ", ".join(
                    _html.escape(s.get("from", "?")) for s in ok_sends
                )
                lines.append(
                    f"  ✅ <b>{_html.escape(to_addr)}</b> — {_html.escape(desc)}"
                )
                lines.append(f"      отправлено с: {from_list}")
            else:
                lines.append(
                    f"  ❌ <b>{_html.escape(to_addr)}</b> — {_html.escape(desc)}"
                )
                if fail_sends and fail_sends[0].get("err"):
                    err_short = _html.escape((fail_sends[0]["err"])[:80])
                    lines.append(f"      ошибка: {err_short}")
    elif not email_accounts_used:
        lines.append("  — SMTP не настроен")
        lines.append("  <i>💡 Добавьте email в Настройки → 📧 Email аккаунты</i>")

    # Email-итог
    if email_total > 0:
        email_summary = f"<b>{email_ok_count}</b> отправлено"
        if email_fail_count:
            email_summary += f", <b>{email_fail_count}</b> ошибок"
        email_summary += f" (из {email_total} попыток)"
    else:
        email_summary = "— не настроены"

    lines += [
        "",
        f"<b>🌐 Форма telegram.org/support:</b> {'✅ отправлена' if af_ok else '— не удалось'}",
        "",
        "─────────────────────────────",
    ]

    # Итоговый вердикт блок
    if header_emoji == "✅":
        verdict_icon = "🟢 УСПЕХ"
    elif header_emoji == "🟡":
        verdict_icon = "🟡 ЧАСТИЧНО"
    else:
        verdict_icon = "🔴 НЕУДАЧА"

    lines += [
        f"📊 <b>Итог: {verdict_icon}</b>",
        f"   MTProto: <b>{r['total_tg_reports']}</b> репортов  ·  "
        f"Email: {email_summary}",
        f"   Форма: {'✅' if af_ok else '❌'}  ·  "
        f"NCMEC: {'✅' if any(e.get('to', '').find('ncmec') != -1 and e.get('ok') for e in emails) else '—'}",
    ]

    if r.get("errors"):
        errs = "; ".join(r["errors"][:3])
        lines.append(f"\n⚠️ Ошибки: <code>{_html.escape(errs[:200])}</code>")

    return "\n".join(lines)
