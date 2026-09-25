"""Мозг организма: из живого мира — цепочки следующих действий.

Не «ещё один островной советчик»: читает ЕДИНЫЙ мир-снимок (world) и события
(spine) и предлагает связки МЕЖДУ модулями — то, чего система не делала. Пример:
входящее «цена» (Vault→CRM пометил горячим) → мозг видит горячих в графе →
предлагает «написать оффер сегментом» (Сегмент→действие). Каждая подсказка несёт
исполняемое действие (kind + payload), которое поверхность разворачивает в один тап.

build_suggestions — чистая функция (тестируется без БД). pulse — сборка живьём.
"""
from __future__ import annotations

_SEV = {"urgent": 0, "warn": 1, "opportunity": 2, "info": 3}

# Периоды «заглушить», предлагаемые кнопками под нуджем. Порядок = порядок кнопок.
SNOOZE_PRESETS: tuple[tuple[str, str, int], ...] = (
    ("6h",  "6 часов",  6 * 3600),
    ("24h", "сутки",    24 * 3600),
    ("3d",  "3 дня",    3 * 24 * 3600),
    ("7d",  "неделю",   7 * 24 * 3600),
)
_SNOOZE_BY_CODE = {code: (label, secs) for code, label, secs in SNOOZE_PRESETS}


def snooze_seconds(code: str) -> int:
    """Секунды по коду периода; 0 — код неизвестен (глушить не будем)."""
    got = _SNOOZE_BY_CODE.get(str(code or ""))
    return got[1] if got else 0


def snooze_label(code: str) -> str:
    got = _SNOOZE_BY_CODE.get(str(code or ""))
    return got[0] if got else str(code or "")


def active_snoozes(snoozed: dict | None, now: float) -> dict:
    """Оставить только НЕ истёкшие глушилки {id: until_ts}.

    Отдельная чистая функция, потому что протухшие записи надо отбрасывать и при
    чтении (иначе подсказка молчала бы вечно), и при записи (иначе словарь растёт).
    """
    out: dict[str, float] = {}
    for sid, until in (snoozed or {}).items():
        try:
            ts = float(until)
        except (TypeError, ValueError):
            continue
        if ts > now:
            out[str(sid)] = ts
    return out


