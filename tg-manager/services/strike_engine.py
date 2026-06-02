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
_CONCURRENCY = 6          # макс параллельных аккаунтов в одной волне
_WAVE_COOLDOWN = (8, 25)  # секунд между волнами атаки (рандомный диапазон)
_VERIFY_WAIT = (30, 120)  # секунд ожидания перед верификацией
_MAX_RETRIES = 2          # макс ретраев при FloodWait на аккаунт
_FLOOD_CAP = 65.0         # кап exponential backoff


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
    "drugs":     "other",
    "terrorism": "violence",
    "fraud":     "spam",
    "csam":      "childabuse",
    "weapons":   "violence",
    "darknet":   "other",
    "escort":    "pornography",
}
PRESET_TEXTS = {
    "drugs":     TEXTS["drugs"],
    "terrorism": TEXTS["terrorism"],
    "csam":      TEXTS["childabuse"],
    "fraud":     TEXTS["fraud"],
    "weapons":   TEXTS["weapons"],
    "darknet":   TEXTS["darknet"],
    "escort":    TEXTS["escort"],
}

# Приоритетные причины для эскалации (наиболее эффективные)
# CSAM/drugs — мгновенная реакция, violence — быстрая, spam — медленная
_REASON_PRIORITY = {
    "childabuse": 0,   # highest priority
    "csam": 0,
    "terrorism": 1,
    "drugs": 2,
    "weapons": 3,
    "fraud": 4,
    "pornography": 5,
    "escort":   5,
    "spam": 6,
    "other": 7,
}


def assign_texts(reason_or_preset: str, count: int) -> list[str]:
    """Выдаёт `count` уникальных текстов — каждому аккаунту свой."""
    pool = PRESET_TEXTS.get(reason_or_preset) or TEXTS.get(reason_or_preset) or TEXTS["other"]
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
    targets: list[str]                          # @usernames целей
    accounts: list[dict]                        # отсортированные аккаунты
    reason: str                                 # базовая причина
    preset: str | None                          # пресет (drugs, fraud, etc.)
    label: str                                  # человекочитаемая метка
    intel: dict[str, dict] = field(default_factory=dict)  # peer → intel
    waves: list[list[dict]] = field(default_factory=list)  # аккаунты по волнам
    started_at: float = 0.0
    phase: str = "init"
    mode: str = "normal"   # fast | normal | maximum


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
    verified_down: bool | None = None   # None = не проверено
    spambot_escalation: str = "skipped"   # sent | skipped | error
    errors: list[str] = field(default_factory=list)
    duration_s: float = 0.0
    phase_results: dict[str, Any] = field(default_factory=dict)


# ── Pre-flight ─────────────────────────────────────────────────────────────────


