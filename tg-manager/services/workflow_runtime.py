"""Исполнитель сценариев (воркфлоу).

До этого модуля сценарий был только схемой: «запуск» заводил строку
`workflow_runs` со статусом running, и дальше её не трогал никто. Здесь
сценарий исполняется по-настоящему, для конкретного человека в конкретном
боте (`bot_id`, `chat_id`).

КАК ЗАПУСКАЕТСЯ ПРОГОН
  * по событию шины (`services/automation_events.py`): /start, слово в
    сообщении, тег, любое событие по типу — см. `trigger_matches`;
  * вручную из мини-аппа — тестовый прогон на самом владельце или на
    указанном подписчике (`start_run`).

КАК ИДЁТ ПРОГОН
  Цикл `run` раз в несколько секунд забирает прогоны, у которых подошло
  `next_run_at`, и проходит шаги подряд, пока не встретит паузу (delay),
  ожидание (кнопки, вопрос, счёт) или конец.

  Каждый шаг с внешним действием сначала ЗАНИМАЕТСЯ условным UPDATE по
  счётчику `exec_seq`, и только потом выполняется. Два исполнителя на одном
  прогоне (две реплики, повтор после падения посреди шага) не отправят одно
  сообщение дважды: второй не пройдёт условие. Цена — «не больше одного раза»:
  если процесс упал между занятием шага и отправкой, это сообщение пропадёт.
  Для рассылки людям это верный выбор: дубль — жалоба на спам, пропуск —
  одно недошедшее сообщение.

ОЖИДАНИЕ ЧЕЛОВЕКА
  Кнопки-ветки, вопрос и счёт переводят прогон в `waiting`. Ответ приходит
  через опрос бота (`services/auto_responder.py`), который зовёт здесь
  `on_button`, `on_reply`, `on_payment` ДО остальной логики бота: ответ на
  вопрос сценария не должен получить ещё и ответ ИИ-продавца или правила.

Внешний текст (ответы людей) — данные: он экранируется перед подстановкой в
HTML-сообщение и никогда не исполняется.
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import random
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import aiohttp

from services import automation_events as bus

log = logging.getLogger(__name__)

CONSUMER = "workflow_runtime"
_TICK_SECONDS = 3
_BATCH = 50
# Шагов за один заход по прогону: сценарий без пауз из 50 шагов пройдёт за
# два захода, а сценарий, зациклившийся переходами, не займёт цикл навсегда.
_STEPS_PER_PASS = 25
# Всего исполненных шагов на прогон: переходы позволяют сделать цикл, и без
# потолка зациклившийся сценарий слал бы сообщения бесконечно.
_MAX_EXEC_PER_RUN = 300
_CLAIM_SECONDS = 120
_FAIL_LIMIT = 5
_BUTTON_WAIT_DEFAULT_MIN = 7 * 24 * 60
_MAX_ASK_ATTEMPTS = 3
CALLBACK_PREFIX = "wf:"

# Будильник исполнителя: опрос бота, получив нажатие или ответ, будит цикл,
# чтобы человек не ждал следующего тика. В пределах одного процесса.
_wake = asyncio.Event()

# Синонимы старого словаря условий (шаблон «Реакция на событие»).
_EVENT_ALIASES = {"new_subscriber": "contact.started", "start": "contact.started"}

TRIGGER_TYPES: dict[str, str] = {
    "manual": "Только вручную",
    "start": "Человек открыл бота (/start)",
    "keyword": "Сообщение со словом",
    "tag_added": "Человеку поставили тег",
    "event": "Событие",
}


def wake() -> None:
    """Разбудить цикл исполнителя (нажатие, ответ, оплата)."""
    _wake.set()


# ── Чистые помощники ────────────────────────────────────────────────────────

def _as_list(raw) -> list:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return []
    return raw if isinstance(raw, list) else []


def _as_dict(raw) -> dict:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return {}
    return raw if isinstance(raw, dict) else {}


def trigger_matches(wf: dict, event: dict) -> bool:
    """Подходит ли событие под триггер сценария. ЧИСТАЯ.

    Сценарий, привязанный к боту, слушает только события своего бота: иначе
    /start в одном боте запускал бы приветствие из другого.
    """
    ttype = (wf.get("trigger_type") or "").strip().lower()
    if not ttype or ttype == "manual":
        return False
    wf_bot = wf.get("bot_id")
    ev_bot = event.get("bot_id")
    if wf_bot and ev_bot and int(wf_bot) != int(ev_bot):
        return False
    value = (wf.get("trigger_value") or "").strip()
    etype = event.get("event_type") or ""
    data = event.get("data") or {}
    if ttype == "start":
        if etype != "contact.started":
            return False
        return not value or value == (data.get("param") or "")
    if ttype == "keyword":
        if etype != "message.received" or not value:
            return False
        text = (data.get("text") or "").lower()
        words = [w.strip().lower() for w in value.split(",") if w.strip()]
        return any(w in text for w in words)
    if ttype == "tag_added":
        if etype != "contact.tag_added" or not value:
            return False
        return (data.get("tag") or "").strip().lower() == value.lower()
    if ttype == "event":
        return bool(value) and etype == _EVENT_ALIASES.get(value, value)
    return False


def validate_trigger(trigger_type, trigger_value) -> tuple[str | None, str | None]:
    """Нормализовать триггер с экрана. ValueError с русским текстом."""
    ttype = str(trigger_type or "manual").strip().lower()
    if ttype not in TRIGGER_TYPES:
        raise ValueError("Неизвестный способ запуска сценария")
    value = str(trigger_value or "").strip()[:200]
    if ttype in ("keyword", "tag_added", "event") and not value:
        raise ValueError(f"Для запуска «{TRIGGER_TYPES[ttype]}» нужно значение")
    if ttype == "manual":
        return None, None
    return ttype, (value or None)


# ── Цель сценария ───────────────────────────────────────────────────────────
# Цель — то, ради чего сценарий существует: человек купил, поставил тег
# «клиент», сделка выиграна. Достигнута — прогон завершается сам, и дожим
# больше не идёт. Раньше сценарий не знал цели и слал «напоминаем об оплате»
# человеку, который уже оплатил.
GOAL_TYPES: dict[str, str] = {
    "paid": "Человек оплатил",
    "tag_added": "Человеку поставили тег",
    "event": "Произошло событие",
}


def validate_goal(goal_type, goal_value) -> tuple[str | None, str | None]:
    """Нормализовать цель с экрана. (None, None) — цели нет. ValueError."""
    gtype = str(goal_type or "").strip().lower()
    if not gtype or gtype == "none":
        return None, None
    if gtype not in GOAL_TYPES:
        raise ValueError("Неизвестная цель сценария")
    value = str(goal_value or "").strip()[:200]
    if gtype in ("tag_added", "event") and not value:
        raise ValueError(f"Для цели «{GOAL_TYPES[gtype]}» нужно значение")
    return gtype, (value or None)


def goal_matches(goal_type: str | None, goal_value: str | None,
                 event: dict) -> bool:
    """Достигнута ли цель этим событием. ЧИСТАЯ."""
    gtype = (goal_type or "").strip().lower()
    etype = event.get("event_type") or ""
    data = event.get("data") or {}
    value = (goal_value or "").strip()
    if gtype == "paid":
        return etype == "payment.received"
    if gtype == "tag_added":
        return (etype == "contact.tag_added" and bool(value)
                and (data.get("tag") or "").strip().lower() == value.lower())
    if gtype == "event":
        return bool(value) and etype == _EVENT_ALIASES.get(value, value)
    return False


# ── Умная задержка: «до ближайшего будня 10:00 по времени человека» ─────────
# Часовой пояс человека Telegram не сообщает. Берём по порядку: поле
# контакта `timezone` (сценарий может спросить), язык клиента Telegram
# (ru — Москва, uk — Киев …), часовой пояс шага, Москва.
_LANG_TZ = {
    "ru": "Europe/Moscow", "uk": "Europe/Kyiv", "be": "Europe/Minsk",
    "kk": "Asia/Almaty", "uz": "Asia/Tashkent", "ky": "Asia/Bishkek",
    "hy": "Asia/Yerevan", "ka": "Asia/Tbilisi", "az": "Asia/Baku",
    "tg": "Asia/Dushanbe", "pl": "Europe/Warsaw", "de": "Europe/Berlin",
    "lv": "Europe/Riga", "lt": "Europe/Vilnius", "et": "Europe/Tallinn",
}
DEFAULT_TZ = "Europe/Moscow"
_HHMM_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def _zone(name: str | None):
    if not name:
        return None
    try:
        return ZoneInfo(str(name).strip())
    except Exception:
        return None


def contact_tz(fields: dict | None, language_code: str | None,
               step_tz: str | None = None) -> str:
    """IANA-зона человека для умной задержки. ЧИСТАЯ."""
    own = (fields or {}).get("timezone")
    if own and _zone(own):
        return str(own).strip()
    lang = (language_code or "").split("-")[0].strip().lower()
    if lang in _LANG_TZ:
        return _LANG_TZ[lang]
    if step_tz and _zone(step_tz):
        return step_tz
    return DEFAULT_TZ


def next_window(now_utc: datetime, at: str, weekdays_only: bool,
                tz_name: str) -> datetime:
    """Ближайший момент «в HH:MM по зоне tz_name» (только будни, если
    weekdays_only) строго позже now_utc. Возвращает UTC. ЧИСТАЯ."""
    m = _HHMM_RE.match((at or "").strip())
    if not m:
        raise ValueError("время — в виде ЧЧ:ММ")
    hh, mm = int(m.group(1)), int(m.group(2))
    zone = _zone(tz_name) or ZoneInfo(DEFAULT_TZ)
    local = now_utc.astimezone(zone)
    cand = local.replace(hour=hh, minute=mm, second=0, microsecond=0)
    for _ in range(8):
        if cand > local and (not weekdays_only or cand.weekday() < 5):
            return cand.astimezone(timezone.utc)
        cand = (cand + timedelta(days=1)).replace(hour=hh, minute=mm)
    return cand.astimezone(timezone.utc)


def pick_split(branches: list[dict], rnd: float | None = None) -> int:
    """Номер ветки A/B-деления по весам. ЧИСТАЯ при заданном rnd."""
    weights = [max(0, int(b.get("weight") or 0)) for b in branches]
    total = sum(weights)
    if total <= 0:
        return 0
    x = (random.random() if rnd is None else rnd) * total
    acc = 0
    for i, w in enumerate(weights):
        acc += w
        if x < acc:
            return i
    return len(weights) - 1


_PHONE_RE = re.compile(r"^\+?\d[\d\s\-()]{8,18}\d$")
_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[a-zA-Zа-яА-Я]{2,24}$")
_NUMBER_RE = re.compile(r"^-?\d{1,15}([.,]\d{1,6})?$")

_ASK_HINTS = {
    "phone": "Похоже, это не номер телефона. Напишите номер цифрами, например +79001234567.",
    "email": "Похоже, это не адрес почты. Напишите его в виде name@example.com.",
    "number": "Нужно число. Напишите его цифрами.",
}


def check_answer(kind: str, text: str) -> str | None:
    """Нормализованный ответ или None, если ответ не прошёл проверку. ЧИСТАЯ."""
    t = (text or "").strip()
    if not t:
        return None
    if kind == "phone":
        if not _PHONE_RE.match(t):
            return None
        digits = re.sub(r"\D", "", t)
        return ("+" + digits) if t.startswith("+") or len(digits) > 10 else digits
    if kind == "email":
        return t.lower() if _EMAIL_RE.match(t) else None
    if kind == "number":
        return t.replace(",", ".") if _NUMBER_RE.match(t) else None
    return t[:1000]


def render(text: str, contact: dict | None, fields: dict, run_vars: dict) -> str:
    """Подставить {{ИМЯ}} и {{поле}} в текст шага. ЧИСТАЯ.

    Текст шага пишет владелец, и HTML в нём разрешён (как во всех текстах
    ботов продукта). Значения подстановок пришли от человека в чате — они
    экранируются: ответ «<b>» не должен ни сломать разметку, ни стать ею.
    """
    if not text or "{{" not in text:
        return text
    c = contact or {}
    first = c.get("first_name") or ""
    last = c.get("last_name") or ""
    uname = c.get("username") or ""
    now = datetime.now()
    values: dict[str, str] = {
        "FIRST_NAME": first,
        "LAST_NAME": last,
        "FULL_NAME": f"{first} {last}".strip(),
        "USERNAME": f"@{uname}" if uname else first,
        "DATE": now.strftime("%d.%m.%Y"),
        "TIME": now.strftime("%H:%M"),
    }
    for src in (fields or {}, run_vars or {}):
        for k, v in src.items():
            if isinstance(k, str) and not k.startswith("_") and v is not None:
                values.setdefault(k, str(v))

    def _sub(m: re.Match) -> str:
        key = m.group(1)
        if key in values:
            return html.escape(values[key], quote=False)
        return m.group(0)

    return re.sub(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}", _sub, text)


def button_markup(run_id: int, seq: int, buttons: list[dict]) -> dict | None:
    """Inline-клавиатура шага. Кнопка-ссылка — url, кнопка-ветка — callback
    `wf:<прогон>:<счётчик>:<номер кнопки>` (до 64 байт, предел Telegram).
    Счётчик в данных кнопки отсекает нажатие на кнопку из СТАРОГО сообщения
    того же прогона: после перехода назад она вела бы не туда. ЧИСТАЯ."""
    rows = []
    for i, b in enumerate(buttons or []):
        if b.get("url"):
            rows.append([{"text": b["text"], "url": b["url"]}])
        else:
            rows.append([{"text": b["text"],
                          "callback_data": f"{CALLBACK_PREFIX}{run_id}:{seq}:{i}"}])
    return {"inline_keyboard": rows} if rows else None


def parse_callback(data: str) -> tuple[int, int, int] | None:
    """(run_id, seq, button) из callback_data или None. ЧИСТАЯ."""
    if not data or not data.startswith(CALLBACK_PREFIX):
        return None
    parts = data[len(CALLBACK_PREFIX):].split(":")
    if len(parts) != 3:
        return None
    try:
        return int(parts[0]), int(parts[1]), int(parts[2])
    except ValueError:
        return None


def invoice_payload(run_id: int, seq: int) -> str:
    return f"{CALLBACK_PREFIX}{run_id}:{seq}"


def parse_invoice_payload(payload: str) -> tuple[int, int] | None:
    if not payload or not payload.startswith(CALLBACK_PREFIX):
        return None
    parts = payload[len(CALLBACK_PREFIX):].split(":")
    if len(parts) != 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None


def _goto_index(step: dict, key: str) -> int | None:
    """Номер шага на экране (с единицы) → индекс в списке."""
    n = step.get(key)
    try:
        n = int(n)
    except (TypeError, ValueError):
        return None
    return n - 1 if n >= 1 else None


# ── Запуск прогона ──────────────────────────────────────────────────────────

async def start_run(pool, wf: dict, bot_id: int | None, chat_id: int | None, *,
                    input_data: dict | None = None,
                    trigger_event_id: int | None = None,
                    force: bool = False,
                    source: str | None = None) -> int | None:
    """Завести прогон сценария для человека. id прогона или None.

    None — человек уже проходит этот сценарий (живой прогон есть) или уже
    прошёл его, а повторный вход выключен. `force` — ручной тестовый прогон:
    владелец проверяет сценарий на себе и вправе пройти его сколько угодно
    раз (но не два параллельно).
    """
    steps = _as_list(wf.get("steps"))
    if chat_id and not force and not wf.get("allow_reentry"):
        seen = await pool.fetchval(
            """SELECT 1 FROM workflow_runs
                WHERE workflow_id=$1 AND bot_id IS NOT DISTINCT FROM $2::bigint
                  AND chat_id=$3 AND status <> 'cancelled' LIMIT 1""",
            wf["id"], bot_id, chat_id)
        if seen:
            return None
    if source is None and bot_id and chat_id:
        # Источник — первое касание человека (параметр ссылки /start),
        # записанный в поле контакта: до оплаты он едет вместе с прогоном.
        try:
            source = await pool.fetchval(
                "SELECT fields->>'source' FROM bot_users WHERE bot_id=$1 AND user_id=$2",
                bot_id, chat_id)
        except Exception:
            source = None
    run_id = await pool.fetchval(
        """INSERT INTO workflow_runs
               (owner_id, workflow_id, status, input_data, current_step,
                total_steps, started_at, bot_id, chat_id, steps_snapshot,
                vars, next_run_at, trigger_event_id, source)
           VALUES ($1, $2, 'running', $3::jsonb, 0, $4, now(), $5, $6,
                   $7::jsonb, '{}'::jsonb, now(), $8, $9)
           ON CONFLICT (workflow_id, bot_id, chat_id)
              WHERE status IN ('running', 'waiting') AND chat_id IS NOT NULL
           DO NOTHING
           RETURNING id""",
        wf["owner_id"], wf["id"], json.dumps(input_data or {}, default=str),
        len(steps), bot_id, chat_id, json.dumps(steps), trigger_event_id,
        (source or None) and str(source)[:64])
    if run_id:
        wake()
    return run_id


async def _remember_source(pool, ev: dict) -> None:
    """Первое касание: параметр ссылки /start → поле контакта `source`.

    Только если поля ещё нет: человек, пришедший по рекламе, а через неделю
    открывший бота по ссылке из канала, остаётся «из рекламы»."""
    param = ((ev.get("data") or {}).get("param") or "").strip()[:64]
    if not param or not ev.get("bot_id") or not ev.get("chat_id"):
        return
    await pool.execute(
        """UPDATE bot_users
              SET fields = COALESCE(fields, '{}'::jsonb) || jsonb_build_object('source', $3::text)
            WHERE bot_id=$1 AND user_id=$2
              AND NOT (COALESCE(fields, '{}'::jsonb) ? 'source')""",
        ev["bot_id"], ev["chat_id"], param)


async def _check_goals(pool, ev: dict) -> int:
    """Событие достигло цели живых прогонов этого человека — завершить их.

    Только прогоны того же бота и человека: оплата одного клиента не
    закрывает дожим другого."""
    if not ev.get("chat_id"):
        return 0
    rows = await pool.fetch(
        """SELECT r.*, d.goal_type, d.goal_value
             FROM workflow_runs r
             JOIN workflow_definitions d ON d.id = r.workflow_id
            WHERE r.owner_id=$1 AND r.chat_id=$2
              AND r.bot_id IS NOT DISTINCT FROM $3::bigint
              AND r.status IN ('running', 'waiting')
              AND d.goal_type IS NOT NULL""",
        ev["owner_id"], ev["chat_id"], ev.get("bot_id"))
    n = 0
    for r in rows:
        run = dict(r)
        if not goal_matches(run.get("goal_type"), run.get("goal_value"), ev):
            continue
        res = await pool.execute(
            """UPDATE workflow_runs
                  SET status='completed', finished_at=now(), goal_reached_at=now(),
                      error_message='Цель достигнута', wait_kind=NULL,
                      wait_until=NULL, claimed_until=NULL
                WHERE id=$1 AND status IN ('running', 'waiting')""", run["id"])
        if isinstance(res, str) and res.endswith(" 1"):
            n += 1
            await _log(pool, run, int(run.get("current_step") or 0), "goal",
                       "goal", ev.get("event_type"))
            await bus.emit(pool, run["owner_id"], "workflow.completed",
                           {"workflow_id": run.get("workflow_id"), "run_id": run["id"],
                            "goal": True},
                           bot_id=run.get("bot_id"), chat_id=run.get("chat_id"))
    return n


async def _log(pool, run: dict, idx: int, step_type: str, outcome: str,
               detail: str | None = None, *, amount: int | None = None,
               currency: str | None = None) -> None:
    """Строка журнала шага. Не роняет прогон: журнал — для владельца, а не
    для исполнения, и его сбой не должен останавливать сообщения людям."""
    try:
        await pool.execute(
            """INSERT INTO workflow_step_log
                   (run_id, workflow_id, owner_id, step_idx, step_type, outcome,
                    detail, amount, currency)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)""",
            run["id"], run.get("workflow_id") or 0, run["owner_id"], int(idx),
            (step_type or "?")[:32], outcome[:32],
            (detail or None) and str(detail)[:300], amount, currency)
    except Exception:
        log.debug("workflow_runtime: журнал шага прогона %s", run.get("id"),
                  exc_info=True)


async def dispatch_events(pool) -> int:
    """Прочитать новые события шины и запустить сценарии с подходящим
    триггером. Возвращает число заведённых прогонов."""
    last = await bus.get_cursor(pool, CONSUMER)
    events = await bus.read_since(pool, last)
    if not events:
        return 0
    started = 0
    owners = {e["owner_id"] for e in events}
    rows = await pool.fetch(
        """SELECT id, owner_id, bot_id, steps, trigger_type, trigger_value,
                  allow_reentry, name
             FROM workflow_definitions
            WHERE owner_id = ANY($1::bigint[]) AND is_active
              AND trigger_type IS NOT NULL AND trigger_type <> 'manual'""",
        list(owners))
    by_owner: dict[int, list[dict]] = {}
    for r in rows:
        by_owner.setdefault(r["owner_id"], []).append(dict(r))
    for ev in events:
        try:
            if ev["event_type"] == "contact.started":
                await _remember_source(pool, ev)
            await _check_goals(pool, ev)
        except Exception:
            log.exception("workflow_runtime: цель/источник по событию %s", ev["id"])
        for wf in by_owner.get(ev["owner_id"], []):
            if not trigger_matches(wf, ev):
                continue
            bot_id = ev.get("bot_id") or wf.get("bot_id")
            try:
                param = ((ev.get("data") or {}).get("param") or "") \
                    if ev["event_type"] == "contact.started" else ""
                rid = await start_run(
                    pool, wf, bot_id, ev.get("chat_id"),
                    input_data={"event_type": ev["event_type"],
                                "event": ev.get("data") or {}},
                    trigger_event_id=ev["id"], source=param or None)
            except Exception:
                log.exception("workflow_runtime: запуск сценария %s по событию %s",
                              wf["id"], ev["id"])
                rid = None
            if rid:
                started += 1
        await bus.advance_cursor(pool, CONSUMER, ev["id"])
    return started


# ── Исполнение ──────────────────────────────────────────────────────────────

async def _claim_due(pool) -> list[dict]:
    rows = await pool.fetch(
        """UPDATE workflow_runs r
              SET claimed_until = now() + make_interval(secs => $2)
            WHERE r.id IN (
                  SELECT id FROM workflow_runs
                   WHERE status = 'running'
                     AND next_run_at <= now()
                     AND (claimed_until IS NULL OR claimed_until < now())
                     -- Воронка, выключенная в своём разделе, стоит на паузе
                     -- целиком, как и у старого исполнителя
                     -- (services/legacy_funnels.py).
                     AND NOT EXISTS (
                         SELECT 1 FROM workflow_definitions d
                          WHERE d.id = workflow_runs.workflow_id
                            AND d.legacy_kind IS NOT NULL
                            AND d.is_active IS NOT TRUE)
                   ORDER BY next_run_at
                   LIMIT $1
                   FOR UPDATE SKIP LOCKED)
        RETURNING r.*""",
        _BATCH, _CLAIM_SECONDS)
    return [dict(r) for r in rows]


async def _take_step(pool, run_id: int, seq: int, **sets) -> bool:
    """Занять шаг: сдвинуть счётчик и записать новое состояние ОДНИМ условным
    UPDATE. False — прогон уже сдвинул кто-то другой (или его отменили)."""
    cols = ["exec_seq = exec_seq + 1"]
    args: list[Any] = [run_id, seq]
    for k, v in sets.items():
        args.append(json.dumps(v, default=str) if k == "vars" else v)
        cast = "::jsonb" if k == "vars" else ""
        cols.append(f"{k} = ${len(args)}{cast}")
    res = await pool.execute(
        f"UPDATE workflow_runs SET {', '.join(cols)} "
        "WHERE id=$1 AND exec_seq=$2 AND status='running'", *args)
    return isinstance(res, str) and res.rsplit(" ", 1)[-1] == "1"


async def _finish(pool, run: dict, status: str, note: str | None = None,
                  vars_: dict | None = None) -> None:
    await pool.execute(
        """UPDATE workflow_runs
              SET status=$2, finished_at=now(), error_message=$3,
                  wait_kind=NULL, wait_until=NULL, claimed_until=NULL,
                  vars=COALESCE($4::jsonb, vars)
            WHERE id=$1 AND status IN ('running', 'waiting')""",
        run["id"], status, note,
        json.dumps(vars_, default=str) if vars_ is not None else None)
    steps = _as_list(run.get("steps_snapshot"))
    idx = int(run.get("current_step") or 0)
    stype = steps[idx].get("type", "?") if 0 <= idx < len(steps) else "end"
    await _log(pool, run, idx, stype, status, note)
    if status == "completed":
        await bus.emit(pool, run["owner_id"], "workflow.completed",
                       {"workflow_id": run.get("workflow_id"), "run_id": run["id"]},
                       bot_id=run.get("bot_id"), chat_id=run.get("chat_id"))


async def _bot_token(pool, bot_id: int | None) -> str | None:
    if not bot_id:
        return None
    from database import db
    row = await db.fetchrow_bot(
        pool, "SELECT token FROM managed_bots WHERE bot_id=$1 AND is_active=TRUE",
        bot_id)
    return (row or {}).get("token")


async def _contact(pool, bot_id, chat_id) -> tuple[dict, dict]:
    if not bot_id or not chat_id:
        return {}, {}
    row = await pool.fetchrow(
        """SELECT username, first_name, last_name, last_seen, fields, language_code
             FROM bot_users WHERE bot_id=$1 AND user_id=$2""", bot_id, chat_id)
    if not row:
        return {}, {}
    d = dict(row)
    return d, _as_dict(d.pop("fields", None))


async def _set_field(pool, bot_id, chat_id, name: str, value: str,
                     owner_id: int | None) -> None:
    if not bot_id or not chat_id:
        return
    await pool.execute(
        """UPDATE bot_users SET fields = COALESCE(fields, '{}'::jsonb)
                                         || jsonb_build_object($3::text, $4::text)
            WHERE bot_id=$1 AND user_id=$2""", bot_id, chat_id, name, value)
    if name == "phone":
        await pool.execute(
            "UPDATE bot_users SET phone=$3 WHERE bot_id=$1 AND user_id=$2",
            bot_id, chat_id, value)
    await bus.emit(pool, owner_id, "contact.field_set",
                   {"field": name, "value": value}, bot_id=bot_id, chat_id=chat_id)


async def evaluate(pool, http, token, run: dict, step: dict,
                   contact: dict, fields: dict) -> bool:
    """Проверить условие шага для человека из прогона."""
    cond = step.get("condition") or ""
    value = (step.get("condition_value") or "").strip()
    bot_id, chat_id = run.get("bot_id"), run.get("chat_id")
    run_vars = _as_dict(run.get("vars"))
    if cond == "event":
        inp = _as_dict(run.get("input_data"))
        want = _EVENT_ALIASES.get(value, value)
        return bool(want) and inp.get("event_type") == want
    if cond == "paid":
        return bool(run_vars.get("paid"))
    if cond == "clicked":
        return (str(run_vars.get("last_button") or "").strip().lower()
                == value.lower())
    if cond in ("field_equals", "field_set"):
        name, _, want = value.partition("=")
        name = name.strip().lower()
        have = fields.get(name, run_vars.get(name))
        if cond == "field_set":
            return have not in (None, "")
        return str(have or "").strip().lower() == want.strip().lower()
    if not bot_id or not chat_id:
        return False
    if cond in ("tag_has", "has_tag", "tag_not"):
        has = bool(await pool.fetchval(
            "SELECT 1 FROM user_tags WHERE bot_id=$1 AND user_id=$2 "
            "AND lower(tag)=lower($3) LIMIT 1", bot_id, chat_id, value))
        return (not has) if cond == "tag_not" else has
    if cond == "inactive_days":
        try:
            days = int(value)
        except ValueError:
            return False
        seen = contact.get("last_seen")
        if not seen:
            return True
        return seen < datetime.now(timezone.utc) - timedelta(days=days)
    if cond == "subscribed":
        if not token:
            return False
        from services import bot_api
        channel = value if value.startswith(("@", "-")) else "@" + value
        data = await bot_api._call(http, token, "getChatMember",
                                   chat_id=channel, user_id=chat_id)
        st = ((data or {}).get("result") or {}).get("status")
        member = ((data or {}).get("result") or {}).get("is_member")
        return st in ("member", "administrator", "creator") or (
            st == "restricted" and bool(member))
    return False


async def _do_webhook(http, run: dict, step: dict, contact: dict,
                      fields: dict) -> int | None:
    """POST на адрес шага. Код ответа или None. Адрес проверяется на
    внутренние сети при КАЖДОМ вызове: DNS имени могли поменять после
    сохранения шага."""
    from services.security import resolve_url_is_public
    url = step.get("url") or ""
    if not await resolve_url_is_public(url):
        log.warning("workflow_runtime: вебхук прогона %s на внутренний адрес отклонён",
                    run["id"])
        return None
    body = {
        "workflow_id": run.get("workflow_id"), "run_id": run["id"],
        "bot_id": run.get("bot_id"), "chat_id": run.get("chat_id"),
        "contact": {k: contact.get(k) for k in ("username", "first_name", "last_name")},
        "fields": fields, "vars": {k: v for k, v in _as_dict(run.get("vars")).items()
                                   if not str(k).startswith("_")},
        "event": _as_dict(run.get("input_data")),
    }
    try:
        async with http.post(url, json=body,
                             timeout=aiohttp.ClientTimeout(total=10),
                             allow_redirects=False) as resp:
            return resp.status
    except Exception:
        log.info("workflow_runtime: вебхук прогона %s не доставлен", run["id"])
        return None


async def _send(pool, http, token, run, text, markup=None) -> tuple[bool, int | None, str]:
    from services import bot_api, brand_injection
    try:
        if await brand_injection.is_free_tier(pool, run["bot_id"]):
            text = brand_injection.add_promo(text, html=True, context="broadcast")
    except Exception:
        log.debug("workflow_runtime: brand_injection", exc_info=True)
    return await bot_api.send_message_classified(
        http, token, run["chat_id"], text, reply_markup=markup)


async def _send_failed(pool, run: dict, step_idx: int, retry: int | None,
                       category: str) -> None:
    """Отправка не удалась: шаг уже занят, решаем судьбу прогона."""
    from services import bot_api
    if bot_api.recipient_is_gone(category):
        await _finish(pool, run, "stopped",
                      "Человек недоступен: заблокировал бота или удалил аккаунт")
        return
    fails = int(run.get("fail_count") or 0) + 1
    if fails >= _FAIL_LIMIT and not retry:
        await _finish(pool, run, "failed",
                      f"Сообщение не отправлено {fails} раз подряд ({category or 'ошибка'})")
        return
    delay = retry or min(60 * fails, 900)
    # Вернуть указатель на этот шаг: он занят, но не выполнен.
    await pool.execute(
        """UPDATE workflow_runs
              SET current_step=$2, status='running', wait_kind=NULL,
                  wait_until=NULL, fail_count=$3,
                  next_run_at=now() + make_interval(secs => $4),
                  claimed_until=NULL
            WHERE id=$1""",
        run["id"], step_idx, fails if not retry else run.get("fail_count") or 0,
        int(delay))


async def advance(pool, http, run: dict, main_bot=None) -> None:
    """Пройти шаги прогона до паузы, ожидания или конца."""
    steps = _as_list(run.get("steps_snapshot"))
    token = await _bot_token(pool, run.get("bot_id"))
    contact, fields = await _contact(pool, run.get("bot_id"), run.get("chat_id"))
    for _ in range(_STEPS_PER_PASS):
        idx = int(run.get("current_step") or 0)
        seq = int(run.get("exec_seq") or 0)
        run_vars = _as_dict(run.get("vars"))
        if seq >= _MAX_EXEC_PER_RUN:
            await _finish(pool, run, "failed",
                          f"Сценарий зациклился: выполнено {seq} шагов")
            return
        if idx < 0 or idx >= len(steps):
            await _finish(pool, run, "completed")
            return
        step = steps[idx]
        stype = step.get("type")
        nxt = idx + 1
        needs_chat = stype in ("message", "ask", "invoice")
        if needs_chat and (not token or not run.get("chat_id")):
            await _finish(pool, run, "failed",
                          "Некому отправить: у сценария нет бота или получателя")
            return

        if stype == "delay":
            now = datetime.now(timezone.utc)
            if step.get("until_time"):
                tz_name = contact_tz(fields, contact.get("language_code"),
                                     step.get("tz"))
                try:
                    when = next_window(now, step["until_time"],
                                       bool(step.get("weekdays_only")), tz_name)
                except ValueError:
                    when = now + timedelta(minutes=1)
                detail = f"до {step['until_time']} ({tz_name})"
            else:
                minutes = int(step.get("delay_minutes") or 1)
                when = now + timedelta(minutes=minutes)
                detail = f"{minutes} мин"
            if not await _take_step(
                    pool, run["id"], seq, current_step=nxt,
                    next_run_at=when, claimed_until=None):
                return
            await _log(pool, run, idx, stype, "delay", detail)
            return

        if stype == "split":
            branches = step.get("branches") or []
            pick = pick_split(branches)
            target = _goto_index(branches[pick], "goto") if branches else None
            nxt = target if target is not None else idx + 1
            if not await _take_step(
                    pool, run["id"], seq, current_step=nxt,
                    vars={**run_vars, "_split_" + str(idx + 1): pick}):
                return
            await _log(pool, run, idx, stype, f"branch:{pick}",
                       (branches[pick].get("label") if branches else None)
                       or chr(ord("A") + pick))

        elif stype == "condition":
            try:
                ok = await evaluate(pool, http, token, run, step, contact, fields)
            except Exception:
                log.warning("workflow_runtime: условие прогона %s не проверено",
                            run["id"], exc_info=True)
                ok = False
            if not ok:
                target = _goto_index(step, "else_goto")
                if target is None:
                    if await _take_step(pool, run["id"], seq, current_step=len(steps)):
                        await _log(pool, run, idx, stype, "no")
                        await _finish(pool, run, "completed", "Условие не выполнено")
                    return
                nxt = target
            if not await _take_step(pool, run["id"], seq, current_step=nxt):
                return
            await _log(pool, run, idx, stype, "yes" if ok else "no")

        elif stype == "message":
            buttons = step.get("buttons") or []
            branching = any(not b.get("url") for b in buttons)
            text = render(step.get("text") or "", contact, fields, run_vars)
            if branching:
                until = datetime.now(timezone.utc) + timedelta(
                    minutes=int(step.get("timeout_minutes") or _BUTTON_WAIT_DEFAULT_MIN))
                if not await _take_step(pool, run["id"], seq, status="waiting",
                                        wait_kind="button", wait_until=until):
                    return
            elif not await _take_step(pool, run["id"], seq, current_step=nxt):
                return
            ok, retry, cat = await _send(pool, http, token, run, text,
                                         button_markup(run["id"], seq + 1, buttons))
            if not ok:
                await _log(pool, run, idx, stype, "send_failed", cat)
                await _send_failed(pool, run, idx, retry, cat)
                return
            await _log(pool, run, idx, stype, "sent")
            if branching:
                _mark_hot(run.get("bot_id"))
                return

        elif stype == "ask":
            until = datetime.now(timezone.utc) + timedelta(
                minutes=int(step.get("timeout_minutes") or 1440))
            if not await _take_step(pool, run["id"], seq, status="waiting",
                                    wait_kind="reply", wait_until=until):
                return
            text = render(step.get("text") or "", contact, fields, run_vars)
            ok, retry, cat = await _send(pool, http, token, run, text)
            if not ok:
                await _log(pool, run, idx, stype, "send_failed", cat)
                await _send_failed(pool, run, idx, retry, cat)
                return
            await _log(pool, run, idx, stype, "sent")
            _mark_hot(run.get("bot_id"))
            return

        elif stype == "invoice":
            until = datetime.now(timezone.utc) + timedelta(
                minutes=int(step.get("timeout_minutes") or 1440))
            if not await _take_step(pool, run["id"], seq, status="waiting",
                                    wait_kind="payment", wait_until=until):
                return
            ok, cat = await send_invoice(pool, http, token, run, step, seq + 1)
            if not ok:
                await _log(pool, run, idx, stype, "send_failed", cat)
                await _send_failed(pool, run, idx, None, cat)
                return
            await _log(pool, run, idx, stype, "sent",
                       amount=int(step.get("amount") or 0) or None,
                       currency=step.get("currency") or "XTR")
            _mark_hot(run.get("bot_id"))
            return

        elif stype == "action":
            action = step.get("action")
            if action == "goto":
                target = _goto_index(step, "goto")
                nxt = target if target is not None else len(steps)
            if action == "stop":
                if await _take_step(pool, run["id"], seq, current_step=len(steps)):
                    await _log(pool, run, idx, stype, "done", "stop")
                    await _finish(pool, run, "completed")
                return
            if not await _take_step(pool, run["id"], seq, current_step=nxt):
                return
            try:
                await _do_action(pool, run, step, contact, fields, main_bot)
                await _log(pool, run, idx, stype, "done", action)
            except Exception:
                log.warning("workflow_runtime: действие %s прогона %s не выполнено",
                            action, run["id"], exc_info=True)
                await _log(pool, run, idx, stype, "error", action)

        elif stype == "webhook":
            if not await _take_step(pool, run["id"], seq, current_step=nxt):
                return
            code = await _do_webhook(http, run, step, contact, fields)
            await _log(pool, run, idx, stype,
                       "done" if code and 200 <= code < 300 else "error",
                       str(code) if code else "не доставлен")

        else:
            # Неизвестный тип (данные со старых времён) — пропустить, не падать.
            if not await _take_step(pool, run["id"], seq, current_step=nxt):
                return

        # Шаг сделан — перечитать прогон: его состояние двигал условный UPDATE.
        fresh = await pool.fetchrow("SELECT * FROM workflow_runs WHERE id=$1", run["id"])
        if not fresh or fresh["status"] != "running":
            return
        run = dict(fresh)
        if run.get("next_run_at") and run["next_run_at"] > datetime.now(timezone.utc):
            return
    # Шагов за заход хватило — остаток подхватит следующий тик.
    await pool.execute("UPDATE workflow_runs SET claimed_until=NULL WHERE id=$1",
                       run["id"])


async def _do_action(pool, run, step, contact, fields, main_bot) -> None:
    from database import db
    action = step.get("action")
    value = step.get("value") or ""
    bot_id, chat_id, owner = run.get("bot_id"), run.get("chat_id"), run["owner_id"]
    if action in ("add_tag", "remove_tag"):
        if not bot_id or not chat_id:
            return
        if action == "add_tag":
            await db.add_user_tag(pool, bot_id, chat_id, value)
        else:
            # Событие «тег снят» пишет сам db.remove_user_tag.
            await db.remove_user_tag(pool, bot_id, chat_id, value)
    elif action == "set_field":
        name, _, val = value.partition("=")
        await _set_field(pool, bot_id, chat_id, name.strip(), val.strip(), owner)
        await pool.execute(
            "UPDATE workflow_runs SET vars = vars || jsonb_build_object($2::text, $3::text) "
            "WHERE id=$1", run["id"], name.strip(), val.strip())
    elif action == "create_deal":
        label = (contact.get("username") and "@" + contact["username"]) \
            or contact.get("first_name") or (str(chat_id) if chat_id else "без контакта")
        title = (value or "Заявка из сценария") + f" — {label}"
        deal_id = await pool.fetchval(
            """INSERT INTO crm_deals (owner_id, title, contact, stage, notes, created_at)
               VALUES ($1, $2, $3, 'lead', $4, now()) RETURNING id""",
            owner, title[:200], label[:200],
            "\n".join(f"{k}: {v}" for k, v in fields.items())[:2000] or None)
        # Сделка прогона запоминается: оплата счёта сценария переведёт её в
        # «выиграна», а действие «Сменить этап сделки» — в любой этап.
        await pool.execute(
            "UPDATE workflow_runs SET vars = vars || jsonb_build_object('_deal_id', $2::bigint) "
            "WHERE id=$1", run["id"], deal_id)
        run.setdefault("vars", {})
        if isinstance(run["vars"], dict):
            run["vars"]["_deal_id"] = deal_id
        await bus.emit(pool, owner, "deal.created",
                       {"deal_id": deal_id, "title": title, "source": "workflow",
                        "workflow_id": run.get("workflow_id"),
                        "contact_source": run.get("source")},
                       bot_id=bot_id, chat_id=chat_id)
    elif action == "move_deal":
        deal_id = _as_dict(run.get("vars")).get("_deal_id")
        if deal_id:
            await db.move_crm_deal_stage(pool, int(deal_id), owner, value,
                                         source="workflow")
    elif action == "notify_owner":
        if main_bot is None:
            return
        who = (contact.get("username") and "@" + contact["username"]) \
            or contact.get("first_name") or (f"id{chat_id}" if chat_id else "—")
        lines = [f"🔔 <b>Сценарий</b>: {html.escape(value or 'уведомление')}",
                 f"👤 {html.escape(who)}"]
        for k, v in list(fields.items())[:10]:
            lines.append(f"• {html.escape(str(k))}: {html.escape(str(v))[:200]}")
        try:
            await main_bot.send_message(owner, "\n".join(lines), parse_mode="HTML")
        except Exception:
            log.info("workflow_runtime: уведомление владельцу %s не доставлено", owner)


# ── Платёж (счёт в чате) ────────────────────────────────────────────────────

async def send_invoice(pool, http, token, run: dict, step: dict,
                       seq: int) -> tuple[bool, str]:
    """Выставить счёт в чат. Звёзды (XTR) — без провайдера; прочие валюты —
    с токеном платёжного провайдера бота."""
    from services import bot_api
    currency = step.get("currency") or "XTR"
    params: dict[str, Any] = {
        "chat_id": run["chat_id"],
        "title": step.get("title") or "Оплата",
        "description": step.get("text") or step.get("title") or "Оплата",
        "payload": invoice_payload(run["id"], seq),
        "currency": currency,
        "prices": [{"label": (step.get("title") or "Оплата")[:32],
                    "amount": int(step.get("amount") or 0)}],
    }
    if currency == "XTR":
        params["provider_token"] = ""
    else:
        provider = await _provider_token(pool, run.get("bot_id"))
        if not provider:
            await _finish(pool, run, "failed",
                          "У бота не подключён платёжный провайдер (настройки "
                          "ИИ-продавца → Заказы): счёт в "
                          f"{currency} выставить нельзя (Звёзды — можно)")
            return True, ""
        params["provider_token"] = provider
    data = await bot_api._call(http, token, "sendInvoice", **params)
    if data.get("ok"):
        return True, ""
    return False, bot_api.classify_send_error(data.get("error_code", 0),
                                              data.get("description", ""))


async def _provider_token(pool, bot_id) -> str | None:
    if not bot_id:
        return None
    # Токен провайдера задаётся в настройках ИИ-продавца бота (раздел
    # «Заказы»): один провайдер на бота и для заказов, и для счетов сценария.
    try:
        raw = await pool.fetchval(
            "SELECT payment_provider_token FROM bot_sales_personas "
            "WHERE bot_id=$1 AND payment_provider_token IS NOT NULL "
            "ORDER BY is_active DESC, id DESC LIMIT 1", bot_id)
    except Exception:
        return None
    if not raw:
        return None
    from services.token_vault import decrypt_token
    return decrypt_token(raw)


# ── Ответы человека (зовёт опрос бота) ─────────────────────────────────────

async def on_button(pool, http, token, bot_id: int, chat_id: int,
                    callback: dict) -> bool:
    """Нажатие кнопки сценария. True — нажатие наше (callback `wf:`), даже
    если кнопка устарела: такое нажатие не должно уйти в другие обработчики."""
    parsed = parse_callback(callback.get("data") or "")
    if not parsed:
        return False
    run_id, seq, bidx = parsed
    row = await pool.fetchrow(
        """SELECT * FROM workflow_runs
            WHERE id=$1 AND bot_id=$2 AND chat_id=$3 AND status='waiting'
              AND wait_kind='button' AND exec_seq=$4""",
        run_id, bot_id, chat_id, seq)
    if not row:
        return True
    run = dict(row)
    steps = _as_list(run.get("steps_snapshot"))
    idx = int(run.get("current_step") or 0)
    if not 0 <= idx < len(steps):
        return True
    buttons = steps[idx].get("buttons") or []
    if not 0 <= bidx < len(buttons):
        return True
    btn = buttons[bidx]
    target = _goto_index(btn, "goto")
    nxt = target if target is not None else idx + 1
    res = await pool.execute(
        """UPDATE workflow_runs
              SET status='running', wait_kind=NULL, wait_until=NULL,
                  current_step=$3, next_run_at=now(), claimed_until=NULL,
                  vars = vars || jsonb_build_object('last_button', $4::text)
            WHERE id=$1 AND status='waiting' AND exec_seq=$2""",
        run_id, seq, nxt, btn.get("text") or "")
    if not (isinstance(res, str) and res.endswith(" 1")):
        return True
    await _log(pool, run, idx, "message", f"button:{bidx}", btn.get("text"))
    # Убрать клавиатуру с сообщения: второе нажатие той же кнопки иначе
    # выглядело бы рабочим, а не делало ничего.
    msg = callback.get("message") or {}
    if token and msg.get("message_id"):
        try:
            from services import bot_api
            await bot_api._call(http, token, "editMessageReplyMarkup",
                                chat_id=chat_id, message_id=msg["message_id"],
                                reply_markup={"inline_keyboard": []})
        except Exception:
            log.debug("workflow_runtime: клавиатура не снята", exc_info=True)
    await bus.emit(pool, run["owner_id"], "button.clicked",
                   {"workflow_id": run.get("workflow_id"), "button": btn.get("text")},
                   bot_id=bot_id, chat_id=chat_id)
    wake()
    return True


async def on_reply(pool, bot_id: int, chat_id: int, text: str) -> dict:
    """Сообщение человека, когда сценарий ждёт ответ на вопрос.

    {"consumed": False} — сценарий ответа не ждёт, сообщение идёт дальше
    обычным путём. {"consumed": True, "reply": str|None} — ответ принят
    сценарием (reply — подсказка, если ответ не прошёл проверку).
    """
    row = await pool.fetchrow(
        """SELECT * FROM workflow_runs
            WHERE bot_id=$1 AND chat_id=$2 AND status='waiting' AND wait_kind='reply'
            ORDER BY id DESC LIMIT 1""", bot_id, chat_id)
    if not row:
        return {"consumed": False}
    run = dict(row)
    steps = _as_list(run.get("steps_snapshot"))
    idx = int(run.get("current_step") or 0)
    if not 0 <= idx < len(steps) or steps[idx].get("type") != "ask":
        return {"consumed": False}
    step = steps[idx]
    kind = step.get("validate") or "any"
    run_vars = _as_dict(run.get("vars"))
    value = check_answer(kind, text)
    if value is None:
        attempts = int(run_vars.get("_ask_attempts") or 0) + 1
        if attempts < _MAX_ASK_ATTEMPTS:
            await pool.execute(
                "UPDATE workflow_runs SET vars = vars || jsonb_build_object("
                "'_ask_attempts', $2::int) WHERE id=$1", run["id"], attempts)
            return {"consumed": True, "reply": _ASK_HINTS.get(kind)}
        # Три неудачные попытки — принимаем как есть: держать человека в
        # вопросе бесконечно хуже, чем сохранить неидеальный ответ.
        value = (text or "").strip()[:1000]
    name = step.get("save_as") or "answer"
    res = await pool.execute(
        """UPDATE workflow_runs
              SET status='running', wait_kind=NULL, wait_until=NULL,
                  current_step=$3, next_run_at=now(), claimed_until=NULL,
                  vars = (vars - '_ask_attempts') || jsonb_build_object($4::text, $5::text)
            WHERE id=$1 AND status='waiting' AND exec_seq=$2""",
        run["id"], run["exec_seq"], idx + 1, name, value)
    if isinstance(res, str) and res.endswith(" 1"):
        await _log(pool, run, idx, "ask", "answered", f"{name}: {value}"[:300])
        await _set_field(pool, bot_id, chat_id, name, value, run["owner_id"])
        wake()
    return {"consumed": True, "reply": None}


async def on_payment(pool, bot_id: int, chat_id: int, payment: dict) -> bool:
    """Успешная оплата счёта сценария. True — платёж наш (payload `wf:`)."""
    parsed = parse_invoice_payload(payment.get("invoice_payload") or "")
    if not parsed:
        return False
    run_id, seq = parsed
    row = await pool.fetchrow(
        """SELECT * FROM workflow_runs
            WHERE id=$1 AND bot_id=$2 AND chat_id=$3""", run_id, bot_id, chat_id)
    if not row:
        return True
    run = dict(row)
    charge = payment.get("telegram_payment_charge_id")
    if charge and await pool.fetchval(
            "SELECT 1 FROM workflow_step_log WHERE run_id=$1 AND outcome='paid' "
            "AND detail=$2 LIMIT 1", run_id, str(charge)[:300]):
        # Повтор апдейта той же оплаты (оффсет опроса сдвигается после
        # пакета): деньги уже учтены — второй раз не считать выручку и не
        # писать второе событие «получена оплата».
        return True
    amount = payment.get("total_amount")
    currency = payment.get("currency")
    paid_vars = {"paid": True, "paid_amount": amount, "paid_currency": currency,
                 "paid_charge_id": payment.get("telegram_payment_charge_id")}
    res = await pool.execute(
        """UPDATE workflow_runs
              SET status='running', wait_kind=NULL, wait_until=NULL,
                  current_step=current_step + 1, next_run_at=now(),
                  claimed_until=NULL, vars = vars || $3::jsonb
            WHERE id=$1 AND status='waiting' AND wait_kind='payment'
              AND exec_seq=$2""",
        run_id, seq, json.dumps(paid_vars, default=str))
    if not (isinstance(res, str) and res.endswith(" 1")):
        # Оплата пришла после тайм-аута счёта — деньги есть, прогон уже
        # ушёл дальше. Отметить в переменных, чтобы условие «Оплатил» в
        # оставшихся шагах это видело.
        await pool.execute("UPDATE workflow_runs SET vars = vars || $2::jsonb "
                           "WHERE id=$1", run_id, json.dumps(paid_vars, default=str))
    steps = _as_list(run.get("steps_snapshot"))
    idx = int(run.get("current_step") or 0)
    await _log(pool, run, idx, steps[idx].get("type", "invoice")
               if 0 <= idx < len(steps) else "invoice", "paid",
               payment.get("telegram_payment_charge_id"),
               amount=int(amount or 0) or None, currency=currency)
    deal_id = _as_dict(run.get("vars")).get("_deal_id")
    if deal_id:
        try:
            from database import db
            await db.move_crm_deal_stage(pool, int(deal_id), run["owner_id"], "won",
                                         source="payment")
        except Exception:
            log.warning("workflow_runtime: сделка %s прогона %s не переведена в won",
                        deal_id, run_id, exc_info=True)
    await bus.emit(pool, run["owner_id"], "payment.received",
                   {"amount": amount, "currency": currency,
                    "source": "workflow", "workflow_id": run.get("workflow_id"),
                    "run_id": run_id, "contact_source": run.get("source"),
                    "charge_id": payment.get("telegram_payment_charge_id")},
                   bot_id=bot_id, chat_id=chat_id)
    wake()
    return True


async def check_pre_checkout(pool, bot_id: int, query: dict) -> tuple[bool, str | None]:
    """Решение по pre_checkout_query для счёта сценария: (ok, ошибка).

    Telegram даёт 10 секунд на ответ. Проверяем, что прогон жив и ждёт
    именно эту оплату: по просроченному или отменённому счёту деньги не
    берём."""
    parsed = parse_invoice_payload(query.get("invoice_payload") or "")
    if not parsed:
        return False, None
    run_id, seq = parsed
    user_id = (query.get("from") or {}).get("id")
    ok = await pool.fetchval(
        """SELECT 1 FROM workflow_runs
            WHERE id=$1 AND bot_id=$2 AND chat_id=$3 AND status='waiting'
              AND wait_kind='payment' AND exec_seq=$4""",
        run_id, bot_id, user_id, seq)
    if ok:
        return True, None
    return True, "Счёт устарел. Запросите новый."


# ── Тайм-ауты ожиданий ──────────────────────────────────────────────────────

async def sweep_timeouts(pool) -> int:
    """Ожидания, у которых вышел срок: переход по `timeout_goto` или конец."""
    rows = await pool.fetch(
        """SELECT * FROM workflow_runs
            WHERE status='waiting' AND wait_until < now()
            ORDER BY wait_until LIMIT 200""")
    n = 0
    for r in rows:
        run = dict(r)
        steps = _as_list(run.get("steps_snapshot"))
        idx = int(run.get("current_step") or 0)
        step = steps[idx] if 0 <= idx < len(steps) else {}
        target = _goto_index(step, "timeout_goto")
        if target is not None:
            res = await pool.execute(
                """UPDATE workflow_runs
                      SET status='running', wait_kind=NULL, wait_until=NULL,
                          current_step=$3, next_run_at=now(), claimed_until=NULL
                    WHERE id=$1 AND status='waiting' AND exec_seq=$2""",
                run["id"], run["exec_seq"], target)
            if isinstance(res, str) and res.endswith(" 1"):
                await _log(pool, run, idx, step.get("type", "?"), "timeout")
        else:
            await _finish(pool, run, "expired", "Человек не ответил в срок")
        n += 1
    return n


async def cancel_for_contact(pool, bot_id: int, chat_id: int) -> int:
    """Человек написал «стоп»: остановить все его прогоны в этом боте."""
    res = await pool.execute(
        """UPDATE workflow_runs SET status='cancelled', finished_at=now(),
                  error_message='Человек отписался', wait_kind=NULL
            WHERE bot_id=$1 AND chat_id=$2 AND status IN ('running','waiting')""",
        bot_id, chat_id)
    try:
        return int(res.rsplit(" ", 1)[-1])
    except Exception:
        return 0


# ── «Горячие» боты для быстрого опроса ──────────────────────────────────────
# Бот, которому сценарий только что отправил кнопки, вопрос или счёт, ждёт
# ответа человека. Обычный опрос ботов идёт раз в 10 секунд, а на
# pre_checkout_query Telegram даёт ровно 10 секунд — счёт срывался бы через
# раз. Такие боты опрос берёт каждую секунду, пока метка не истечёт.
_HOT_SECONDS = 15 * 60
_hot: dict[int, float] = {}


def _mark_hot(bot_id, seconds: int = _HOT_SECONDS) -> None:
    if not bot_id:
        return
    _hot[int(bot_id)] = max(_hot.get(int(bot_id), 0.0), time.monotonic() + seconds)


def mark_hot(bot_id, seconds: int = _HOT_SECONDS) -> None:
    _mark_hot(bot_id, seconds)


def hot_bots() -> set[int]:
    now = time.monotonic()
    for b in [b for b, t in _hot.items() if t < now]:
        _hot.pop(b, None)
    return set(_hot)


# ── Цикл ────────────────────────────────────────────────────────────────────

async def tick(pool, http, main_bot=None) -> None:
    try:
        await dispatch_events(pool)
    except Exception:
        log.exception("workflow_runtime: разбор событий")
    try:
        await sweep_timeouts(pool)
    except Exception:
        log.exception("workflow_runtime: тайм-ауты ожиданий")
    runs = await _claim_due(pool)
    for run in runs:
        try:
            await advance(pool, http, run, main_bot)
        except Exception:
            log.exception("workflow_runtime: прогон %s", run.get("id"))
            fails = int(run.get("fail_count") or 0) + 1
            if fails >= _FAIL_LIMIT:
                await _finish(pool, run, "failed", "Внутренняя ошибка исполнителя")
            else:
                await pool.execute(
                    """UPDATE workflow_runs SET fail_count=$2, claimed_until=NULL,
                              next_run_at=now() + make_interval(secs => $3)
                        WHERE id=$1 AND status='running'""",
                    run["id"], fails, 60 * fails)


async def run(pool, http: aiohttp.ClientSession, main_bot=None) -> None:
    """Фоновый цикл исполнителя сценариев."""
    log.info("workflow_runtime: исполнитель сценариев запущен")
    while True:
        try:
            await tick(pool, http, main_bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("workflow_runtime: цикл")
        _wake.clear()
        try:
            await asyncio.wait_for(_wake.wait(), timeout=_TICK_SECONDS)
        except asyncio.TimeoutError:
            pass
