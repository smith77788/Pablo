"""Strike Engine — разведка цели, параллельная скоординированная атака, внешняя эскалация.

Отличие от простой отправки жалоб:
  1. Разведка   — полная карта цели до первого выстрела
  2. Уникальные тексты — каждый аккаунт пишет своё, на случайном языке
  3. Параллельность — все аккаунты стреляют одновременно (семафор 8)
  4. Сеть        — атакуем все найденные узлы: линкованная группа, боты, упомянутые каналы
  5. Внешняя эскалация — форма Telegram abuse + @spambot
"""
from __future__ import annotations

import asyncio
import logging
import random
from typing import Any

import aiohttp

log = logging.getLogger(__name__)

_CONCURRENCY = 8   # максимум параллельных аккаунтов

# ── Уникальные тексты жалоб (25+ на категорию, на 6+ языках) ─────────────────
# Каждый аккаунт получает СВОЙ уникальный текст, на случайном языке.
# Это создаёт разнообразный сигнал — не похоже на ботнет, похоже на живых людей.

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


def assign_texts(reason_or_preset: str, count: int) -> list[str]:
    """Выдаёт `count` уникальных текстов — каждому аккаунту свой."""
    pool = PRESET_TEXTS.get(reason_or_preset) or TEXTS.get(reason_or_preset) or TEXTS["other"]
    if count <= len(pool):
        return random.sample(pool, count)
    result: list[str] = []
    while len(result) < count:
        result.extend(random.sample(pool, min(len(pool), count - len(result))))
    return result


async def submit_abuse_form(
    target_username: str,
    reason: str,
    title: str = "",
    members: int = 0,
) -> dict[str, Any]:
    """Отправляет официальную форму жалобы на Telegram abuse endpoint."""
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
    }.get(reason, "Terms of Service violation and harmful illegal content")

    body = (
        f"I am reporting a Telegram channel for {reason_text}.\n\n"
        f"Channel: @{clean}\n"
        f"Title: {title}\n"
        f"Subscribers: {members}\n\n"
        f"This channel actively violates Telegram's Terms of Service by openly distributing "
        f"illegal content related to {reason_text}. The content is systematic, ongoing and "
        f"openly visible. Immediate removal is urgently required."
    )
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=20),
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


async def parallel_strike(
    accounts: list[dict],
    peer_username: str,
    intel: dict,
    reason: str,
    preset: str | None,
) -> list[dict]:
    """
    Запускает report_peer_deep параллельно для всех аккаунтов.
    Каждый аккаунт получает уникальный текст на своём языке.
    Возвращает список результатов (один на аккаунт).
    """
    from services import account_manager

    label = preset or reason
    texts = assign_texts(label, len(accounts))
    sem = asyncio.Semaphore(_CONCURRENCY)

    async def _one(acc: dict, text: str) -> dict:
        async with sem:
            try:
                return await account_manager.report_peer_deep(
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
    """
    Атакует все обнаруженные узлы сети: linked_group, упомянутые каналы, боты.
    Использует ротацию аккаунтов чтобы не нагружать один.
    Возвращает {"nodes_attacked": N, "total_reports": M}.
    """
    from services import account_manager

    nodes: list[str] = []
    if intel.get("linked_group_id"):
        nodes.append(str(intel["linked_group_id"]))
    nodes.extend(intel.get("mentioned_usernames", [])[:5])
    nodes.extend(intel.get("bot_usernames", [])[:3])

    if not nodes or not accounts:
        return {"nodes_attacked": 0, "total_reports": 0}

    label = preset or reason
    nodes_hit = 0
    total_rep = 0
    acc_cycle = list(accounts) * ((len(nodes) // len(accounts)) + 2)

    for i, node in enumerate(nodes):
        acc = acc_cycle[i % len(accounts)]
        text = assign_texts(label, 1)[0]
        try:
            res = await account_manager.report_peer_deep(
                acc["session_str"], node, reason,
                message=text,
                msg_messages=assign_texts(label, 5),
                max_msg_reports=20,
                block_after=False,
                multi_reason=True,
                join_first=False,
                negative_react=False,
                report_admins=False,
                report_linked_bots=False,
                forward_to_bot=False,
                report_photo=True,
                report_pinned=True,
                report_linked_group=False,
                _acc=acc,
            )
            if res.get("peer_reported"):
                nodes_hit += 1
                total_rep += 1 + res.get("multi_reason_sent", 0)
        except Exception as e:
            log.warning("strike_network_nodes node=%s: %s", node, e)
        await asyncio.sleep(random.uniform(1.5, 4.0))

    return {"nodes_attacked": nodes_hit, "total_reports": total_rep}


def aggregate_results(results: list[dict]) -> dict:
    """Суммирует результаты параллельной атаки."""
    s = {
        "peer": 0, "multi": 0, "photo": 0, "pinned": 0,
        "msgs": 0, "spam": 0, "reacts": 0, "admins": 0,
        "linked_grp": 0, "bots": 0, "fwd": 0, "blocked": 0, "failed": 0,
    }
    for r in results:
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
