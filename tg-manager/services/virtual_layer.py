"""Infragram Virtual Layer — вычислительный слой поверх Telegram.

Telegram сообщает, ЧТО произошло (пришло сообщение, тап по кнопке). Этот слой
определяет, ЧТО ЭТО ЗНАЧИТ: в каком состоянии находится сущность и какое
виртуальное событие из этого следует.

Организм уже дал шину и память (`organism/spine.py`). Здесь добавляется то,
чего не было, — СОСТОЯНИЕ как динамическая величина:

    не тег «interested», а
    {value: interested, confidence: 0.74, source: bot_17, expires: 48ч, history: …}

Три механики, и все, кроме записи в БД, — чистые функции (их и проверяют
тесты; заглушка пула типы связывания не ловит — см. CLAUDE.md):

* **SIGNAL → переход.** Сигнал двигает сущность по лестнице состояний. Вверх —
  только по сигналу, вниз — распадом (тишина остужает) или явным негативом.
* **Распад.** Состояние без подтверждения деградирует само: интерес, о котором
  сутки ничего не слышно, — уже не интерес. Тег так не умеет, оттого и врёт.
* **Каскад.** Много состояний уровня N рождают состояние уровня N+1: 147
  «горячих» пользователей бота → сам бот «горячий» → кампания «горячая».

На каждом РЕАЛЬНОМ переходе (значение изменилось) рождается виртуальное
событие, которого Telegram не шлёт: PURCHASE_INTENT_DETECTED, USER_LOST_INTEREST.
Оно уходит в spine — и существующий мозг/автоматизации реагируют на него так же,
как на любое другое событие. Это и есть «событие → интерпретация → состояние →
решение», а не «событие → if → действие».

Осознанные границы (владелец сам отметил, что это гипотезы):
* это НАДСТРОЙКА над unified_contacts.stage, не второй источник правды: сырой
  тег остаётся, сюда пишется вычисленное — с уверенностью и распадом;
* «форк реальности» (симуляция веток до действия) и маршрутизатор намерений —
  следующие слои поверх этого ядра, не здесь;
* координация ботов (Bot Mesh) — Telegram РЕАЛЬНО поддерживает bot-to-bot
  (opt-in в @BotFather; в группах доставка по `/cmd@bot` или reply, в ЛС —
  обоюдный opt-in, в бизнес-аккаунтах — только у отправителя). Гашение петель
  Telegram НЕ делает — это на нашей стороне; ядро координации и защиты от
  петель живёт в `services/bot_mesh.py`.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

# ── Уровни сущностей ───────────────────────────────────────────────────────
USER = "user"
CONVERSATION = "conversation"
BOT = "bot"
CAMPAIGN = "campaign"
NETWORK = "network"

# ── Лестница воронки (state_key = "funnel") ────────────────────────────────
# Порядок = сила намерения. Индекс на лестнице и есть «ранг».
LADDER = ["new", "curious", "interested", "qualified", "ready", "purchased"]
LOST = "lost"          # терминальный негатив (отписка/явный отказ)
_TERMINAL = {"purchased", LOST}   # не распадаются и вверх сигналом не двигаются

# Виртуальное событие при ВХОДЕ в состояние (снизу). Пусто — событие не рождаем.
ENTER_EVENT = {
    "curious": "user_became_active",
    "interested": "user_showed_interest",
    "ready": "purchase_intent_detected",
    "purchased": "purchase_confirmed",
    LOST: "user_lost_interest",
}
# Виды виртуальных событий этого слоя (для фильтрации ленты в read-поверхности).
VIRTUAL_EVENT_KINDS = list(dict.fromkeys(ENTER_EVENT.values()))

# Русские подписи значений и событий — владелец не читает по-английски.
VALUE_LABEL = {
    "new": "Новый", "curious": "Присматривается", "interested": "Интерес",
    "qualified": "Квалифицирован", "ready": "Готов купить",
    "purchased": "Купил", LOST: "Потерян",
}
TEMPERATURE_LABEL = {"hot": "🔥 Горячий", "warm": "🌤 Тёплый"}
EVENT_LABEL = {
    "user_became_active": "🌱 Ожил",
    "user_showed_interest": "👀 Проявил интерес",
    "purchase_intent_detected": "🔥 Намерение купить",
    "purchase_confirmed": "✅ Покупка",
    "user_lost_interest": "❄️ Остыл",
}

# Сигнал → минимальный ранг, до которого он подтягивает. Сигнал НЕ опускает
# (кроме явных негативов ниже): опускает только распад.
SIGNAL_TARGET = {
    "opened": "curious",
    "reacted": "curious",
    "asked_question": "curious",
    "replied": "interested",
    "asked_price": "interested",
    "clicked_offer": "qualified",
    "left_contact": "qualified",
    "added_to_cart": "ready",
    "asked_how_to_pay": "ready",
    "paid": "purchased",
}
# Явные негативные сигналы — переводят в терминальный LOST независимо от ранга.
NEGATIVE_SIGNALS = {"unsubscribed", "blocked", "refused", "reported"}

# Срок жизни рунга до распада (часы). Чем горячее — тем короче память: «готов»,
# о ком сутки тишина, почти наверняка уже остыл; «curious» живёт дольше.
_TTL_HOURS = {
    "curious": 168, "interested": 96, "qualified": 72, "ready": 24,
}
DEFAULT_TTL_HOURS = 96


def _now(now: datetime | None = None) -> datetime:
    return now or datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def rank(value: str | None) -> int:
    """Ранг на лестнице. Неизвестное/терминальный негатив → -1."""
    try:
        return LADDER.index(value)
    except (ValueError, TypeError):
        return -1


def ttl_for(value: str) -> timedelta | None:
    """Сколько живёт рунг до распада. Терминальные — бессрочно (None)."""
    if value in _TERMINAL:
        return None
    return timedelta(hours=_TTL_HOURS.get(value, DEFAULT_TTL_HOURS))


# ── SIGNAL → переход (чистая функция) ──────────────────────────────────────

def apply_signal(current: dict | None, signal: str, *,
                 confidence: float = 0.6, now: datetime | None = None) -> dict | None:
    """current-состояние + сигнал → НОВОЕ состояние, либо None если не изменилось.

    current: {"value","confidence","expires_at"} или None (состояния ещё нет).
    Возвращаемое: {"value","confidence","expires_at","reason"} для записи.

    Правила:
    * негативный сигнал → LOST (терминально), если ещё не LOST;
    * позитивный сигнал подтягивает до своего целевого рунга, но НЕ опускает;
    * повтор того же рунга не меняет значение, но копит уверенность (до 0.98) —
      это тоже переход (уверенность выросла), поэтому возвращаем его.
    """
    now = _now(now)
    cur_val = (current or {}).get("value")
    cur_conf = float((current or {}).get("confidence") or 0.0)
    conf = max(0.0, min(1.0, float(confidence)))

    if signal in NEGATIVE_SIGNALS:
        if cur_val == LOST:
            return None
        return {"value": LOST, "confidence": max(conf, 0.9),
                "expires_at": None, "reason": f"signal:{signal}"}

    target = SIGNAL_TARGET.get(signal)
    if target is None:
        return None                          # неизвестный сигнал не трогает состояние
    if cur_val in _TERMINAL:
        return None                          # из терминального сигналом не вытащить

    tr, cr = rank(target), rank(cur_val)
    if tr > cr:
        # Продвижение: значение растёт, уверенность = уверенность сигнала.
        return {"value": target, "confidence": conf,
                "expires_at": _expiry(target, now), "reason": f"signal:{signal}"}
    if tr == cr and cr >= 0:
        # Подтверждение того же рунга: копим уверенность, продлеваем срок.
        new_conf = min(0.98, cur_conf + (1.0 - cur_conf) * 0.4)
        if new_conf - cur_conf < 0.01:
            # Уже уверены — обновим только срок (продление), значение то же.
            return {"value": cur_val, "confidence": cur_conf,
                    "expires_at": _expiry(cur_val, now), "reason": "reaffirm",
                    "value_changed": False}
        return {"value": cur_val, "confidence": new_conf,
                "expires_at": _expiry(cur_val, now), "reason": "reaffirm",
                "value_changed": False}
    return None                              # сигнал слабее текущего — игнор


def _expiry(value: str, now: datetime) -> datetime | None:
    ttl = ttl_for(value)
    return now + ttl if ttl else None


# ── Распад (чистая функция) ────────────────────────────────────────────────

def decay(current: dict, *, now: datetime | None = None) -> dict | None:
    """Просроченное состояние → на рунг ниже, либо None если распад не нужен.

    Внизу лестницы (new) распадаться некуда → None. Терминальные не распадаются.
    Уверенность при остывании падает.
    """
    now = _now(now)
    val = current.get("value")
    if val in _TERMINAL:
        return None
    exp = _aware(current.get("expires_at"))
    if exp is None or exp > now:
        return None                          # ещё не пора
    r = rank(val)
    if r <= 0:
        return None                          # ниже new не опускаем
    lower = LADDER[r - 1]
    conf = max(0.2, float(current.get("confidence") or 0.5) * 0.6)
    return {"value": lower, "confidence": round(conf, 3),
            "expires_at": _expiry(lower, now), "reason": "decay"}


# ── Каскад (чистая функция) ────────────────────────────────────────────────

def cascade(child_values: list[str], *, hot_at: str = "ready",
            min_count: int = 20, min_share: float = 0.15) -> str | None:
    """Состояния детей → состояние родителя: hot | warm | None.

    Родитель «горячий», когда «горячих» детей достаточно И по числу (min_count),
    И по доле (min_share) — одно без другого лжёт: 3 из 5 это не тренд бота,
    а 20 из 100000 — не «горячая» кампания. «Тёплый» — половина порога.
    """
    total = len([v for v in child_values if v])
    if total == 0:
        return None
    threshold = rank(hot_at)
    if threshold < 0:
        return None
    hot = sum(1 for v in child_values if rank(v) >= threshold)
    share = hot / total
    if hot >= min_count and share >= min_share:
        return "hot"
    if hot >= max(1, min_count // 2) and share >= min_share / 2:
        return "warm"
    return None


def virtual_event_for(from_value: str | None, to_value: str) -> str | None:
    """Имя виртуального события при переходе (только при движении ВВЕРХ или в
    LOST). Распад вниз по лестнице события не рождает, кроме ухода в LOST."""
    if to_value == from_value:
        return None
    if to_value == LOST:
        return ENTER_EVENT.get(LOST)
    if rank(to_value) > rank(from_value):
        return ENTER_EVENT.get(to_value)
    return None


# ── Персистенция (тонкая; вся логика — выше) ───────────────────────────────

async def get_state(pool, owner_id: int, entity_type: str, entity_id,
                    state_key: str = "funnel") -> dict | None:
    """Текущее состояние. Просроченное отдаём как есть (распад — отдельный
    проход `run_decay`); вызывающему видно по expires_at, что оно остыло."""
    row = await pool.fetchrow(
        "SELECT value, confidence, source, expires_at, updated_at "
        "FROM virtual_states WHERE owner_id=$1 AND entity_type=$2 "
        "AND entity_id=$3 AND state_key=$4",
        owner_id, entity_type, str(entity_id), state_key)
    return dict(row) if row else None


async def _write(pool, owner_id: int, entity_type: str, entity_id: str,
                 state_key: str, new: dict, source: str | None,
                 from_value: str | None) -> None:
    await pool.execute(
        """INSERT INTO virtual_states
             (owner_id, entity_type, entity_id, state_key, value, confidence,
              source, expires_at, updated_at)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8, now())
           ON CONFLICT (owner_id, entity_type, entity_id, state_key)
           DO UPDATE SET value=EXCLUDED.value, confidence=EXCLUDED.confidence,
             source=EXCLUDED.source, expires_at=EXCLUDED.expires_at,
             updated_at=now()""",
        owner_id, entity_type, entity_id, state_key, new["value"],
        float(new["confidence"]), source, new.get("expires_at"))
    await pool.execute(
        """INSERT INTO virtual_state_history
             (owner_id, entity_type, entity_id, state_key, from_value, to_value,
              reason, confidence)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8)""",
        owner_id, entity_type, entity_id, state_key, from_value, new["value"],
        new.get("reason"), float(new["confidence"]))


async def signal(pool, owner_id: int, entity_type: str, entity_id,
                 signal_name: str, *, state_key: str = "funnel",
                 confidence: float = 0.6, source: str | None = None,
                 now: datetime | None = None) -> dict | None:
    """Подать сигнал сущности. Пишет новое состояние, ведёт историю и на
    РЕАЛЬНОМ переходе (значение изменилось) эмитит виртуальное событие в spine.

    Возвращает применённое изменение или None (сигнал ничего не изменил).
    Fail-open: сбой шины не откатывает состояние.
    """
    eid = str(entity_id)
    current = await get_state(pool, owner_id, entity_type, eid, state_key)
    change = apply_signal(current, signal_name, confidence=confidence, now=now)
    if change is None:
        return None
    from_value = (current or {}).get("value")
    await _write(pool, owner_id, entity_type, eid, state_key, change, source, from_value)

    if change.get("value_changed") is False:
        return change                        # только уверенность/срок — событие не рождаем
    ev = virtual_event_for(from_value, change["value"])
    if ev:
        try:
            from services.organism import spine
            await spine.emit(pool, owner_id, ev, {
                "entity_type": entity_type, "entity_id": eid,
                "state_key": state_key, "from": from_value,
                "to": change["value"], "confidence": change["confidence"],
                "source": source})
        except Exception:
            log.debug("virtual_layer: emit failed ev=%s", ev, exc_info=True)
    return change


async def signal_for_telegram_user(pool, owner_id: int, tg_user_id,
                                   signal_name: str, *, confidence: float = 0.6,
                                   source: str | None = None) -> dict | None:
    """Сигнал по Telegram-id человека, а не по id контакта.

    Источники сигналов живут там, где приходят обновления Telegram, и знают
    человека по его tg-id. Состояния этого слоя ключуются по контакту
    (unified_contacts.id) — так же, как их пишет сенсор намерений. Разводить
    два пространства идентификаторов нельзя: один и тот же человек получил бы
    два независимых состояния, и оба были бы неполными.

    Человека, которого у владельца нет в контактах, пропускаем молча: слой —
    надстройка над CRM-контактом, и заводить контакт по чужому сообщению здесь
    не его дело.

    Fail-open: сбой слоя не ломает обработку сообщения.
    """
    if not owner_id or not tg_user_id:
        return None
    try:
        contact_id = await pool.fetchval(
            "SELECT id FROM unified_contacts "
            "WHERE owner_id=$1 AND telegram_user_id=$2",
            int(owner_id), int(tg_user_id))
    except Exception:
        log.debug("virtual_layer: contact lookup failed owner=%s tg=%s",
                  owner_id, tg_user_id, exc_info=True)
        return None
    if not contact_id:
        return None
    try:
        return await signal(pool, int(owner_id), USER, contact_id, signal_name,
                            confidence=confidence, source=source)
    except Exception:
        log.debug("virtual_layer: signal failed owner=%s signal=%s",
                  owner_id, signal_name, exc_info=True)
        return None


async def run_decay(pool, owner_id: int | None = None, *, limit: int = 500,
                    now: datetime | None = None) -> int:
    """Остудить просроченные состояния (на рунг ниже). Возвращает число
    остывших. Каждый распад в LOST-переход тоже рождает виртуальное событие."""
    now = _now(now)
    where = "expires_at IS NOT NULL AND expires_at <= now()"
    args: list = []
    if owner_id is not None:
        where += " AND owner_id=$1"
        args.append(owner_id)
    rows = await pool.fetch(
        f"SELECT owner_id, entity_type, entity_id, state_key, value, confidence, "
        f"expires_at, source FROM virtual_states WHERE {where} LIMIT {int(limit)}",
        *args)
    n = 0
    for r in rows:
        cur = dict(r)
        new = decay(cur, now=now)
        if new is None:
            continue
        await _write(pool, cur["owner_id"], cur["entity_type"], cur["entity_id"],
                     cur["state_key"], new, cur.get("source"), cur["value"])
        n += 1
        ev = virtual_event_for(cur["value"], new["value"])
        if ev:
            try:
                from services.organism import spine
                await spine.emit(pool, cur["owner_id"], ev, {
                    "entity_type": cur["entity_type"], "entity_id": cur["entity_id"],
                    "state_key": cur["state_key"], "from": cur["value"],
                    "to": new["value"], "reason": "decay"})
            except Exception:
                log.debug("virtual_layer: decay emit failed", exc_info=True)
    return n


async def _apply_cascade_verdict(pool, owner_id: int, parent_type: str, parent_id,
                                 state_key: str, verdict: str) -> str:
    """Записать вердикт каскада родителю и, на смене, эмитнуть событие."""
    pid = str(parent_id)
    current = await get_state(pool, owner_id, parent_type, pid, state_key)
    if (current or {}).get("value") == verdict:
        return verdict                       # без изменений — не шумим
    new = {"value": verdict, "confidence": 0.8, "expires_at": None,
           "reason": "cascade"}
    await _write(pool, owner_id, parent_type, pid, state_key, new, "cascade",
                 (current or {}).get("value"))
    try:
        from services.organism import spine
        await spine.emit(pool, owner_id, f"{parent_type}_became_{verdict}", {
            "entity_type": parent_type, "entity_id": pid, "state_key": state_key,
            "to": verdict})
    except Exception:
        log.debug("virtual_layer: cascade emit failed", exc_info=True)
    return verdict


async def recompute_cascade(pool, owner_id: int, parent_type: str, parent_id,
                            child_type: str, *, state_key: str = "funnel",
                            **kw) -> str | None:
    """Пересчитать состояние родителя из состояний детей и записать его.

    Детьми считаются все сущности `child_type` этого владельца с состоянием по
    `state_key`. Родитель получает hot/warm по правилу `cascade`. Возвращает
    новое состояние родителя или None.
    """
    rows = await pool.fetch(
        "SELECT value FROM virtual_states WHERE owner_id=$1 AND entity_type=$2 "
        "AND state_key=$3", owner_id, child_type, state_key)
    verdict = cascade([r["value"] for r in rows], **kw)
    if verdict is None:
        return None
    return await _apply_cascade_verdict(pool, owner_id, parent_type, parent_id,
                                        state_key, verdict)


# Источник состояния записывается подателем сигнала: auto_responder ставит
# "bot_<id>". Это и есть недостающая связь «человек → бот», из-за отсутствия
# которой каскад до сих пор катился только на уровень всей аудитории владельца.
_BOT_SOURCE = re.compile(r"^bot_(\d+)$")


async def recompute_bot_cascade(pool, owner_id: int, *, state_key: str = "funnel",
                                min_count: int = 5, min_share: float = 0.1,
                                **kw) -> dict[str, str]:
    """Свернуть состояния людей в состояние БОТА, через которого они пришли.

    Пороги ниже, чем у каскада на всю аудиторию: у отдельного бота аудитория
    меньше на порядок, и порог «20 горячих» для него недостижим — каскад просто
    никогда не срабатывал бы. Пять горячих из полусотни — уже разговор.

    Возвращает {bot_id: вердикт} только по ботам, где вердикт есть.
    """
    rows = await pool.fetch(
        "SELECT source, value FROM virtual_states "
        "WHERE owner_id=$1 AND entity_type=$2 AND state_key=$3 "
        "AND source IS NOT NULL",
        owner_id, USER, state_key)

    by_bot: dict[str, list[str]] = {}
    for r in rows:
        m = _BOT_SOURCE.match(r["source"] or "")
        if m:
            by_bot.setdefault(m.group(1), []).append(r["value"])

    out: dict[str, str] = {}
    for bot_id, values in by_bot.items():
        verdict = cascade(values, min_count=min_count, min_share=min_share, **kw)
        if verdict is None:
            continue
        out[bot_id] = await _apply_cascade_verdict(
            pool, owner_id, BOT, bot_id, state_key, verdict)
    return out


# ── Read-поверхность (обзор для владельца) ─────────────────────────────────

async def overview(pool, owner_id: int, *, entity_type: str = USER,
                   state_key: str = "funnel") -> dict:
    """Сводка слоя для экрана: распределение воронки + «горячие» сущности.

    Лента виртуальных событий берётся отдельно из spine (это его память),
    поэтому здесь только состояния. Fail-open: пустая сводка вместо падения.
    """
    out = {"funnel": [], "hot": [], "total": 0, "audience": None, "bots": []}
    try:
        rows = await pool.fetch(
            "SELECT value, COUNT(*) AS c FROM virtual_states "
            "WHERE owner_id=$1 AND entity_type=$2 AND state_key=$3 "
            "GROUP BY value", owner_id, entity_type, state_key)
    except Exception:
        log.debug("virtual_layer.overview funnel failed owner=%s", owner_id)
        return out
    dist = {r["value"]: int(r["c"]) for r in rows}
    order = LADDER + [LOST]
    out["funnel"] = [
        {"value": v, "label": VALUE_LABEL.get(v, v), "count": dist.get(v, 0)}
        for v in order if dist.get(v, 0) > 0
    ]
    out["total"] = sum(dist.values())
    # «Горячие» сущности верхних рунгов — кого дожимать в первую очередь.
    try:
        hot_rows = await pool.fetch(
            "SELECT entity_type, entity_id, value, confidence, updated_at "
            "FROM virtual_states WHERE owner_id=$1 AND state_key=$2 "
            "AND value = ANY($3::text[]) ORDER BY confidence DESC, updated_at DESC "
            "LIMIT 50", owner_id, state_key, ["ready", "qualified"])
        out["hot"] = [
            {"entity_type": r["entity_type"], "entity_id": r["entity_id"],
             "value": r["value"], "label": VALUE_LABEL.get(r["value"], r["value"]),
             "confidence": round(float(r["confidence"] or 0), 2)}
            for r in hot_rows]
    except Exception:
        log.debug("virtual_layer.overview hot failed owner=%s", owner_id)
    # Температура аудитории (каскад): hot|warm|None.
    try:
        aud = await get_state(pool, owner_id, NETWORK, "audience", state_key)
        if aud:
            out["audience"] = aud.get("value")
    except Exception:
        log.debug("virtual_layer.overview audience failed owner=%s", owner_id)
    # Температура КАЖДОГО бота (каскад «бот ← люди»). Владельцу важно не
    # «аудитория греется», а ГДЕ именно: с каким ботом разговаривают всерьёз.
    try:
        bot_rows = await pool.fetch(
            "SELECT vs.entity_id, vs.value, vs.confidence, vs.updated_at, "
            "       COALESCE(mb.username, mb.first_name) AS bot_name "
            "FROM virtual_states vs "
            "LEFT JOIN managed_bots mb ON mb.bot_id::text = vs.entity_id "
            "WHERE vs.owner_id=$1 AND vs.entity_type=$2 AND vs.state_key=$3 "
            "ORDER BY vs.updated_at DESC LIMIT 50",
            owner_id, BOT, state_key)
        out["bots"] = [
            {"bot_id": r["entity_id"],
             "name": r["bot_name"] or f"id{r['entity_id']}",
             "value": r["value"],
             "label": TEMPERATURE_LABEL.get(r["value"], r["value"]),
             "confidence": round(float(r["confidence"] or 0), 2)}
            for r in bot_rows]
    except Exception:
        log.debug("virtual_layer.overview bots failed owner=%s", owner_id)
    return out