def build_suggestions(snap: dict, dismissed=(), snoozed=None, now: float | None = None) -> list[dict]:
    fleet = snap.get("fleet") or {}
    ops = snap.get("ops") or {}
    graph = snap.get("graph") or {}
    vault = snap.get("vault") or {}
    goal = snap.get("goal")
    out: list[dict] = []
    dismissed = set(dismissed or ())
    if now is None:
        import time as _t
        now = _t.time()
    muted = active_snoozes(snoozed, now)

    def add(sid, sev, title, why, action):
        # dismissed — отклонено навсегда; muted — заглушено до срока.
        if sid in dismissed or sid in muted:
            return
        out.append({"id": sid, "severity": sev, "title": title,
                    "why": why, "action": action})

    vh = vault.get("health")
    if vh in ("disabled", "never"):
        add("vault_off", "urgent", "Хранилище не пишет",
            "Входящие ЛС не сохраняются — сенсор намерений и welcome-аналитика "
            "не работают. Переподключите бизнес-бота.", {"kind": "vault"})
    wr = vault.get("waiting_reply", 0)
    if wr > 0 and vh not in ("disabled", "never"):
        add("vault_waiting", "warn", f"{wr} клиентов ждут ответа",
            "Люди написали в ваш Telegram и остались без ответа (переписка "
            "сохранена в разделе «Хранилище»). Нажмите «Открыть» — прочитаете и "
            "ответите прямо в приложении, пока лид тёплый.",
            {"kind": "vault"})
    if vh == "stale":
        d = vault.get("stale_days")
        add("vault_stale", "warn", "Хранилище молчит",
            f"{d} дн. без новых сообщений — вероятно бизнес-бот отвалился.",
            {"kind": "vault"})

    # Виртуальный слой. Сначала — то, что слой ПОНЯЛ только что: виртуальные
    # события за сутки. Счётчик «сколько всего готовых» не меняется от того,
    # что человек дошёл до решения минуту назад, и мозг повторял одно и то же
    # про один и тот же остывающий список. Событие — это момент, ради которого
    # слой и строился, и реагировать на него надо, пока он момент.
    vl = snap.get("vlayer") or {}
    _fresh = vl.get("became_ready_24h", 0)
    _cooled = vl.get("cooled_24h", 0)
    _hot_bot = vl.get("hot_bot")

    if _fresh >= 3:
        add("vlayer_intent_now", "urgent",
            f"{_fresh} дошли до решения за сутки",
            "Модель поведения поймала переход в «готов купить» — это свежие "
            "решения, а не накопленный список. Такие контакты остывают сами: "
            "отправьте оффер, пока момент не ушёл.",
            {"kind": "vlayer"})
    elif vl.get("ready", 0) >= 5:
        add("vlayer_hot", "warn", f"{vl['ready']} готовы купить",
            "Модель поведения свела переписку в состояние «готов купить». Эти "
            "контакты остынут сами, если не дожать сейчас — отправьте оффер.",
            {"kind": "vlayer"})
    elif vl.get("audience") == "hot":
        add("vlayer_audience", "warn", "Аудитория разогрелась",
            "Готовых и квалифицированных набралось достаточно, чтобы вся "
            "аудитория считалась горячей — момент для рассылки.",
            {"kind": "vlayer"})

    # Остывание — вторая половина слоя, на которую до сих пор не реагировал
    # никто: событие рождалось и уходило в журнал.
    if _cooled >= 5:
        add("vlayer_cooling", "warn", f"{_cooled} остыли за сутки",
            "Слой перевёл их в «потерян»: интерес не подтверждался и распался "
            "сам. Это ещё не чужие люди — реактивация по ним дешевле нового "
            "трафика, но через неделю будет поздно.",
            {"kind": "vlayer"})

    # «Заходил и уходил» — не интерес, а привычка смотреть и не покупать.
    # Такого события Telegram не шлёт: оно видно только по истории переходов.
    if vl.get("bouncing", 0) >= 3:
        add("vlayer_bouncing", "warn",
            f"{vl['bouncing']} ходят по кругу",
            "Эти люди уже несколько раз подходили к решению и отходили. "
            "Ещё одно такое же письмо даст тот же круг: им нужно другое "
            "предложение, живой разговор или честное «не сейчас».",
            {"kind": "vlayer"})

    # Где именно греется: каскад «бот ← люди» уже посчитал температуру, но
    # подсказки по ней не было — владелец видел цифру и не знал, что с ней.
    if _hot_bot:
        _name = _hot_bot if str(_hot_bot).startswith("@") else f"@{_hot_bot}"
        add("vlayer_bot_hot", "warn", f"Бот {_name} разогрелся",
            "С этим ботом разговаривают всерьёз: у его аудитории набралось "
            "достаточно готовых. Сюда и стоит направить оффер и внимание — "
            "остальные боты сейчас холоднее.",
            {"kind": "vlayer"})

    anom = snap.get("anomalies") or {}
    if anom.get("critical", 0) > 0:
        add("anomaly_crit", "urgent",
            f"{anom['critical']} критических аномалий флота",
            (f"{anom['top']}. " if anom.get("top") else "")
            + "Детектор поймал резкий сбой (ошибки/trust/латентность). Откройте "
            "здоровье — это ранний сигнал бана, лучше вмешаться сейчас.",
            {"kind": "health"})
    elif anom.get("warning", 0) > 0:
        add("anomaly_warn", "warn",
            f"{anom['warning']} аномалий флота за сутки",
            (f"{anom['top']}. " if anom.get("top") else "")
            + "Показатели отклонились от нормы. Проверьте здоровье, пока не переросло "
            "в блокировки.",
            {"kind": "health"})

    ic = snap.get("invite_chats") or {}
    if ic.get("frozen", 0) > 0 or ic.get("dead", 0) > 0:
        fr, dd = ic.get("frozen", 0), ic.get("dead", 0)
        bits = []
        if fr:
            bits.append(f"{fr} на паузе приёма")
        if dd:
            bits.append(f"{dd} мёртвых по активности")
        add("invite_chats_frozen", "warn",
            "Инвайт: " + ", ".join(bits),
            "Telegram морозит приём в эти чаты (chat-flood или «холодный» приток в "
            "мёртвый чат). Лить туда дальше — гарантированный флуд: смените цель или "
            "сначала оживите чат.",
            {"kind": "invite"})

    restricted = fleet.get("restricted", 0)
    if restricted > 0:
        add("accounts_restricted", "warn",
            f"{restricted} аккаунтов под спам-блоком",
            "Ограничение снимается не само: снимите их с массовых операций, дайте "
            "тихий прогрев и перепроверьте статус — иначе они копят риск и тянут "
            "флот вниз. Откройте здоровье для реабилитации.",
            {"kind": "health"})

    bans = fleet.get("bans_24h", 0)
    if bans > 0:
        add("bans", "warn", f"{bans} бан(ов) за сутки",
            "Флот теряет аккаунты. Снизьте темп, проверьте прокси/прогрев — "
            "губернатор уже притормозил, но причину стоит устранить.",
            {"kind": "governor"})
    if fleet.get("governor_level") == "red":
        add("gov_red", "warn", f"Флот под давлением ×{fleet.get('governor_mult')}",
            f"Давление {fleet.get('pressure')}/100 — рисковые операции лучше "
            "отложить, флот прогреть.", {"kind": "governor"})

    bots = snap.get("bots") or {}
    if bots.get("inactive", 0) > 0 and bots.get("total", 0) > 0:
        add("bots_inactive", "warn",
            f"{bots['inactive']} из {bots['total']} ботов сети неактивны",
            "Неактивные боты не принимают вебхуки и не участвуют в сетевой "
            "рассылке. Проверьте токены/вебхуки в разделе сети.",
            {"kind": "bots"})
    if bots.get("community_empty", 0) > 0:
        add("community_empty", "opportunity",
            f"{bots['community_empty']} сообществ без каналов",
            "Нода-комьюнити пуста: добавьте каналы-топики и пригласите аудиторию, "
            "иначе сообщество не оживёт.",
            {"kind": "community"})

    growth = snap.get("growth") or {}
    if growth.get("suspicious", 0) > 0:
        top = growth.get("fake_top")
        add("growth_fake", "warn",
            f"Подозрение на накрутку: {growth['suspicious']} канал(ов)",
            (f"«{top}» и др. — " if top else "")
            + "резкий неорганический скачок подписчиков (боты влетают разом и "
            "осыпаются, мёртвая аудитория режет охваты). Проверьте источник "
            "прироста, пока не занизило охваты.",
            {"kind": "growth"})
    if growth.get("channels", 0) > 0 and growth.get("growth_ops_7d", 0) == 0:
        add("growth_stall", "opportunity",
            f"{growth['channels']} каналов без роста 7 дн.",
            "Инструменты роста простаивают. Запустите Мотор роста — накрутка, "
            "агент роста и самопиар в одном месте с планом под цель.",
            {"kind": "growth"})

    seo = snap.get("seo") or {}
    if seo.get("weak", 0) > 0:
        w = seo["weak"]
        worst = seo.get("worst")
        add("seo_weak", "opportunity",
            f"{w} объектов плохо находятся в поиске",
            (f"«{worst}» и др. — " if worst else "")
            + "слабый заголовок/@username/описание. Оптимизируйте — и канал начнут "
            "находить по ключевым словам, а не только по прямой ссылке.",
            {"kind": "seo"})

    geo = fleet.get("geo") or {}
    if geo.get("concentrated") and geo.get("top"):
        add("geo_concentrated", "opportunity",
            f"Флот сосредоточен в {geo['top']} ({int(geo.get('top_share', 0) * 100)}%)",
            "Одна гео = одна точка отказа (блок подсети/страны бьёт разом) и узкий "
            "охват. Добавьте прокси других стран — присутствие шире, риск ниже.",
            {"kind": "geo"})
    elif geo.get("unknown_share", 0) > 0.5 and geo.get("total", 0) >= 5:
        add("geo_unknown", "info",
            f"{int(geo['unknown_share'] * 100)}% аккаунтов без гео-прокси",
            "У большинства аккаунтов не определена страна прокси — гео-таргетинг и "
            "ночной режим по таймзоне для них не работают. Привяжите гео-прокси.",
            {"kind": "geo"})

    lf = ops.get("last_failed")
    if lf and ops.get("failed_24h", 0) > 0:
        add("op_fail", "warn", f"Операция #{lf['op_id']} не выполнена",
            (lf.get("reason") or lf.get("op_type") or "").strip() or "без причины",
            {"kind": "operation", "op_id": lf["op_id"]})

    # Ключевая межмодульная цепочка: горячие из графа → оффер сегментом.
    hot = graph.get("hot_leads", 0)
    if hot > 0:
        add("hot_offer", "opportunity", f"{hot} горячих ждут оффера",
            "Контакты в переговорах/с тегом интереса. Напишите им оффер сегментом "
            "— одной рассылкой с защитой темпа.",
            {"kind": "segment_hot"})

    it = graph.get("intents_24h", 0)
    if it > 0:
        add("intents", "info", f"{it} сигналов намерения за сутки",
            "Люди писали ключевые фразы («цена», «купить»). Посмотрите и дожмите.",
            {"kind": "intents"})

    # Ретеншен: пригласить — половина дела, вторая — удержать. Высокий отток при
    # значимом объёме → welcome не работает (лить в дырявое ведро).
    ret = snap.get("retention") or {}
    if ret.get("health") == "red" and ret.get("joined", 0) >= 10:
        add("retention_low", "warn",
            f"Отток {ret.get('churn_pct')}% приглашённых",
            f"Из {ret['joined']} вступивших за месяц ушли {ret.get('left', 0)}. "
            "Приглашать без удержания — лить в дырявое ведро: усильте welcome-цепочку "
            "и сравните варианты приветствия (A/B).",
            {"kind": "retention"})

    if fleet.get("dead", 0) > 0:
        add("dead", "opportunity", f"{fleet['dead']} мёртвых аккаунтов",
            "Невоскрешаемые (бан/деактивация/сессия) — удалите, чтобы не искажали "
            "планирование ёмкости.", {"kind": "purge_dead"})

    if not goal:
        add("set_goal", "info", "Задайте цель кампании",
            "Планировщик разложит «+N участников к сроку» по реальной ёмкости флота.",
            {"kind": "campaign"})
    else:
        lbl = goal.get("label") or f"+{goal.get('goal', '?')} участников"
        add("goal", "info", f"Активная цель: {lbl}",
            "Продолжайте по плану — планировщик пересчитает достижимость.",
            {"kind": "campaign"})

    if (ops.get("running", 0) == 0 and ops.get("pending", 0) == 0
            and fleet.get("active", 0) > 0 and graph.get("contacts", 0) > 0
            and fleet.get("governor_level") != "red" and hot == 0):
        add("idle", "info", "Флот свободен",
            "Нет активных операций, давление в норме. Запустите инвайт или "
            "напишите сегменту.", {"kind": "invite"})

    cw = snap.get("chat_warmup") or {}
    if cw.get("stalled", 0) > 0:
        add("cw_stalled", "warn", "Разогрев чата простаивает",
            f"{cw['stalled']} активных сессий без реплик 30+ мин — вероятно не задан "
            "AI-ключ (Claude/Groq) или флот занят. Задайте ключ в админке / проверьте флот.",
            {"kind": "chatwarmup"})
    elif cw.get("active", 0) == 0 and cw.get("chats", 0) > 0:
        add("cw_idle", "opportunity", "Оживите тихие чаты",
            f"У вас {cw['chats']} групп/чатов — флот может вести в них ОСМЫСЛЕННЫЙ "
            "диалог: между собой по темам и отвечая реальным участникам.",
            {"kind": "chatwarmup"})

    out.sort(key=lambda x: _SEV.get(x["severity"], 9))
    return out[:6]