def preflight_accounts(accounts: list[dict], min_trust: float = 0.0) -> list[dict]:
    """
    Предполётная проверка аккаунтов:
      - Отсев аккаунтов в кулдауне (cooldown_until > now)
      - Отсев неактивных (is_active = False)
      - Сортировка по trust_score (высокие первыми — лучшие аккаунты в первой волне)
      - Отсев с trust_score ниже min_trust

    Возвращает отсортированный список годных аккаунтов.
    """
    now = time.time()
    viable: list[dict] = []

    for acc in accounts:
        if not acc.get("is_active", False):
            continue
        # Проверка кулдауна
        cooldown = acc.get("cooldown_until")
        if cooldown:
            try:
                cd_ts = cooldown.timestamp() if hasattr(cooldown, "timestamp") else float(cooldown)
                if cd_ts > now:
                    continue  # аккаунт в кулдауне — пропускаем
            except (TypeError, ValueError, OSError):
                log_exc_swallow(log, "Не удалось распарсить cooldown_until аккаунта в preflight")
        # Проверка trust_score
        ts = acc.get("trust_score") or 0
        if ts < min_trust:
            continue
        viable.append(acc)

    # Сортировка: trust_score DESC, flood_count_7d ASC
    viable.sort(
        key=lambda a: (
            -(a.get("trust_score") or 0),
            a.get("flood_count_7d") or 0,
        )
    )
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

    # Staggered start внутри волны — каждый следующий аккаунт ждёт чуть дольше
    stagger_delay = random.uniform(1.5, 5.0) * (acc_index + 1)
    await asyncio.sleep(stagger_delay)

    text = texts[acc_index % len(texts)]
    msg_texts = assign_texts(preset or reason, 15)

    if mode == "fast":
        # fast: нет join, нет доп. векторов — только быстрые жалобы
        kwargs: dict = dict(
            join_first=False, negative_react=False, report_admins=False,
            report_linked_group=False, report_linked_bots=False,
            forward_to_bot=False, block_after=False, multi_reason=True,
            max_msg_reports=30, report_photo=True, report_pinned=True,
        )
    elif mode == "maximum":
        # maximum: join во всех волнах — нужен для msg reporting
        kwargs = dict(
            join_first=True, negative_react=True, report_admins=True,
            report_linked_group=(wave_num == 0), report_linked_bots=True,
            forward_to_bot=(wave_num <= 1), block_after=(wave_num >= 2),
            multi_reason=True, max_msg_reports=100, report_photo=True, report_pinned=True,
        )
    else:  # normal
        # normal: join во всех волнах — без join MsgReportRequest не работает
        kwargs = dict(
            join_first=True, negative_react=(wave_num <= 1),
            report_admins=(wave_num <= 1), report_linked_group=(wave_num == 0),
            report_linked_bots=True, forward_to_bot=(wave_num <= 1),
            block_after=(wave_num >= 2), multi_reason=True,
            max_msg_reports=60, report_photo=True, report_pinned=True,
        )

    async with sem:
        for attempt in range(_MAX_RETRIES + 1):
            try:
                result = await account_manager.report_peer_deep_v2(
                    acc["session_str"], peer_username, reason,
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
                return result
            except Exception as e:
                err_str = str(e)[:150]
                # Если FloodWait — записываем в flood_engine, ждём и ретраим
                if "FLOOD_WAIT" in err_str.upper() or "flood" in err_str.lower():
                    import re as _re
                    wait_match = _re.search(r'(\d+)', err_str)
                    wait_s = min(_FLOOD_CAP, float(wait_match.group(1)) if wait_match else 30.0)
                    log.warning("strike acc %s wave %d: FloodWait %ss, retry %d/%d",
                                acc.get("id"), wave_num, wait_s, attempt + 1, _MAX_RETRIES)
                    if pool is not None:
                        try:
                            from services.flood_engine import record_flood
                            await record_flood(pool, acc["id"], int(wait_s), "strike")
                        except Exception:
                            log_exc_swallow(log, f"strike: record_flood failed acc={acc.get('id')}")
                    else:
                        try:
                            from services.flood_engine import get_account_state
                            state = get_account_state(acc["id"])
                            import time as _time
                            state.consecutive_floods += 1
                            state.last_flood_at = _time.monotonic()
                            state.cooldown_until = _time.monotonic() + wait_s + 10
                            state.risk_score = min(1.0, state.risk_score + 0.15)
                        except Exception:
                            pass
                    await asyncio.sleep(wait_s + random.uniform(2, 8))
                    continue
                log.warning("strike acc %s wave %d: %s", acc.get("id"), wave_num, err_str)
                return {
                    "peer_reported": False, "error": err_str,
                    "_acc_id": acc.get("id"), "_wave": wave_num,
                }

        return {
            "peer_reported": False,
            "error": f"max retries ({_MAX_RETRIES}) exceeded",
            "_acc_id": acc.get("id"), "_wave": wave_num,
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

    for target in plan.targets:
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
            await progress_cb("strike_wave1", f"🎯 {target}: Волна 1 — {len(plan.waves[0]) if plan.waves else 0} аккаунтов")

        # ═══ Волна 1: ReportPeer + ReportSpam + Join + Internal ═══
        wave_results: list[dict] = []
        if plan.waves:
            texts_w1 = assign_texts(plan.preset or plan.reason, len(plan.waves[0]))
            tasks = [
                _one_account_strike(acc, target, intel, plan.reason, plan.preset,
                                    texts_w1, i, 0, sem, mode=plan.mode, pool=pool)
                for i, acc in enumerate(plan.waves[0])
            ]
            wave_results = _safe_gather_results(await asyncio.gather(*tasks, return_exceptions=True))

        # Пауза между волнами
        if len(plan.waves) > 1:
            wc = random.uniform(*_WAVE_COOLDOWN)
            if progress_cb:
                await progress_cb("strike_cooldown", f"⏳ Пауза {wc:.0f}с перед волной 2...")
            await asyncio.sleep(wc)

        # ═══ Волна 2: Поддержка — другие причины, админы, боты ═══
        if len(plan.waves) > 1 and plan.waves[1]:
            if progress_cb:
                await progress_cb("strike_wave2", f"🎯 {target}: Волна 2 — {len(plan.waves[1])} аккаунтов")
            texts_w2 = assign_texts(plan.preset or plan.reason, len(plan.waves[1]))
            tasks = [
                _one_account_strike(acc, target, intel, plan.reason, plan.preset,
                                    texts_w2, i, 1, sem, mode=plan.mode, pool=pool)
                for i, acc in enumerate(plan.waves[1])
            ]
            w2_results = _safe_gather_results(await asyncio.gather(*tasks, return_exceptions=True))
            wave_results.extend(w2_results)

        # Пауза перед финальной волной
        if len(plan.waves) > 2:
            wc = random.uniform(*_WAVE_COOLDOWN)
            if progress_cb:
                await progress_cb("strike_cooldown", f"⏳ Пауза {wc:.0f}с перед волной 3...")
            await asyncio.sleep(wc)

        # ═══ Волна 3: Завершение — блокировка, финальные жалобы ═══
        if len(plan.waves) > 2 and plan.waves[2]:
            if progress_cb:
                await progress_cb("strike_wave3", f"🎯 {target}: Волна 3 (финал) — {len(plan.waves[2])} аккаунтов")
            texts_w3 = assign_texts(plan.preset or plan.reason, len(plan.waves[2]))
            tasks = [
                _one_account_strike(acc, target, intel, plan.reason, plan.preset,
                                    texts_w3, i, 2, sem, mode=plan.mode, pool=pool)
                for i, acc in enumerate(plan.waves[2])
            ]
            w3_results = _safe_gather_results(await asyncio.gather(*tasks, return_exceptions=True))
            wave_results.extend(w3_results)

        # Агрегация — явный маппинг ключей aggregate_results → поля StrikeResult
        agg = aggregate_results(wave_results)
        result.peer_reported          += agg.get("peer", 0)
        result.multi_reason           += agg.get("multi", 0)
        result.photo_reported          = result.photo_reported or bool(agg.get("photo", 0))
        result.pinned_reported        += agg.get("pinned", 0)
        result.msgs_reported          += agg.get("msgs", 0)
        result.msgs_fetched           += agg.get("msgs_fetched", 0)
        result.spam_signaled          += agg.get("spam", 0)
        result.reactions              += agg.get("reacts", 0)
        result.admins_reported        += agg.get("admins", 0)
        result.linked_group_reported   = result.linked_group_reported or bool(agg.get("linked_grp", 0))
        result.bots_reported          += agg.get("bots", 0)
        result.forwarded              += agg.get("fwd", 0)
        result.blocked                += agg.get("blocked", 0)
        result.errors = [r.get("error") for r in wave_results if r.get("error")]
        for wave_result in wave_results:
            result.errors.extend(wave_result.get("errors", [])[:3])
            if wave_result.get("rate_limited"):
                result.errors.append(
                    f"account {wave_result.get('_acc_id', '?')}: Telegram rate limit"
                )

        # ═══ Фаза: Network nodes (параллельно) ═══
        if progress_cb:
            await progress_cb("strike_network", f"🌐 {target}: Атака сетевых узлов...")
        net = await strike_network_nodes_v2(plan.accounts, intel, plan.reason, plan.preset)
        result.network_nodes = net.get("nodes_attacked", 0)
        result.network_reports = net.get("total_reports", 0)

        # ═══ Фаза: External escalation ═══
        if progress_cb:
            await progress_cb("strike_escalation", f"📤 {target}: Внешняя эскалация...")
        abuse_res = await submit_abuse_form(target, plan.preset or plan.reason,
                                            title=intel.get("title", ""),
                                            members=intel.get("members", 0))
        result.abuse_form_ok = abuse_res.get("ok", False)

        # ═══ Фаза: SpamBot escalation ═══
        spambot_result = await _escalate_to_spambot(plan.accounts[0] if plan.accounts else None, target)
        if isinstance(spambot_result, dict):
            result.spambot_escalation = spambot_result.get("status", "unknown")
        elif spambot_result is True:
            result.spambot_escalation = "sent"
        else:
            result.spambot_escalation = "skipped"

        result.duration_s = time.time() - t_start
        all_results.append(result)

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
                acc["session_str"], node, reason,
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
            return {"node": node, "ok": res.get("peer_reported", False),
                    "reports": 1 + res.get("multi_reason_sent", 0)}
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
    """Отправляет официальную форму жалобы через telegram.org/support."""
    clean = target_username.lstrip("@")
    reason_text = {
        "drugs": "illegal drug sales and drug trafficking",
        "terrorism": "terrorism, extremism and incitement to violence",
        "childabuse": "child sexual abuse material (CSAM)",
        "csam": "child sexual abuse material (CSAM)",
        "fraud": "financial fraud and scam operation",
        "weapons": "illegal weapons trafficking and arms dealing",
        "darknet": "darknet criminal services and illegal marketplace",
        "violence": "promotion of violence and terrorism",
        "spam": "mass spam and platform abuse",
        "pornography": "illegal pornographic content and adult material without age verification",
        "escort": "prostitution, illegal escort services and sexual exploitation",
        "other": "Terms of Service violation and harmful illegal content",
    }.get(reason, "Terms of Service violation and harmful illegal content")

    body = (
        f"I am reporting a Telegram channel for {reason_text}.\n\n"
        f"Channel: @{clean}\n"
        f"Title: {title}\n"
        f"Subscribers: {members}\n\n"
        f"This channel actively violates Telegram's Terms of Service by openly distributing "
        f"illegal content related to {reason_text}. The content is systematic, ongoing and "
        f"openly visible. Multiple independent users have reported this channel. "
        f"Immediate removal is urgently required."
    )
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=25),
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
        ) as sess:
            async with sess.post(
                "https://telegram.org/support",
                data={"message": body, "technical_info": f"@{clean}", "question_type": "report"},
                allow_redirects=True,
            ) as resp:
                return {"ok": resp.status in (200, 201, 302), "status": resp.status}
    except Exception as e:
        log.warning("submit_abuse_form: %s", e)
        return {"ok": False, "error": str(e)[:100]}


