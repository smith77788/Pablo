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
}

# Маппинг пресетов на базовые причины
PRESET_TO_REASON = {
    "drugs":     "other",
    "terrorism": "violence",
    "fraud":     "spam",
    "csam":      "childabuse",
    "weapons":   "violence",
    "darknet":   "other",
}
PRESET_TEXTS = {
    "drugs":     TEXTS["drugs"],
    "terrorism": TEXTS["terrorism"],
    "csam":      TEXTS["childabuse"],
    "fraud":     TEXTS["fraud"],
    "weapons":   TEXTS["weapons"],
    "darknet":   TEXTS["darknet"],
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


@dataclass
class StrikeResult:
    """Результат strike-операции."""
    target: str
    total_reports: int = 0
    unique_accounts: int = 0
    peer_reported: int = 0
    multi_reason: int = 0
    msgs_reported: int = 0
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
) -> dict:
    """
    Один аккаунт выполняет полную 12-векторную атаку.
    С задержкой на основе позиции в волне (staggered start).
    С ретраями при FloodWait.
    """
    from services import account_manager

    # Staggered start внутри волны — каждый следующий аккаунт ждёт чуть дольше
    stagger_delay = random.uniform(1.5, 5.0) * (acc_index + 1)
    await asyncio.sleep(stagger_delay)

    text = texts[acc_index % len(texts)]
    msg_texts = assign_texts(preset or reason, 15)

    async with sem:
        for attempt in range(_MAX_RETRIES + 1):
            try:
                result = await account_manager.report_peer_deep_v2(
                    acc["session_str"], peer_username, reason,
                    message=text,
                    msg_messages=msg_texts,
                    max_msg_reports=60,
                    block_after=(wave_num >= 2),    # блокировать только в последней волне
                    multi_reason=True,
                    join_first=(wave_num == 0),      # входить только в первой волне
                    negative_react=(wave_num <= 1),  # реакции в первых двух волнах
                    report_admins=(wave_num <= 1),
                    report_linked_bots=True,
                    forward_to_bot=(wave_num <= 1),
                    report_photo=True,
                    report_pinned=True,
                    report_linked_group=(wave_num == 0),
                    wave_num=wave_num,
                    _acc=acc,
                )
                # Добавляем метаданные аккаунта
                result["_acc_id"] = acc.get("id")
                result["_acc_trust"] = acc.get("trust_score", 0)
                result["_wave"] = wave_num
                return result
            except Exception as e:
                err_str = str(e)[:150]
                # Если FloodWait — ждём и ретраим
                if "FLOOD_WAIT" in err_str.upper() or "flood" in err_str.lower():
                    import re as _re
                    wait_match = _re.search(r'(\d+)', err_str)
                    wait_s = min(_FLOOD_CAP, float(wait_match.group(1)) if wait_match else 30.0)
                    log.warning("strike acc %s wave %d: FloodWait %ss, retry %d/%d",
                                acc.get("id"), wave_num, wait_s, attempt + 1, _MAX_RETRIES)
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


async def staggered_strike(
    plan: StrikePlan,
    progress_cb=None,
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
        t_start = time.time()

        if progress_cb:
            await progress_cb("strike_wave1", f"🎯 {target}: Волна 1 — {len(plan.waves[0]) if plan.waves else 0} аккаунтов")

        # ═══ Волна 1: ReportPeer + ReportSpam + Join + Internal ═══
        wave_results: list[dict] = []
        if plan.waves:
            texts_w1 = assign_texts(plan.preset or plan.reason, len(plan.waves[0]))
            tasks = [
                _one_account_strike(acc, target, intel, plan.reason, plan.preset,
                                    texts_w1, i, 0, sem)
                for i, acc in enumerate(plan.waves[0])
            ]
            wave_results = await asyncio.gather(*tasks)

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
                                    texts_w2, i, 1, sem)
                for i, acc in enumerate(plan.waves[1])
            ]
            w2_results = await asyncio.gather(*tasks)
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
                                    texts_w3, i, 2, sem)
                for i, acc in enumerate(plan.waves[2])
            ]
            w3_results = await asyncio.gather(*tasks)
            wave_results.extend(w3_results)

        # Агрегация
        agg = aggregate_results(wave_results)
        for k, v in agg.items():
            if k in ("failed",):
                continue
            if hasattr(result, k):
                setattr(result, k, getattr(result, k, 0) + v)
        result.errors = [r.get("error") for r in wave_results if r.get("error")]

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

    results = await asyncio.gather(*tasks)
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
        "msgs": 0, "spam": 0, "reacts": 0, "admins": 0,
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
        s["spam"]      += r.get("spam_signaled", 0)
        s["reacts"]    += r.get("reactions_sent", 0)
        s["admins"]    += r.get("admins_reported", 0)
        s["linked_grp"] += 1 if r.get("linked_group_reported") else 0
        s["bots"]      += r.get("bots_reported", 0)
        s["fwd"]       += r.get("forwarded", 0)
        s["blocked"]   += 1 if r.get("blocked") else 0
    return s


def format_strike_summary(results: list[StrikeResult]) -> str:
    """Форматирует итоговый отчёт об атаке в HTML."""
    lines = ["⚔️ <b>Strike — Итоговый отчёт</b>\n"]
    for r in results:
        status = "🟢" if r.verified_down else ("🟡" if r.verified_down is None else "🔴")
        lines.append(
            f"{status} <code>{r.target}</code>\n"
            f"  ├ Жалоб на канал: <b>{r.peer_reported}</b> "
            f"(+{r.multi_reason} доп. причин)\n"
            f"  ├ Сообщений: <b>{r.msgs_reported}</b> · "
            f"Закреплённых: <b>{r.pinned_reported}</b> · "
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
            f"Заблокирован: <b>{r.blocked}</b>\n"
            f"  └ Длительность: <b>{r.duration_s:.0f}с</b> · "
            f"Аккаунтов: <b>{r.unique_accounts}</b>"
        )
        if r.errors:
            lines.append(f"  ⚠️ Ошибок: {len(r.errors)}")
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

    results = await asyncio.gather(*[_one(acc, txt) for acc, txt in zip(accounts, texts)])
    return list(results)


async def strike_network_nodes(
    accounts: list[dict],
    intel: dict,
    reason: str,
    preset: str | None,
) -> dict[str, int]:
    """Обратно-совместимый вызов — использует v2 (параллельная атака узлов)."""
    return await strike_network_nodes_v2(accounts, intel, reason, preset)
