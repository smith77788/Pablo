"""Сенсор намерений: входящее ЛС в Хранилище → CRM-стадия + тег + алерт.

Замыкает шов «Vault → CRM»: хранилище перестаёт быть архивом и становится
триггером на граф. Оператор задаёт правила «фраза → действие» (напр. «цена» →
стадия proposal + тег «горячий» + уведомление). Сенсор ловит их во ВХОДЯЩИХ
бизнес-сообщениях, находит/создаёт контакт по telegram_user_id, двигает стадию,
вешает тег и шлёт алерт — только когда что-то реально изменилось (без спама).

Пуст по умолчанию (никакого скрытого авто-тегирования); рекомендованный набор
включается в один тап (seed_default_rules).
"""
from __future__ import annotations

import datetime as dt
import logging
import re as _re

import asyncpg

log = logging.getLogger(__name__)

VALID_STAGES = ("lead", "contact", "proposal", "negotiation", "won", "lost")

# Рекомендованный набор (включается опционально). Фразы — в нижнем регистре.
DEFAULT_RULES = [
    {"phrase": "цена", "stage": "proposal", "tag": "интерес-цена", "notify": True},
    {"phrase": "сколько стоит", "stage": "proposal", "tag": "интерес-цена", "notify": True},
    {"phrase": "купить", "stage": "negotiation", "tag": "горячий", "notify": True},
    {"phrase": "оплат", "stage": "negotiation", "tag": "горячий", "notify": True},
    {"phrase": "готов", "stage": "negotiation", "tag": "горячий", "notify": True},
    {"phrase": "не интересно", "stage": "lost", "tag": "отказ", "notify": False},
    {"phrase": "спам", "stage": "lost", "tag": "жалоба", "notify": True},
]

_STAGE_RANK = {s: i for i, s in enumerate(VALID_STAGES)}

# Воронка продаж: lost — не «продвинутее» won, это отдельный терминальный исход.
# Общий _STAGE_RANK ставил lost на самый верх (5) просто по позиции в кортеже,
# из-за чего фраза-отказ перебивала уже закрытую сделку.
_PIPELINE = ("lead", "contact", "proposal", "negotiation", "won")
_PIPELINE_RANK = {s: i for i, s in enumerate(_PIPELINE)}


def decide_stage(current: str | None, matched_stages: list[str]) -> str | None:
    """Куда двигать стадию по совпавшим правилам. None — не двигать.

    Правила движения (раньше их не было вовсе — стадия просто менялась на
    совпавшую, из-за чего клиент из «Выиграно», спросивший «а какая цена на
    второй заказ», откатывался в «Предложение» и портил воронку):
      • только ВПЕРЁД по воронке, назад — никогда;
      • «Проиграно» — терминальный отказ, ставится с любой стадии, кроме
        «Выиграно»: закрытую сделку одной фразой отменять нельзя;
      • из «Проиграно» клиент может вернуться в воронку (передумал).

    Чистая функция — проверяется отдельно от БД.
    """
    stages = [s for s in matched_stages if s in _STAGE_RANK]
    if not stages:
        return None
    if "lost" in stages:
        # Явный отказ информативнее любого позитивного совпадения в том же
        # сообщении, но закрытую сделку он не трогает.
        return None if current == "won" else ("lost" if current != "lost" else None)
    target = max(stages, key=lambda s: _PIPELINE_RANK.get(s, -1))
    if target == current:
        return None
    # Из lost — возвращаем в воронку (человек передумал), иначе только вперёд.
    if current in (None, "lost"):
        return target
    return target if _PIPELINE_RANK.get(target, -1) > _PIPELINE_RANK.get(current, -1) else None


async def list_rules(pool: asyncpg.Pool, owner_id: int) -> list[dict]:
    rows = await pool.fetch(
        "SELECT id, phrase, stage, tag, notify, is_active, hits FROM vault_intent_rules "
        "WHERE owner_id=$1 ORDER BY id", owner_id)
    return [dict(r) for r in rows]