async def _escalate_to_spambot(acc: dict | None, target_username: str) -> bool:
    """
    Отправляет жалобу через @SpamBot — официальный механизм Telegram.
    SpamBot принимает пересланные сообщения и реагирует на /report.
    """
    if not acc:
        return False
    from services import account_manager
    try:
        # Пересылаем сообщение от цели в @SpamBot если есть доступ
        clean = target_username.lstrip("@")
        client = account_manager._make_client(acc["session_str"], acc)
        try:
            await asyncio.wait_for(client.connect(), timeout=15)
            # Пробуем получить и переслать одно сообщение в SpamBot
            msgs = await client.get_messages(clean, limit=1)
            if msgs:
                spam_bot = await client.get_entity("SpamBot")
                await client.forward_messages(spam_bot, msgs[0])
            await client.disconnect()
            return True
        except Exception:
            log_exc_swallow(log, "Сбой операции в _escalate_to_spambot")
            try:
                await client.disconnect()
            except Exception:
                log_exc_swallow(log, "Сбой disconnect клиента в _escalate_to_spambot")
            return False
    except Exception as e:
        log.warning("_escalate_to_spambot: %s", e)
        return False


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
            from telethon.tl.types import Channel
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
            # Эти ошибки означают что канал удалён/недоступен
            if any(kw in err_str for kw in (
                "not found", "no user", "channel private", "banned",
                "deactivated", "invalid", "username not found",
                "cannot get entity", "peer not found",
            )):
                return True
            # Другие ошибки — возможно временные, пробуем ещё
            log.info("verify_takedown attempt %d/%d for %s: %s",
                     attempt + 1, max_attempts, clean, err_str[:80])

    return False  # цель всё ещё жива после всех попыток