def narrative(snap: dict) -> str:
    f = snap.get("fleet") or {}
    g = snap.get("graph") or {}
    o = snap.get("ops") or {}
    parts = [f"Флот: {f.get('accounts', 0)} акк. ({f.get('active', 0)} активны"
             + (f", {f['dead']} мёртвых" if f.get("dead") else "") + ")",
             f"давление {f.get('pressure', 0)}/100"]
    an = snap.get("anomalies") or {}
    day = []
    # Риск-сигналы — вперёд: сводка должна кричать о том, что горит.
    if an.get("critical"):
        day.append(f"⚠ {an['critical']} критич. аномалий")
    if o.get("running"):
        day.append(f"{o['running']} операц. в работе")
    if g.get("intents_24h"):
        day.append(f"{g['intents_24h']} намерений за сутки")
    if g.get("hot_leads"):
        day.append(f"{g['hot_leads']} горячих")
    tail = (" · " + ", ".join(day)) if day else ""
    return " · ".join(parts) + tail + "."


async def pulse(pool, owner_id: int) -> dict:
    """Живой пульс: мир + повествование + цепочки действий (без отклонённых)."""
    import time as _t
    from services.organism import world, spine
    snap = await world.snapshot(pool, owner_id)
    try:
        dismissed = await spine.state_get(pool, owner_id, "dismissed", []) or []
    except Exception:
        dismissed = []
    try:
        snoozed = await spine.state_get(pool, owner_id, SNOOZE_KEY, {}) or {}
    except Exception:
        snoozed = {}
    return {
        "narrative": narrative(snap),
        "snapshot": snap,
        "suggestions": build_suggestions(snap, dismissed, snoozed, _t.time()),
    }


SNOOZE_KEY = "snoozed"


async def snooze(pool, owner_id: int, suggestion_id: str, code: str) -> float:
    """Заглушить подсказку на период `code`. Возвращает unix-время окончания (0 — не вышло).

    Протухшие записи выкидываем при каждой записи — словарь не растёт бесконечно.
    """
    import time as _t
    from services.organism import spine
    secs = snooze_seconds(code)
    if not secs or not suggestion_id:
        return 0.0
    now = _t.time()
    try:
        cur = await spine.state_get(pool, owner_id, SNOOZE_KEY, {}) or {}
    except Exception:
        cur = {}
    fresh = active_snoozes(cur, now)
    until = now + secs
    fresh[str(suggestion_id)] = until
    await spine.state_set(pool, owner_id, SNOOZE_KEY, fresh)
    return until