async def add_rule(pool: asyncpg.Pool, owner_id: int, phrase: str,
                   stage: str | None, tag: str | None, notify: bool = True) -> int:
    phrase = (phrase or "").strip()[:120]
    # Регэксп-правила (re:) сохраняем как есть — понижение регистра ломает
    # экранированные классы (\B→\b). Совпадение всё равно идёт по IGNORECASE.
    # Обычные фразы/альтернативы — в нижний регистр (как и раньше).
    if phrase[:3].lower() != "re:":
        phrase = phrase.lower()
    if not phrase:
        raise ValueError("Пустая фраза")
    if phrase[:3].lower() == "re:":
        pat = phrase[3:].strip()
        if not pat:
            raise ValueError("Пустой регэксп после re:")
        try:
            _re.compile(pat)
        except _re.error as e:
            raise ValueError(f"Некорректный регэксп: {e}") from e
    stage = stage if stage in VALID_STAGES else None
    tag = (tag or "").strip()[:60] or None
    if not stage and not tag:
        raise ValueError("Правило без действия: укажите стадию или тег")
    return int(await pool.fetchval(
        "INSERT INTO vault_intent_rules(owner_id, phrase, stage, tag, notify) "
        "VALUES($1,$2,$3,$4,$5) RETURNING id",
        owner_id, phrase, stage, tag, bool(notify)))


async def update_rule(pool: asyncpg.Pool, owner_id: int, rule_id: int, *,
                      phrase: str | None = None, stage: str | None = ...,
                      tag: str | None = ..., notify: bool | None = None) -> bool:
    """Изменить правило на месте.

    Правки не было вовсе: чтобы исправить опечатку во фразе, правило удаляли и
    создавали заново — вместе со счётчиком срабатываний и журналом, то есть
    теряя ровно ту историю, по которой правило и настраивают.

    `stage`/`tag` принимают None как ОСМЫСЛЕННОЕ значение (снять), поэтому
    «не передано» обозначено сентинелом `...`.
    """
    sets: list[str] = []
    args: list = []
    if phrase is not None:
        p = (phrase or "").strip()[:120]
        if p[:3].lower() != "re:":
            p = p.lower()
        if not p:
            raise ValueError("Пустая фраза")
        if p[:3].lower() == "re:":
            pat = p[3:].strip()
            if not pat:
                raise ValueError("Пустой регэксп после re:")
            try:
                _re.compile(pat)
            except _re.error as e:
                raise ValueError(f"Некорректный регэксп: {e}") from e
        args.append(p); sets.append(f"phrase=${len(args)}")
    if stage is not ...:
        args.append(stage if stage in VALID_STAGES else None)
        sets.append(f"stage=${len(args)}")
    if tag is not ...:
        args.append((tag or "").strip()[:60] or None)
        sets.append(f"tag=${len(args)}")
    if notify is not None:
        args.append(bool(notify)); sets.append(f"notify=${len(args)}")
    if not sets:
        raise ValueError("Нечего изменять")

    # Правило без действия бесполезно: проверяем ИТОГОВОЕ состояние, а не только
    # присланные поля — иначе можно было снять и стадию, и тег по отдельности.
    cur = await pool.fetchrow(
        "SELECT stage, tag FROM vault_intent_rules WHERE id=$1 AND owner_id=$2",
        rule_id, owner_id)
    if not cur:
        return False
    new_stage = (stage if stage in VALID_STAGES else None) if stage is not ... else cur["stage"]
    new_tag = ((tag or "").strip()[:60] or None) if tag is not ... else cur["tag"]
    if not new_stage and not new_tag:
        raise ValueError("Правило без действия: укажите стадию или тег")

    args.extend([rule_id, owner_id])
    res = await pool.execute(
        f"UPDATE vault_intent_rules SET {', '.join(sets)} "
        f"WHERE id=${len(args) - 1} AND owner_id=${len(args)}", *args)
    return not str(res).endswith(" 0")