# ── Aggregate helper ───────────────────────────────────────────────────────────


def aggregate_results(results: list[dict]) -> dict:
    """Суммирует результаты атаки."""
    s = {
        "peer": 0, "multi": 0, "photo": 0, "pinned": 0,
        "msgs": 0, "msgs_fetched": 0, "spam": 0, "reacts": 0, "admins": 0,
        "linked_grp": 0, "bots": 0, "fwd": 0, "blocked": 0, "failed": 0,
    }
    for r in results:
        if not r:
            s["failed"] += 1
            continue
        if r.get("peer_reported"):
            s["peer"] += 1
        else:
            s["failed"] += 1
        s["multi"]     += r.get("multi_reason_sent", 0)
        s["photo"]     += 1 if r.get("photo_reported") else 0
        s["pinned"]    += r.get("pinned_reported", 0)
        s["msgs"]      += r.get("msg_reported", 0)
        s["msgs_fetched"] += r.get("msgs_fetched", 0)
        s["spam"]      += r.get("spam_signaled", 0)
        s["reacts"]    += r.get("reactions_sent", 0)
        s["admins"]    += r.get("admins_reported", 0)
        s["linked_grp"] += 1 if r.get("linked_group_reported") else 0
        s["bots"]      += r.get("bots_reported", 0)
        s["fwd"]       += r.get("forwarded", 0)
        s["blocked"]   += 1 if r.get("blocked") else 0
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
        elif r.peer_reported > 0 and (r.msgs_reported > 0 or r.pinned_reported > 0 or r.msgs_fetched > 0):
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
            lines.append(f"  🔍 Проверка: {'✅ УДАЛЁН' if r.verified_down else '⚠️ Всё ещё активен'}")
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
                    acc["session_str"], peer_username, reason,
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
        await asyncio.gather(*[_one(acc, txt) for acc, txt in zip(accounts, texts)], return_exceptions=True)
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
    texts_sample = TEXTS.get(cat["report_msg_key"], TEXTS["other"])
    description = texts_sample[0] if texts_sample else "Illegal content."
    return (
        f"Dear Telegram Trust & Safety Team,\n\n"
        f"I am reporting the following channel for illegal content requiring immediate action.\n\n"
        f"Channel: {chan_url}\n"
        f"Category: {cat['label']}\n"
        f"Severity: {cat['severity']}\n"
        f"Date/Time: {report_time} UTC\n\n"
        f"{description}\n\n"
        f"This channel is publicly accessible via Telegram search and may be discovered "
        f"by minors and vulnerable users.\n\n"
        f"Requested action: immediate review, removal from search, account suspension if confirmed.\n\n"
        f"This report was filed simultaneously via Telegram MTProto Report API and this email.\n"
        f"Reference: MINI-{target.upper()}-{report_time[:10].replace('-', '')}\n"
    )