async def recent_hits(pool: asyncpg.Pool, owner_id: int, rule_id: int | None = None,
                      limit: int = 50) -> list[dict]:
    """Последние срабатывания правил — чтобы их можно было отлаживать.

    Fail-open: журнал появился позже правил, и его отсутствие (лаг миграции)
    не должно ронять экран.
    """
    try:
        if rule_id:
            rows = await pool.fetch(
                """SELECT h.id, h.rule_id, r.phrase, h.peer_name, h.chat_id,
                          h.text_preview, h.stage_to, h.tags_added, h.created_at
                     FROM vault_intent_hits h
                     JOIN vault_intent_rules r ON r.id = h.rule_id
                    WHERE h.owner_id=$1 AND h.rule_id=$2
                 ORDER BY h.created_at DESC LIMIT $3""",
                owner_id, rule_id, min(int(limit), 200))
        else:
            rows = await pool.fetch(
                """SELECT h.id, h.rule_id, r.phrase, h.peer_name, h.chat_id,
                          h.text_preview, h.stage_to, h.tags_added, h.created_at
                     FROM vault_intent_hits h
                     JOIN vault_intent_rules r ON r.id = h.rule_id
                    WHERE h.owner_id=$1
                 ORDER BY h.created_at DESC LIMIT $2""",
                owner_id, min(int(limit), 200))
    except Exception:
        log.debug("intent_sensor: журнал недоступен owner=%s", owner_id)
        return []
    out = []
    for r in rows:
        d = dict(r)
        d["created_at"] = r["created_at"].isoformat() if r["created_at"] else None
        d["tags_added"] = list(r["tags_added"] or [])
        out.append(d)
    return out


async def delete_rule(pool: asyncpg.Pool, owner_id: int, rule_id: int) -> bool:
    res = await pool.execute(
        "DELETE FROM vault_intent_rules WHERE id=$1 AND owner_id=$2", rule_id, owner_id)
    return not str(res).endswith(" 0")


async def toggle_rule(pool: asyncpg.Pool, owner_id: int, rule_id: int) -> None:
    await pool.execute(
        "UPDATE vault_intent_rules SET is_active = NOT is_active "
        "WHERE id=$1 AND owner_id=$2", rule_id, owner_id)


async def seed_default_rules(pool: asyncpg.Pool, owner_id: int) -> int:
    """Включить рекомендованный набор. Идемпотентно: не плодит дубли по фразе."""
    existing = {r["phrase"] for r in await pool.fetch(
        "SELECT phrase FROM vault_intent_rules WHERE owner_id=$1", owner_id)}
    added = 0
    for r in DEFAULT_RULES:
        if r["phrase"] in existing:
            continue
        await pool.execute(
            "INSERT INTO vault_intent_rules(owner_id, phrase, stage, tag, notify) "
            "VALUES($1,$2,$3,$4,$5)",
            owner_id, r["phrase"], r["stage"], r["tag"], r["notify"])
        added += 1
    return added


async def _ensure_contact(pool: asyncpg.Pool, owner_id: int, peer: dict) -> str | None:
    """Найти контакт по telegram_user_id или создать. Возвращает РЕАЛЬНЫЙ id
    (upsert_contact на конфликте отдаёт новый uuid — нам нужен существующий)."""
    pid = peer.get("peer_user_id")
    if not pid:
        return None
    row = await pool.fetchrow(
        "SELECT id FROM unified_contacts WHERE owner_id=$1 AND telegram_user_id=$2",
        owner_id, pid)
    if row:
        return row["id"]
    # upsert_contact теперь возвращает РЕАЛЬНЫЙ id (RETURNING id) — доверяем ему.
    from services.contacts_hub.repository import upsert_contact
    return await upsert_contact(pool, owner_id, {
        "telegram_user_id": pid, "username": peer.get("peer_username"),
        "first_name": peer.get("peer_name"), "display_name": peer.get("peer_name"),
        "discovered_at": dt.datetime.now(dt.timezone.utc),
    })


# Частицы отрицания перед фразой. Подстрочное совпадение без этой проверки
# трактовало «не готов» как готовность купить и двигало клиента в «Переговоры» —
# ровно наоборот сказанному. Фраза «готов» входит в рекомендованный набор, так
# что срабатывало это постоянно.
_NEGATIONS = frozenset({"не", "нет", "без", "никогда", "вряд"})
_WORD_BEFORE_RE = _re.compile(r"([а-яёa-z]+)[\s,]*$")


def _negated_before(low: str, idx: int) -> bool:
    """Стоит ли прямо перед совпадением частица отрицания?

    Сравниваем ЦЕЛОЕ предыдущее слово, а не хвост строки: иначе «мне» в
    «мне не интересно» сам выглядел бы как «не» и глушил бы верное совпадение.
    """
    if idx <= 0:
        return False
    m = _WORD_BEFORE_RE.search(low[:idx])
    return bool(m and m.group(1) in _NEGATIONS)