def _build_ncmec_email(target: str, report_time: str) -> str:
    chan_url = f"https://t.me/{target}"
    return (
        f"Dear NCMEC CyberTipline Team,\n\n"
        f"I am filing an emergency report of suspected Child Sexual Abuse Material (CSAM) "
        f"on the Telegram platform.\n\n"
        f"Platform: Telegram Messenger (telegram.org)\n"
        f"Channel URL: {chan_url}\n"
        f"Date/Time: {report_time} UTC\n\n"
        f"The above Telegram public channel appears to distribute child sexual abuse material. "
        f"The channel is indexed in Telegram's public search and discoverable by any user "
        f"including minors.\n\n"
        f"Action taken:\n"
        f"• Reported to Telegram via MTProto API (all available report reasons)\n"
        f"• Reported to Telegram via abuse@telegram.org\n\n"
        f"Please coordinate with Telegram to ensure expedited removal and preservation of "
        f"evidence for law enforcement.\n\n"
        f"Reference: NCMEC-{target.upper()}-{report_time[:10].replace('-', '')}\n"
    )


def _smtp_send_sync(
    smtp_host: str, smtp_port: int, smtp_user: str, smtp_pass: str,
    from_addr: str, to_addr: str, subject: str, body: str,
) -> None:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg.attach(MIMEText(body, "plain", "utf-8"))
    ctx = _ssl.create_default_context()
    if smtp_port == 465:
        with smtplib.SMTP_SSL(smtp_host, smtp_port, context=ctx, timeout=20) as srv:
            srv.login(smtp_user, smtp_pass)
            srv.sendmail(from_addr, to_addr, msg.as_string())
    else:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=20) as srv:
            srv.ehlo()
            srv.starttls(context=ctx)
            srv.login(smtp_user, smtp_pass)
            srv.sendmail(from_addr, to_addr, msg.as_string())