def _contains(needle: str, low: str) -> bool:
    """Вхождение подстроки, не считая случаев с отрицанием перед ней.

    Если сама фраза начинается с отрицания («не интересно»), проверку не
    применяем — отрицание в ней и есть смысл правила.
    """
    if not needle:
        return False
    first = needle.split(None, 1)[0] if needle.split() else ""
    self_negated = first in _NEGATIONS
    start = 0
    while True:
        idx = low.find(needle, start)
        if idx < 0:
            return False
        if self_negated or not _negated_before(low, idx):
            return True
        start = idx + 1        # это вхождение отрицали — ищем следующее


def _phrase_matches(phrase: str, low: str) -> bool:
    """Совпадает ли правило с текстом (уже в нижнем регистре)?

    Три формы фразы (обратная совместимость сохранена):
      • обычная подстрока — «цена»
      • несколько альтернатив через | — «цена|стоимость|прайс» (любая)
      • регэксп с префиксом re: — «re:\\d{4,}\\s*руб» (гибкие шаблоны)
    Битый регэксп никогда не роняет сканер — просто не матчит.

    Подстрочные формы не срабатывают, если прямо перед фразой стоит отрицание
    («не готов» ≠ «готов»). На регэксп-правила это не распространяется: там
    автор выражает условие сам.
    """
    p = (phrase or "").strip()
    if not p:
        return False
    if p[:3].lower() == "re:":
        pat = p[3:].strip()
        if not pat:
            return False
        try:
            return _re.search(pat, low, _re.IGNORECASE) is not None
        except _re.error:
            return False
    if "|" in p:
        return any(_contains(a.strip().lower(), low) for a in p.split("|") if a.strip())
    return _contains(p.lower(), low)


def _match(text: str, rules: list[dict]) -> list[dict]:
    low = (text or "").lower()
    return [r for r in rules if _phrase_matches(r.get("phrase", ""), low)]


async def scan_incoming(pool: asyncpg.Pool, bot, owner_id: int, peer: dict,
                        text: str, notifier=None) -> dict:
    """Обработать одно ВХОДЯЩЕЕ сообщение. Возвращает {matched, stage, tags,
    notified} (для тестов). notifier — seam отправки алерта (по умолчанию bot).
    Тихо ничего не делает, если правил нет или совпадений нет."""
    if not text:
        return {"matched": 0}
    rules = await pool.fetch(
        "SELECT id, phrase, stage, tag, notify FROM vault_intent_rules "
        "WHERE owner_id=$1 AND is_active=TRUE", owner_id)
    rules = [dict(r) for r in rules]
    matched = _match(text, rules)
    if not matched:
        return {"matched": 0}

    contact_id = await _ensure_contact(pool, owner_id, peer)
    if not contact_id:
        return {"matched": len(matched), "error": "no_contact"}

    new_tags = [m["tag"] for m in matched if m["tag"]]
    want_notify = any(m["notify"] for m in matched)

    # Текущее состояние — чтобы менять и уведомлять только по факту изменения.
    cur = await pool.fetchrow(
        "SELECT tags FROM unified_contacts WHERE id=$1", contact_id)
    cur_tags = set(cur["tags"] or []) if cur else set()
    cur_stage = await pool.fetchval(
        "SELECT stage FROM contact_crm WHERE owner_id=$1 AND contact_id=$2",
        owner_id, contact_id)

    added_tags = [t for t in dict.fromkeys(new_tags) if t not in cur_tags]
    # Движение стадии решает чистая функция: только вперёд по воронке, отказ не
    # отменяет закрытую сделку. Иначе любая фраза перекидывала клиента куда
    # попало, в том числе назад.
    target_stage = decide_stage(cur_stage, [m["stage"] for m in matched if m["stage"]])
    stage_changed = bool(target_stage)

    if added_tags:
        await pool.execute(
            "UPDATE unified_contacts SET tags = ARRAY(SELECT DISTINCT unnest("
            "COALESCE(tags,'{}'::text[]) || $2::text[])), updated_at=NOW() "
            "WHERE id=$1 AND owner_id=$3", contact_id, added_tags, owner_id)
    if stage_changed:
        from services.contacts_hub import crm_engine
        await crm_engine.upsert_crm(pool, owner_id, contact_id, {
            "stage": target_stage,
            "last_interaction_type": "intent",
            "last_message_preview": text[:200],
            "last_interaction_at": dt.datetime.now(dt.timezone.utc),
        })
    # Счётчик срабатываний
    await pool.execute(
        "UPDATE vault_intent_rules SET hits = hits + 1 WHERE id = ANY($1::bigint[])",
        [m["id"] for m in matched])
    # Журнал: голый счётчик не позволял понять, НА ЧТО сработало правило, и
    # ложные срабатывания оставались невидимыми — контакт молча уезжал не в ту
    # стадию. Fail-open: сбой записи журнала не должен ломать саму обработку.
    try:
        await pool.executemany(
            """INSERT INTO vault_intent_hits
                   (owner_id, rule_id, chat_id, peer_name, text_preview, stage_to, tags_added)
               VALUES ($1,$2,$3,$4,$5,$6,$7)""",
            [(owner_id, m["id"], peer.get("peer_user_id"),
              peer.get("peer_name") or peer.get("peer_username"),
              (text or "")[:300], target_stage if stage_changed else None, added_tags)
             for m in matched])
    except Exception:
        log.debug("intent_sensor: журнал срабатываний не записан owner=%s", owner_id)

    notified = False
    if want_notify and (added_tags or stage_changed):
        who = peer.get("peer_name") or peer.get("peer_username") or "Собеседник"
        parts = []
        if stage_changed:
            parts.append(f"стадия → <b>{_stage_label(target_stage)}</b>")
        if added_tags:
            parts.append("теги: " + ", ".join(f"<b>{t}</b>" for t in added_tags))
        msg = (f"🎯 <b>Намерение: {_esc(who)}</b>\n"
               f"«{_esc(text[:160])}»\n" + " · ".join(parts))
        try:
            if notifier:
                await notifier(owner_id, msg)
            else:
                chat_id = await _notify_chat(pool, owner_id)
                if chat_id:
                    await bot.send_message(chat_id, msg, parse_mode="HTML")
            notified = True
        except Exception:
            log.debug("intent_sensor: notify failed owner=%s", owner_id)

    # Событие в общую шину организма (память + реакция мозга).
    try:
        from services.organism import spine
        await spine.emit(pool, owner_id, "intent", {
            "contact_id": str(contact_id),
            "peer": peer.get("peer_name") or peer.get("peer_username"),
            "stage": target_stage if stage_changed else None,
            "tags": added_tags, "text": (text or "")[:160]})
    except Exception:
        pass

    # Virtual Layer: намерение — это сигнал воронки. Раскладываем CRM-стадию в
    # сигнал состояния; переход родит виртуальное событие (purchase_intent_
    # detected / user_lost_interest), на которое реагируют автоматизации. Это
    # НАДСТРОЙКА над стадией: у состояния есть уверенность и распад, которых у
    # тега нет. Fail-open — сбой слоя не ломает обработку сообщения.
    if stage_changed:
        _sig = {"proposal": "asked_price", "negotiation": "asked_how_to_pay",
                "lead": "replied", "lost": "refused"}.get(target_stage)
        if _sig:
            try:
                from services import virtual_layer
                await virtual_layer.signal(
                    pool, owner_id, virtual_layer.USER, contact_id, _sig,
                    confidence=0.7, source="intent_sensor")
            except Exception:
                log.debug("intent_sensor: virtual_layer signal failed owner=%s", owner_id)

    return {"matched": len(matched), "stage": target_stage if stage_changed else None,
            "tags": added_tags, "notified": notified, "contact_id": contact_id}


async def _notify_chat(pool: asyncpg.Pool, owner_id: int):
    row = await pool.fetchrow(
        "SELECT user_chat_id FROM business_connections WHERE owner_id=$1 "
        "AND is_enabled=TRUE ORDER BY updated_at DESC LIMIT 1", owner_id)
    return (row["user_chat_id"] if row else None) or owner_id


def _stage_label(s: str) -> str:
    return {"lead": "Лид", "contact": "Контакт", "proposal": "Предложение",
            "negotiation": "Переговоры", "won": "Выиграно", "lost": "Проиграно"}.get(s, s)


def _esc(t: str) -> str:
    return (str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