async def _send_email(
    smtp_host: str, smtp_port: int, smtp_user: str, smtp_pass: str,
    from_addr: str, to_addr: str, subject: str, body: str,
) -> tuple[bool, str]:
    """Async email via thread executor. Returns (ok, error)."""
    try:
        await asyncio.to_thread(
            _smtp_send_sync,
            smtp_host, smtp_port, smtp_user, smtp_pass,
            from_addr, to_addr, subject, body,
        )
        log.info("mini_strike: email sent → %s", to_addr)
        return True, ""
    except Exception as e:
        log.warning("mini_strike: email failed → %s: %s", to_addr, e)
        return False, str(e)[:120]


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
    Phase 2 — Email abuse@telegram.org
    Phase 3 — Email NCMEC (только category=csam)
    Phase 4 — Abuse-форма telegram.org/support
    Phase 5 — Сохранение в strike_reports

    progress_cb(text) — если передан, вызывается на каждой фазе.
    """
    import json
    from datetime import datetime, timezone
    from services import account_manager
    from config import (
        SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS,
        REPORT_FROM_EMAIL, NCMEC_EMAIL,
    )

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
                pass

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
            _m = _re.search(r'(\d+)', err_str)
            flood_wait = int(_m.group(1)) if _m else 60
            try:
                from services.flood_engine import record_flood
                await record_flood(pool, acc["id"], flood_wait, "strike")
            except Exception:
                log_exc_swallow(log, "mini_strike: record_flood flood_engine failed")
        log.exception("mini_strike: telethon failed target=%s", target_clean)

    # ── Phase 2+3: Email из всех настроенных ящиков ──────────────────────────
    await _prog(
        f"📧 <b>Фаза 2/4:</b> Email → abuse@telegram.org...\n"
        f"   Telethon: <b>{result['total_tg_reports']}</b> репортов отправлено"
    )

    # Загружаем email-аккаунты из БД (добавляются через Settings → Email аккаунты)
    db_emails: list[dict] = []
    try:
        rows = await pool.fetch(
            """SELECT id, email, smtp_host, smtp_port, smtp_pass
               FROM strike_email_accounts
               WHERE owner_id=$1 AND is_active=TRUE
               ORDER BY last_used_at ASC NULLS FIRST""",
            owner_id,
        )
        db_emails = [dict(r) for r in rows]
    except Exception as e:
        log.debug("mini_strike: email accounts fetch skipped: %s", e)

    body_tg = _build_abuse_tg_email(target_clean, cat, report_time)

    if db_emails:
        for ea in db_emails:
            ok, err = await _send_email(
                ea["smtp_host"], ea["smtp_port"], ea["email"], ea["smtp_pass"],
                ea["email"], "abuse@telegram.org",
                f"{cat['email_subject']} — @{target_clean}",
                body_tg,
            )
            result["emails"].append({
                "from": ea["email"], "to": "abuse@telegram.org", "ok": ok, "err": err,
            })
            if ok:
                result["total_emails"] += 1
                try:
                    await pool.execute(
                        "UPDATE strike_email_accounts SET last_used_at=now(), fail_count=0 WHERE id=$1",
                        ea["id"],
                    )
                except Exception:
                    pass
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
                    pass

        # Phase 3: NCMEC (только CSAM, отправляем с первого рабочего ящика)
        if cat.get("ncmec"):
            await _prog("📧 <b>Фаза 3/4:</b> NCMEC CyberTipline — экстренный CSAM-репорт...")
            ncmec_addr = "cybertipline@ncmec.org"
            body_ncmec = _build_ncmec_email(target_clean, report_time)
            for ea in db_emails:
                ok_n, err_n = await _send_email(
                    ea["smtp_host"], ea["smtp_port"], ea["email"], ea["smtp_pass"],
                    ea["email"], ncmec_addr,
                    f"URGENT CyberTip: CSAM on Telegram — t.me/{target_clean}",
                    body_ncmec,
                )
                result["emails"].append({
                    "from": ea["email"], "to": ncmec_addr, "ok": ok_n, "err": err_n,
                })
                if ok_n:
                    result["total_emails"] += 1
                    break  # Достаточно одного NCMEC-репорта
    else:
        result["emails"].append({
            "to": "abuse@telegram.org", "ok": False,
            "err": "Email не настроены. Добавьте в Strike → ⚙️ Настройки → 📧 Email аккаунты",
        })

    # ── Phase 4: Abuse form telegram.org/support ──────────────────────────────
    await _prog("🌐 <b>Фаза 4/4:</b> Форма telegram.org/support...")
    try:
        from services.strike_engine import submit_abuse_form as _saf
        abuse_res = await _saf(
            target_clean,
            cat["tg_reason"],
            title="",
            members=0,
        )
        result["abuse_form"] = abuse_res
    except Exception as e:
        result["abuse_form"] = {"ok": False, "error": str(e)[:80]}

    # ── Phase 5: Save to DB ───────────────────────────────────────────────────
    try:
        await pool.execute(
            """INSERT INTO strike_reports
               (owner_id, target, category, tg_reports_sent, emails_sent,
                total_reports, details)
               VALUES ($1, $2, $3, $4, $5, $6, $7)""",
            owner_id, target_clean, category,
            result["total_tg_reports"],
            result["total_emails"],
            result["total_tg_reports"] + result["total_emails"],
            json.dumps({
                "tg": result["tg"],
                "emails": result["emails"],
                "abuse_form": result["abuse_form"],
                "errors": result["errors"],
            }),
        )
    except Exception as e:
        log.debug("mini_strike: DB save skipped: %s", e)

    log.info(
        "mini_strike: DONE target=%s tg=%d emails=%d form=%s errors=%d",
        target_clean, result["total_tg_reports"], result["total_emails"],
        result["abuse_form"].get("ok"), len(result["errors"]),
    )
    return result


def format_mini_result(r: dict) -> str:
    """Форматировать финальный отчёт мини-страйка для Telegram HTML."""
    import html as _html
    tg = r.get("tg", {})
    emails = r.get("emails", [])
    af = r.get("abuse_form", {})

    lines = [
        f"✅ <b>Мини-страйк завершён — @{_html.escape(r['target'])}</b>",
        f"Категория: {r['category_label']} · Уровень: <b>{r['severity']}</b>\n",
        "<b>📡 Telegram MTProto (12 векторов):</b>",
        f"  ① ReportPeer (все причины): {'✅' if tg.get('peer_reported') else '❌'}"
        f" +{tg.get('multi_reason_sent', 0)} доп.",
        f"  ② Фото профиля: {'✅' if tg.get('photo_reported') else '❌'}",
        f"  ③ Вступил изнутри: {'✅' if tg.get('joined') else '❌'}",
        f"  ④ Закреплённые: {tg.get('pinned_reported', 0)} репортов",
        f"  ⑤ Сообщения: {tg.get('msg_reported', 0)} репортов",
        f"  ⑥ ReportSpam: {tg.get('spam_signaled', 0)} сигналов",
        f"  ⑦ Реакции 👎💩: {tg.get('reactions_sent', 0)}",
        f"  ⑧ Администраторы: {tg.get('admins_reported', 0)} репортов",
        f"  ⑨ Связанная группа: {'✅' if tg.get('linked_group_reported') else 'нет'}",
        f"  ⑩ Боты в описании: {tg.get('bots_reported', 0)} репортов",
        f"  ⑪ Форвард в @stopCA: {tg.get('forwarded', 0)} сообщ.",
        f"  ⑫ Заблок. + выход: {'✅' if tg.get('blocked') else '—'}",
        "",
        "<b>📧 Email-репорты:</b>",
    ]
    if emails:
        for e in emails:
            status = "✅ отправлен" if e.get("ok") else f"❌ {_html.escape((e.get('err') or '')[:50])}"
            lines.append(f"  → {_html.escape(e['to'])}: {status}")
    else:
        lines.append("  SMTP не настроен")

    lines += [
        "",
        f"<b>🌐 Форма telegram.org/support:</b> {'✅' if af.get('ok') else '❌'}",
        "",
        f"<b>Итого репортов:</b> "
        f"<b>{r['total_tg_reports']}</b> (Telegram MTProto) + "
        f"<b>{r['total_emails']}</b> (email)",
    ]

    if r.get("errors"):
        errs = "; ".join(r["errors"][:3])
        lines.append(f"\n⚠️ Ошибки: <code>{_html.escape(errs[:200])}</code>")

    return "\n".join(lines)
