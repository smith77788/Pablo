"""Workflow Engine — automated multi-step operation orchestration.

Manages workflow definitions, execution, and status tracking.
"""
from __future__ import annotations

import json
import logging
import math
import re
from datetime import datetime
from typing import Any, Optional

import asyncpg

log = logging.getLogger(__name__)


# ── Шаблоны сценариев ────────────────────────────────────────────────────────
# Экран предлагал четыре шаблона («👋 Приветствие», «💧 Дрип-серия», «⚡ Реакция
# на событие», «🔀 Условная логика»), спрашивал подтверждение и отвечал
# «✅ Воркфлоу по шаблону создан», а обработчик параметр `template` не читал
# вовсе: создавался сценарий с `steps = '[]'` — пустой.
#
# Шаги описаны в той же форме, что складывает экран добавления шага
# (`{type, text|delay_minutes|condition…}`), и типы — ровно те, что экран умеет
# показывать: message, delay, condition, action, webhook.
WORKFLOW_TEMPLATES: dict[str, dict] = {
    "welcome": {
        "label": "Приветствие",
        "trigger_type": "start",
        "steps": [
            {"type": "message",
             "text": "Привет! Спасибо за подписку — рассказываю, что здесь есть."},
            {"type": "delay", "delay_minutes": 60},
            {"type": "message",
             "text": "Если остались вопросы — напишите, отвечу лично."},
        ],
    },
    "drip": {
        "label": "Дрип-серия",
        "trigger_type": "start",
        "steps": [
            {"type": "message", "text": "Письмо 1: с чего начать."},
            {"type": "delay", "delay_minutes": 1440},
            {"type": "message", "text": "Письмо 2: разбор частой ошибки."},
            {"type": "delay", "delay_minutes": 2880},
            {"type": "message", "text": "Письмо 3: что делать дальше."},
        ],
    },
    "react": {
        "label": "Реакция на событие",
        "trigger_type": "start",
        "steps": [
            {"type": "condition", "condition": "event", "condition_value": "new_subscriber"},
            {"type": "message", "text": "Вижу, вы только присоединились — держите короткий гид."},
        ],
    },
    "condition": {
        "label": "Условная логика",
        "trigger_type": "tag_added",
        "trigger_value": "клиент",
        "steps": [
            {"type": "condition", "condition": "has_tag", "condition_value": "клиент"},
            {"type": "message", "text": "Для клиентов: отдельные условия и поддержка."},
            {"type": "delay", "delay_minutes": 720},
            {"type": "message", "text": "Напоминание: предложение ещё в силе."},
        ],
    },
    # Квалификация заявки: то, ради чего у лидеров есть «конструктор» —
    # кнопки ведут в разные ветки, ответы сохраняются в карточку, владелец
    # получает готовую сделку и уведомление.
    "lead": {
        "label": "Квалификация заявки",
        "trigger_type": "start",
        "steps": [
            {"type": "message",
             "text": "Здравствуйте! Подскажите, что вас интересует?",
             "buttons": [{"text": "Хочу купить", "goto": 2},
                         {"text": "Просто смотрю", "goto": 8}]},
            {"type": "ask", "text": "Как к вам обращаться?", "save_as": "name",
             "validate": "any", "timeout_minutes": 1440},
            {"type": "ask", "text": "Оставьте номер телефона — менеджер перезвонит.",
             "save_as": "phone", "validate": "phone", "timeout_minutes": 1440},
            {"type": "action", "action": "create_deal", "value": "Заявка из бота"},
            {"type": "action", "action": "notify_owner", "value": "Новая заявка"},
            {"type": "message", "text": "Спасибо! Менеджер свяжется с вами в ближайшее время."},
            {"type": "action", "action": "stop"},
            {"type": "action", "action": "add_tag", "value": "смотрит"},
            {"type": "message",
             "text": "Хорошо! Если появятся вопросы — просто напишите сюда."},
        ],
    },
}

# Типы шагов, которые экран умеет показать. Шаг неизвестного типа в шаблоне
# означал бы строку «⚫ undefined» в деталях сценария.
WORKFLOW_STEP_TYPES = frozenset({"message", "delay", "condition", "action", "webhook",
                                 "ask", "invoice", "split", "wait", "ai"})

# Действия шага `action` — ровно те, что исполнитель (services/workflow_runtime.py)
# умеет выполнить. Раньше имя действия было произвольным текстом: шаг
# «⚡ warmup» сохранялся, показывался и не означал ничего, потому что
# исполнять его было некому. Теперь шаг исполняется, и действие, которого
# исполнитель не знает, — отказ при сохранении, а не тихий пропуск в прогоне.
WORKFLOW_ACTIONS: dict[str, str] = {
    "add_tag": "Поставить тег",
    "remove_tag": "Снять тег",
    "set_field": "Записать поле (поле=значение)",
    "add_to_field": "Прибавить к полю (поле+=число): баллы, счётчики",
    "create_deal": "Создать сделку в CRM",
    "move_deal": "Сменить этап сделки прогона",
    "notify_owner": "Уведомить меня",
    "notify_chat": "Карточка заявки в чат или канал (@чат или -100…, после «|» заголовок)",
    "start_workflow": "Запустить другой сценарий (номер сценария)",
    "stop_workflows": "Снять с других сценариев (пусто — со всех, или номер)",
    "goto": "Перейти к шагу",
    "stop": "Завершить сценарий",
}
# Этапы сделки — ровно те, что пускает ограничение crm_deals_stage_check
# (services/mini_app_api.py). Раньше действие «Создать сделку» писало этап
# 'new', которого ограничение не пускает: сделка не создавалась вовсе, а
# ошибка тонула в журнале исполнителя.
DEAL_STAGES: dict[str, str] = {
    "lead": "лид", "contact": "контакт", "proposal": "предложение",
    "negotiation": "переговоры", "won": "выиграна", "lost": "проиграна",
}

# Действия, которым значение обязательно.
_ACTIONS_NEED_VALUE = frozenset({"add_tag", "remove_tag", "set_field", "goto",
                                 "move_deal", "start_workflow", "add_to_field",
                                 "notify_chat"})

# Условия шага `condition` — те, что исполнитель умеет проверить.
WORKFLOW_CONDITIONS: dict[str, str] = {
    "tag_has": "Есть тег",
    "has_tag": "Есть тег",
    "tag_not": "Нет тега",
    "field_equals": "Поле равно (поле=значение)",
    "field_not_equals": "Поле не равно (поле=значение)",
    "field_contains": "Поле содержит (поле=текст)",
    "field_gt": "Поле больше числа (поле=число)",
    "field_lt": "Поле меньше числа (поле=число)",
    "field_set": "Поле заполнено",
    "clicked": "Нажал кнопку (текст кнопки)",
    "inactive_days": "Дней неактивен (≥)",
    "subscribed": "Подписан на канал (@канал)",
    "paid": "Оплатил",
    "event": "Сценарий запущен событием",
    "work_hours": "Сейчас рабочее время (09-18, можно «09-18 будни»)",
}

# Условия «поле=значение»: имя поля проверяется при сохранении.
_FIELD_COMPARE = frozenset({"field_equals", "field_not_equals", "field_contains",
                            "field_gt", "field_lt"})


def _fmt_number(x: float) -> str:
    """10.0 → «10», 2.5 → «2.5»: в поле человека не «10.0». ЧИСТАЯ."""
    return str(int(x)) if float(x).is_integer() else f"{x:.6g}"


def _to_number(raw) -> float | None:
    """«1 500,5» → 1500.5; не число — None. ЧИСТАЯ.

    «nan», «inf», «1e309» — тоже не число: float() их принимает, а дальше NaN
    проходит любой потолок (`abs(nan) > 1e6` ложно), и шаг «score+=nan» или
    ответ подписчика «inf» навсегда портил поле и сравнения «больше/меньше».
    """
    try:
        x = float(str(raw).replace(" ", "").replace("\u00a0", "").replace(",", "."))
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


# Проверка ответа на вопрос (шаг `ask`).
ASK_VALIDATORS = ("any", "phone", "email", "number")

# Валюты счёта. XTR — Telegram Stars: работают без платёжного провайдера.
# Остальные требуют токен провайдера у бота (managed_bots.payment_provider_token).
INVOICE_CURRENCIES = ("XTR", "RUB", "USD", "EUR", "UAH", "KZT")

# Имя поля контакта: латиница/цифры/подчёркивание — оно же ключ в jsonb и
# плейсхолдер {{имя}} в тексте сообщения.
_FIELD_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
MAX_BUTTONS = 8
MAX_BUTTON_TEXT = 64
MAX_AI_PROMPT = 2000
# Сколько источников показывать в воронке по шагам.
FUNNEL_SOURCES = 5
# Вложение шага «Сообщение»: тип → метод Bot API. Подпись — до 1024 символов.
MEDIA_KINDS: dict[str, str] = {"photo": "фото", "video": "видео", "document": "файл"}
MAX_CAPTION = 1024
_FILE_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{20,250}$")
# Повторы шага «Вебхук» при сбое сервиса (по умолчанию и предел).
WEBHOOK_DEFAULT_RETRIES = 3
WEBHOOK_MAX_RETRIES = 5


def workflow_template(name: str) -> dict | None:
    """Описание шаблона: {key, label, steps[]}. Чистая функция."""
    tpl = WORKFLOW_TEMPLATES.get((name or "").strip().lower())
    if not tpl:
        return None
    key = (name or "").strip().lower()
    # Копия: вызывающий складывает шаги в jsonb и может их править.
    return {"key": key, "label": tpl["label"],
            "trigger_type": tpl.get("trigger_type"),
            "trigger_value": tpl.get("trigger_value"),
            "steps": [json.loads(json.dumps(step)) for step in tpl["steps"]]}


async def init_workflow_tables(pool: asyncpg.Pool) -> None:
    """Create workflow tables."""
    await pool.execute('''
        CREATE TABLE IF NOT EXISTS workflow_definitions (
            id SERIAL PRIMARY KEY,
            owner_id BIGINT NOT NULL,
            name TEXT NOT NULL,
            description TEXT,
            steps JSONB NOT NULL DEFAULT '[]',
            is_active BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        );
    ''')
    await pool.execute('''
        CREATE TABLE IF NOT EXISTS workflow_runs (
            id SERIAL PRIMARY KEY,
            owner_id BIGINT NOT NULL,
            workflow_id INTEGER REFERENCES workflow_definitions(id),
            status TEXT DEFAULT 'pending',
            input_data JSONB DEFAULT '{}',
            output_data JSONB DEFAULT '{}',
            current_step INTEGER DEFAULT 0,
            total_steps INTEGER DEFAULT 0,
            error_message TEXT,
            started_at TIMESTAMPTZ,
            finished_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ DEFAULT NOW()
        );
    ''')
    log.info("Workflow tables initialized")


async def create_workflow(
    pool: asyncpg.Pool,
    owner_id: int,
    name: str,
    description: str = "",
    steps: list[dict] | None = None,
    is_active: bool = False,
    bot_id: int | None = None,
) -> dict:
    """Создать определение сценария.

    `is_active=False` по умолчанию: сценарий ещё никто не исполняет, и
    включённый по рождению он обещал бы владельцу работу, которой не будет.
    Экран так и делал у себя в обработчике (`VALUES(..., FALSE)`) — условие
    переехало сюда, чтобы оба пути создания означали одно.

    Шаги пишутся КАК ДАНЫ: проверка — `validate_steps`, её зовут обработчики
    (см. services/mini_app_workflows.py). Разделение намеренное: эту функцию
    зовут и тесты движка, и служебный код, где шаги уже проверены.

    `bot_id` — бот, которому сценарий принадлежит. Экран его присылал с самого
    начала (в модалке создания есть выбор «Бот»), а читать его было некуда:
    колонки не существовало, и выбор владельца исчезал без следа. Проверять,
    что бот ЕГО, обязан обработчик — здесь владелец бота не известен.
    """
    try:
        row = await pool.fetchrow(
            '''INSERT INTO workflow_definitions
                   (owner_id, name, description, steps, is_active, bot_id)
               VALUES ($1, $2, $3, $4::jsonb, $5, $6)
               RETURNING id''',
            owner_id, name, description, json.dumps(steps or []), bool(is_active),
            bot_id)
        return {"ok": True, "id": row["id"]}
    except Exception as e:
        log.warning("create_workflow error: %s", e)
        return {"ok": False, "error": str(e)}


async def copy_workflow(pool: asyncpg.Pool, owner_id: int, workflow_id: int,
                        bot_id: int | None = None, name: str | None = None) -> dict:
    """Копия сценария: шаги, способ запуска, цель и повторный вход.

    Так делают у лидеров (ManyChat, Make): рабочий сценарий размножают на
    второго бота или переделывают копию, не трогая боевой. Копия рождается
    ВЫКЛЮЧЕННОЙ — включённая сразу начала бы писать людям второго бота, не
    дождавшись, пока владелец поправит тексты. Владельца бота `bot_id`
    проверяет обработчик. Зеркала старых воронок не копируются: их шаги
    живут в разделе воронок, и копия разошлась бы с ним.

    ValueError — с русским текстом для владельца."""
    src = await pool.fetchrow(
        """SELECT name, description, steps, bot_id, trigger_type, trigger_value,
                  allow_reentry, goal_type, goal_value, legacy_kind, send_window,
                  max_per_day
             FROM workflow_definitions WHERE id=$1 AND owner_id=$2""",
        workflow_id, owner_id)
    if not src:
        raise LookupError("Сценарий не найден")
    if src["legacy_kind"]:
        raise ValueError("Это воронка из раздела «Воронки» — копируйте её там")
    steps = src["steps"]
    if isinstance(steps, str):
        steps = json.loads(steps or "[]")
    steps = validate_steps(steps or [])
    target_bot = bot_id if bot_id is not None else src["bot_id"]
    new_name = (name or "").strip()[:120] or f"{src['name']} (копия)"[:120]
    res = await create_workflow(pool, owner_id, new_name,
                                description=src["description"] or "",
                                steps=steps, is_active=False, bot_id=target_bot)
    if not res.get("ok"):
        raise RuntimeError(res.get("error") or "копия не создана")
    ttype = src["trigger_type"]
    if ttype in ("inactive", "schedule", "webhook") and target_bot is None:
        ttype = None
    # У копии свой адрес вебхука: общий запускал бы оба сценария одним
    # запросом — вернее, только один из них, и неизвестно какой.
    await pool.execute(
        """UPDATE workflow_definitions
              SET trigger_type=$2, trigger_value=$3, allow_reentry=$4,
                  goal_type=$5, goal_value=$6, send_window=$7, max_per_day=$8,
                  inbound_token=CASE WHEN $2='webhook' THEN $9 END
            WHERE id=$1""",
        res["id"], ttype, src["trigger_value"] if ttype else None,
        bool(src["allow_reentry"]), src["goal_type"], src["goal_value"],
        src["send_window"], src["max_per_day"], _new_inbound_token())
    return {"ok": True, "id": res["id"], "name": new_name, "steps": len(steps),
            "bot_id": target_bot}


# ── Выгрузка и загрузка сценария ────────────────────────────────────────────
# Сценарий — текст JSON: им делятся между аккаунтами, хранят как резервную
# копию, переносят на новый проект. Загрузка проверяет ВСЁ тем же языком,
# что экран (validate_steps / validate_trigger / validate_goal): JSON пришёл
# снаружи, и сценарий, которого экран не умеет показать, не пишется.
EXPORT_FORMAT = "infragram.workflow"
EXPORT_VERSION = 1
MAX_IMPORT_BYTES = 256 * 1024


async def export_workflow(pool: asyncpg.Pool, owner_id: int, workflow_id: int) -> dict:
    """Сценарий владельца как переносимый JSON. LookupError — нет такого."""
    src = await pool.fetchrow(
        """SELECT name, description, steps, trigger_type, trigger_value,
                  allow_reentry, goal_type, goal_value, send_window, legacy_kind,
                  max_per_day
             FROM workflow_definitions WHERE id=$1 AND owner_id=$2""",
        workflow_id, owner_id)
    if not src:
        raise LookupError("Сценарий не найден")
    if src["legacy_kind"]:
        raise ValueError("Это воронка из раздела «Воронки» — выгружать её там")
    steps = src["steps"]
    if isinstance(steps, str):
        steps = json.loads(steps or "[]")
    return {
        "format": EXPORT_FORMAT, "version": EXPORT_VERSION,
        "name": src["name"], "description": src["description"] or "",
        "steps": steps or [],
        "trigger_type": src["trigger_type"] or "manual",
        "trigger_value": src["trigger_value"] or "",
        "allow_reentry": bool(src["allow_reentry"]),
        "goal_type": src["goal_type"] or "", "goal_value": src["goal_value"] or "",
        "send_window": src["send_window"] or "",
        "max_per_day": src["max_per_day"] or 0,
    }


def _portable_steps(steps: list) -> tuple[list, int]:
    """Шаги, ссылающиеся на сценарии ДРУГОГО аккаунта по номеру, смысла в
    этом аккаунте не имеют: «Запустить сценарий #12» запустил бы чужой по
    номеру или ничего. Такой шаг становится уведомлением владельцу «укажите
    номер», а «снять со сценария #12» — «снять со всех»: число шагов не
    меняется, переходы по номерам остаются верными. ЧИСТАЯ."""
    out, changed = [], 0
    for st in steps:
        st = dict(st) if isinstance(st, dict) else st
        if isinstance(st, dict) and st.get("type") == "action":
            if st.get("action") == "start_workflow":
                st = {"type": "action", "action": "notify_owner",
                      "value": f"Здесь сценарий запускал сценарий #{st.get('value')} "
                               "из другого аккаунта — замените шаг на свой"}
                changed += 1
            elif st.get("action") == "stop_workflows" and st.get("value"):
                st = {"type": "action", "action": "stop_workflows"}
                changed += 1
        out.append(st)
    return out, changed


async def import_workflow(pool: asyncpg.Pool, owner_id: int, data,
                          bot_id: int | None = None) -> dict:
    """Создать (выключенный) сценарий из выгрузки. ValueError — по-русски.
    Владельца `bot_id` проверяет обработчик."""
    from services import workflow_runtime as _rt
    if isinstance(data, str):
        if len(data.encode("utf-8")) > MAX_IMPORT_BYTES:
            raise ValueError("Выгрузка слишком большая")
        try:
            data = json.loads(data)
        except ValueError:
            raise ValueError("Это не выгрузка сценария: текст не разбирается как JSON")
    if not isinstance(data, dict) or data.get("format") != EXPORT_FORMAT:
        raise ValueError("Это не выгрузка сценария Infragram")
    if int(data.get("version") or 0) > EXPORT_VERSION:
        raise ValueError("Выгрузка из более новой версии — обновите приложение")
    name = str(data.get("name") or "").strip()[:110] or "Сценарий"
    raw_steps = data.get("steps")
    if not isinstance(raw_steps, list):
        raise ValueError("В выгрузке нет шагов")
    portable, changed = _portable_steps(raw_steps)
    steps = validate_steps(portable)
    ttype, tval = _rt.validate_trigger(data.get("trigger_type"), data.get("trigger_value"))
    if ttype in ("inactive", "schedule") and bot_id is None:
        ttype, tval = None, None
    gtype, gval = _rt.validate_goal(data.get("goal_type"), data.get("goal_value"))
    win = _rt.parse_send_window(data.get("send_window") or None)
    try:
        cap = int(data.get("max_per_day") or 0) or None
    except (TypeError, ValueError):
        raise ValueError("Предел сообщений в сутки — целое число")
    if cap is not None and not 1 <= cap <= _rt.MAX_PER_DAY_LIMIT:
        raise ValueError(f"Предел сообщений в сутки — от 1 до {_rt.MAX_PER_DAY_LIMIT}")
    res = await create_workflow(pool, owner_id, f"{name} (загружен)"[:120],
                                description=str(data.get("description") or "")[:500],
                                steps=steps, is_active=False, bot_id=bot_id)
    if not res.get("ok"):
        raise RuntimeError(res.get("error") or "сценарий не создан")
    await pool.execute(
        """UPDATE workflow_definitions
              SET trigger_type=$2, trigger_value=$3, allow_reentry=$4,
                  goal_type=$5, goal_value=$6, send_window=$7, max_per_day=$8
            WHERE id=$1""",
        res["id"], ttype, tval, bool(data.get("allow_reentry")), gtype, gval,
        f"{win[0]:02d}-{win[1]:02d}" if win else None, cap)
    return {"ok": True, "id": res["id"], "steps": len(steps), "adapted_steps": changed,
            "trigger_type": ttype or "manual"}


async def get_workflows(pool: asyncpg.Pool, owner_id: int) -> list:
    """Get all workflow definitions."""
    try:
        rows = await pool.fetch(
            'SELECT * FROM workflow_definitions WHERE owner_id = $1 ORDER BY name',
            owner_id)
        return [dict(r) for r in rows]
    except Exception as e:
        log.warning("get_workflows error: %s", e)
        return []


async def execute_workflow(
    pool: asyncpg.Pool,
    owner_id: int,
    workflow_id: int,
    input_data: dict | None = None,
    chat_id: int | None = None,
) -> dict:
    """Запустить сценарий вручную — тестовый прогон.

    Раньше здесь только заводилась строка `workflow_runs` со статусом
    running, и шаги не выполнял никто. Теперь прогон исполняет
    `services/workflow_runtime.py`.

    Получатель: `chat_id` (подписчик бота) или, по умолчанию, сам владелец —
    так он проверяет сценарий на себе. Telegram не даёт боту написать
    человеку первым, поэтому получатель обязан хотя бы раз открыть бота.
    Сценарий без бота исполняется без получателя: в нём имеют смысл только
    действия и вебхуки.
    """
    from services import workflow_runtime as _rt
    wf = await pool.fetchrow(
        'SELECT * FROM workflow_definitions WHERE id = $1 AND owner_id = $2',
        workflow_id, owner_id)
    if not wf:
        return {"ok": False, "error": "Воркфлоу не найден", "code": 404}
    wf = dict(wf)
    if wf.get("draft_steps") is not None:
        # Тестовый прогон проверяет то, что владелец сейчас собирает: черновик.
        # Люди по-прежнему проходят опубликованные шаги.
        wf["steps"] = wf["draft_steps"]
        wf["steps_version"] = None
    total_steps = len(_rt._as_list(wf.get("steps")))
    if not total_steps:
        return {"ok": False, "error": "В сценарии нет шагов", "code": 400}
    bot_id = wf.get("bot_id")
    target = None
    if bot_id:
        target = int(chat_id or owner_id)
        known = await pool.fetchval(
            "SELECT 1 FROM bot_users WHERE bot_id=$1 AND user_id=$2",
            bot_id, target)
        if not known:
            who = "Получатель" if chat_id else "Вы"
            return {"ok": False, "code": 400,
                    "error": f"{who} ещё не открывали этого бота. Откройте бота "
                             "и нажмите /start — Telegram не даёт боту написать "
                             "первым."}
    run_id = await _rt.start_run(
        pool, wf, bot_id, target,
        input_data={"event_type": "manual", **(input_data or {})}, force=True)
    if not run_id:
        return {"ok": False, "code": 409,
                "error": "Этот сценарий уже идёт у получателя — дождитесь "
                         "окончания или отмените прогон"}
    return {"ok": True, "run_id": run_id, "total_steps": total_steps,
            "chat_id": target}


async def delete_step(pool: asyncpg.Pool, owner_id: int, workflow_id: int,
                      index: int) -> int | None:
    """Удалить шаг по индексу (с нуля). Число оставшихся шагов или None —
    нет такого сценария или шага.

    Оставшиеся шаги проходят ту же проверку словарём, что и при записи: в
    базе могли лежать шаги со старых времён, и пересохранение не должно
    увековечить их.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            target = await _edit_target(conn, owner_id, workflow_id)
            if target is None:
                return None
            column, steps = target
            if not 0 <= index < len(steps):
                return None
            old_len = len(steps)
            del steps[index]
            # Переходы «на шаг N» — номера, и без пересчёта удаление шага
            # молча уводило кнопки и ветки на соседний шаг.
            steps = remap_jumps(steps, lambda n: _after_delete(n, index + 1, old_len))
            kept = []
            for i, st in enumerate(steps):
                try:
                    kept.append(validate_steps([st])[0])
                except ValueError:
                    log.info("delete_step: шаг %s сценария %s со старым языком "
                             "отброшен", i + 1, workflow_id)
            await conn.execute(
                f"UPDATE workflow_definitions SET {column}=$1::jsonb, updated_at=NOW() "
                "WHERE id=$2 AND owner_id=$3", json.dumps(kept), workflow_id, owner_id)
    return len(kept)


# ── Переходы при правке ─────────────────────────────────────────────────────
# Переход хранится НОМЕРОМ шага (с единицы): `goto` действия «перейти»,
# `else_goto`, `timeout_goto`, `paid_goto`, `goto` кнопок и веток A/B.
# Удаление, вставка и перестановка меняют номера, и каждый переход обязан
# поехать вместе со своим шагом. Номер за концом сценария («шаг, который ещё
# не добавлен», исполнитель читает его как «завершить») не трогаем.

_JUMP_KEYS = ("goto", "else_goto", "timeout_goto", "paid_goto")


def remap_jumps(steps: list, f) -> list:
    """Копия шагов, где каждый номер перехода n заменён на f(n). ЧИСТАЯ."""
    def _one(n):
        try:
            k = int(n)
        except (TypeError, ValueError):
            return n
        return f(k) if k >= 1 else n

    out = []
    for st in steps:
        if not isinstance(st, dict):
            out.append(st)
            continue
        st = json.loads(json.dumps(st))
        for key in _JUMP_KEYS:
            if st.get(key) not in (None, "", 0, "0"):
                st[key] = _one(st[key])
        for listkey in ("buttons", "branches"):
            for b in st.get(listkey) or []:
                if isinstance(b, dict) and b.get("goto") not in (None, "", 0, "0"):
                    b["goto"] = _one(b["goto"])
        # Действие «перейти» со старых времён могло хранить номер в value.
        if st.get("type") == "action" and st.get("action") == "goto" \
                and st.get("value") not in (None, "") and "goto" not in st:
            st["value"] = str(_one(st["value"]))
        out.append(st)
    return out


def _after_delete(n: int, deleted: int, old_len: int) -> int:
    """Номер шага n после удаления шага `deleted` (оба с единицы).

    Переход на удалённый шаг ведёт на тот, что встал на его место: человек
    идёт дальше по сценарию, а не проваливается в конец."""
    if n > old_len or n < deleted:
        return n
    if n == deleted:
        return deleted
    return n - 1


def _after_permutation(order: list[int], old_len: int):
    """f(n) для новой расстановки: order[новый_индекс] = старый_индекс."""
    where = {old: new for new, old in enumerate(order)}

    def f(n: int) -> int:
        if n > old_len:
            return n
        return where.get(n - 1, n - 1) + 1
    return f


async def update_step(pool: asyncpg.Pool, owner_id: int, workflow_id: int,
                      index: int, raw_step: dict) -> tuple[dict, bool] | None:
    """Заменить шаг на месте. (шаг, ушёл ли в черновик) или None — нет
    сценария или шага. ValueError — шаг не прошёл проверку.

    Раньше исправить текст шага было нельзя вовсе: только удалить и добавить
    в конец, сломав порядок. Шаблоны с текстами-заготовками так и оставались
    заготовками."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            target = await _edit_target(conn, owner_id, workflow_id)
            if target is None:
                return None
            column, steps = target
            if not 0 <= index < len(steps):
                return None
            step = validate_step(raw_step, index)
            steps[index] = step
            await conn.execute(
                f"UPDATE workflow_definitions SET {column}=$1::jsonb, updated_at=NOW() "
                "WHERE id=$2 AND owner_id=$3", json.dumps(steps), workflow_id, owner_id)
    return step, column == "draft_steps"


async def move_step(pool: asyncpg.Pool, owner_id: int, workflow_id: int,
                    index: int, to: int) -> tuple[int, bool] | None:
    """Переставить шаг с места `index` на место `to` (оба с нуля), переходы
    едут вместе с шагами. (новое место, черновик ли) или None — нет
    сценария или такого шага."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            target = await _edit_target(conn, owner_id, workflow_id)
            if target is None:
                return None
            column, steps = target
            n = len(steps)
            if not 0 <= index < n:
                return None
            to = max(0, min(int(to), n - 1))
            order = list(range(n))
            order.insert(to, order.pop(index))
            moved = [steps[i] for i in order]
            moved = remap_jumps(moved, _after_permutation(order, n))
            await conn.execute(
                f"UPDATE workflow_definitions SET {column}=$1::jsonb, updated_at=NOW() "
                "WHERE id=$2 AND owner_id=$3", json.dumps(moved), workflow_id, owner_id)
    return to, column == "draft_steps"


# ── Проверка перед включением ───────────────────────────────────────────────
# Включить можно было что угодно: сценарий без шагов, с сообщениями без бота,
# с удалённым ботом — он числился «🟢 Активен» и не делал ничего, а владелец
# узнавал об этом от подписчиков. Теперь включение проходит проверку.

_SENDS = frozenset({"message", "ask", "invoice", "ai"})


def _template_texts() -> set[str]:
    return {st["text"] for tpl in WORKFLOW_TEMPLATES.values()
            for st in tpl["steps"] if st.get("text")}


def check_steps(steps: list, trigger_type: str | None) -> list[dict]:
    """Проблемы шагов без БД: [{level: 'block'|'warn', text}]. ЧИСТАЯ."""
    out: list[dict] = []
    steps = _steps_list(steps)
    if not steps:
        out.append({"level": "block", "text": "В сценарии нет шагов — добавьте хотя бы один"})
        return out
    n = len(steps)
    tpl_texts = _template_texts()
    for i, st in enumerate(steps, 1):
        if not isinstance(st, dict):
            continue
        if st.get("type") in ("message", "ask") and st.get("text") in tpl_texts:
            out.append({"level": "warn",
                        "text": f"Шаг {i}: текст из шаблона не изменён — люди получат заготовку"})
        jumps = [st.get(k) for k in _JUMP_KEYS]
        jumps += [b.get("goto") for b in (st.get("buttons") or []) + (st.get("branches") or [])
                  if isinstance(b, dict)]
        for j in jumps:
            try:
                k = int(j)
            except (TypeError, ValueError):
                continue
            if k > n:
                out.append({"level": "warn",
                            "text": f"Шаг {i} ведёт на шаг {k}, которого нет — "
                                    "на этом месте сценарий закончится"})
                break
    if (trigger_type or "manual") == "manual":
        out.append({"level": "warn",
                    "text": "Способ запуска — «Только вручную»: сам сценарий не "
                            "запустится, только тестом или из другого сценария"})
    return out


async def activation_problems(pool: asyncpg.Pool, owner_id: int,
                              workflow_id: int) -> list[dict] | None:
    """Почему сценарий не заработает, если его включить. None — сценария нет.

    'block' — включать нельзя (отказ с причиной), 'warn' — включить можно,
    но владелец должен увидеть предупреждение до включения."""
    row = await pool.fetchrow(
        """SELECT steps, draft_steps, bot_id, trigger_type
             FROM workflow_definitions WHERE id=$1 AND owner_id=$2""",
        workflow_id, owner_id)
    if row is None:
        return None
    steps = _steps_list(row["steps"])
    problems = []
    if not steps and row["draft_steps"] is not None and _steps_list(row["draft_steps"]):
        problems.append({"level": "block",
                         "text": "Шаги есть только в черновике — сначала опубликуйте его"})
    else:
        problems.extend(check_steps(steps, row["trigger_type"]))
    sends = any(isinstance(s, dict) and s.get("type") in _SENDS for s in steps)
    if sends:
        if row["bot_id"]:
            ok = await pool.fetchval(
                """SELECT 1 FROM managed_bots
                    WHERE bot_id=$1 AND added_by=$2 AND is_active=TRUE""",
                row["bot_id"], owner_id)
            if not ok:
                problems.append({"level": "block",
                                 "text": "Бот сценария отключён или удалён — "
                                         "привяжите сценарий к работающему боту"})
        else:
            have = await pool.fetchval(
                "SELECT 1 FROM managed_bots WHERE added_by=$1 AND is_active=TRUE LIMIT 1",
                owner_id)
            if not have:
                problems.append({"level": "block",
                                 "text": "Нет ни одного подключённого бота — "
                                         "сообщения сценария отправлять некому"})
    return problems


def blocking_text(problems: list[dict]) -> str | None:
    """Текст отказа во включении или None, если блокирующих проблем нет."""
    blocks = [p["text"] for p in problems or [] if p.get("level") == "block"]
    if not blocks:
        return None
    return "Сценарий не заработает: " + "; ".join(blocks)


# ── Черновик и публикация ───────────────────────────────────────────────────
# Включённый сценарий проходят живые люди. Правка его шагов идёт в черновик
# (`draft_steps`), а люди проходят опубликованные `steps`, пока владелец не
# нажмёт «Опубликовать». Выключенный сценарий никто не проходит — его шаги
# правятся напрямую, без лишнего шага для нового сценария.

def _steps_list(raw) -> list:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "[]")
        except Exception:
            return []
    return list(raw) if isinstance(raw, list) else []


async def _edit_target(conn, owner_id: int, workflow_id: int) -> tuple[str, list] | None:
    """Что правит владелец: ('draft_steps' | 'steps', текущие шаги). None —
    сценария нет у владельца. Вызывать в транзакции: строка блокируется."""
    row = await conn.fetchrow(
        """SELECT steps, draft_steps, COALESCE(is_active, FALSE) AS active
             FROM workflow_definitions
            WHERE id=$1 AND owner_id=$2 FOR UPDATE""", workflow_id, owner_id)
    if row is None:
        return None
    if row.get("draft_steps") is not None:
        return "draft_steps", _steps_list(row["draft_steps"])
    if row.get("active"):
        return "draft_steps", _steps_list(row["steps"])
    return "steps", _steps_list(row["steps"])


async def append_step(pool: asyncpg.Pool, owner_id: int, workflow_id: int,
                      raw_step: dict) -> tuple[dict, bool] | None:
    """Добавить шаг. (шаг, ушёл ли в черновик) или None — сценария нет.
    ValueError — шаг не прошёл проверку."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            target = await _edit_target(conn, owner_id, workflow_id)
            if target is None:
                return None
            column, steps = target
            # Уже сохранённые шаги считаются: предел на сценарий имеет смысл
            # только вместе с ними.
            step = validate_steps([raw_step], already=len(steps))[0]
            steps.append(step)
            await conn.execute(
                f"UPDATE workflow_definitions SET {column}=$1::jsonb, updated_at=NOW() "
                "WHERE id=$2 AND owner_id=$3", json.dumps(steps), workflow_id, owner_id)
    return step, column == "draft_steps"


async def publish_draft(pool: asyncpg.Pool, owner_id: int,
                        workflow_id: int) -> int | None:
    """Опубликовать черновик: новые люди пойдут по нему. Номер версии или
    None — черновика нет. Идущие прогоны доходят по своей версии (снимок)."""
    return await pool.fetchval(
        """UPDATE workflow_definitions
              SET steps = draft_steps, draft_steps = NULL,
                  steps_version = COALESCE(steps_version, 1) + 1,
                  published_at = now(), updated_at = now()
            WHERE id=$1 AND owner_id=$2 AND draft_steps IS NOT NULL
        RETURNING steps_version""", workflow_id, owner_id)


async def discard_draft(pool: asyncpg.Pool, owner_id: int, workflow_id: int) -> bool:
    """Выбросить черновик. False — черновика нет."""
    res = await pool.execute(
        """UPDATE workflow_definitions SET draft_steps=NULL, updated_at=now()
            WHERE id=$1 AND owner_id=$2 AND draft_steps IS NOT NULL""",
        workflow_id, owner_id)
    return isinstance(res, str) and res.endswith(" 1")


async def set_trigger(pool: asyncpg.Pool, owner_id: int, workflow_id: int,
                      trigger_type: str | None, trigger_value: str | None,
                      allow_reentry: bool | None = None) -> bool:
    """Записать способ запуска. False — сценарий не найден у владельца.

    Запуск по аудитории («не заходил N дней», расписание) идёт по
    подписчикам ОДНОГО бота: сценарий без бота так не запустится никогда,
    и это отказ при сохранении, а не тихое «ничего не происходит»."""
    if trigger_type in ("inactive", "schedule", "webhook"):
        bot_id = await pool.fetchval(
            "SELECT bot_id FROM workflow_definitions WHERE id=$1 AND owner_id=$2",
            workflow_id, owner_id)
        if bot_id is None:
            raise ValueError("Этот способ запуска работает по подписчикам бота: "
                             "сначала привяжите сценарий к боту")
    res = await pool.execute(
        """UPDATE workflow_definitions
              SET trigger_type=$3, trigger_value=$4,
                  allow_reentry=COALESCE($5, allow_reentry), updated_at=NOW(),
                  inbound_token=CASE WHEN $3='webhook'
                                     THEN COALESCE(inbound_token, $6) END
            WHERE id=$1 AND owner_id=$2""",
        workflow_id, owner_id, trigger_type, trigger_value, allow_reentry,
        _new_inbound_token())
    return not (isinstance(res, str) and res.rsplit(" ", 1)[-1] == "0")


def _new_inbound_token() -> str:
    import secrets
    return secrets.token_urlsafe(24)


async def rotate_inbound_token(pool: asyncpg.Pool, owner_id: int,
                               workflow_id: int) -> str | None:
    """Новый адрес входящего вебхука; старый сразу перестаёт работать.
    None — сценария нет у владельца или он запускается не вебхуком."""
    return await pool.fetchval(
        """UPDATE workflow_definitions SET inbound_token=$3, updated_at=NOW()
            WHERE id=$1 AND owner_id=$2 AND trigger_type='webhook'
        RETURNING inbound_token""",
        workflow_id, owner_id, _new_inbound_token())


def inbound_url(public_base: str, token: str | None) -> str:
    """Адрес, на который внешняя система шлёт запуск сценария."""
    return f"{public_base.rstrip('/')}/webhook/scenario/{token}" if token else ""


async def set_send_window(pool: asyncpg.Pool, owner_id: int, workflow_id: int,
                          value: str | None) -> str | None:
    """Окно отправки «ЧЧ-ЧЧ» по времени человека (пусто — в любое время).
    Возвращает сохранённое значение. ValueError — мусор, LookupError — нет
    сценария у владельца."""
    from services import workflow_runtime as _rt
    win = _rt.parse_send_window(value)
    stored = f"{win[0]:02d}-{win[1]:02d}" if win else None
    res = await pool.execute(
        "UPDATE workflow_definitions SET send_window=$3, updated_at=NOW() "
        "WHERE id=$1 AND owner_id=$2", workflow_id, owner_id, stored)
    if isinstance(res, str) and res.endswith(" 0"):
        raise LookupError("Сценарий не найден")
    return stored


async def set_max_per_day(pool: asyncpg.Pool, owner_id: int, workflow_id: int,
                          value) -> int | None:
    """Предел сообщений человеку за сутки от всех сценариев бота (пусто или
    0 — без предела). ValueError — мусор, LookupError — нет сценария."""
    from services import workflow_runtime as _rt
    raw = str(value if value is not None else "").strip()
    cap = None
    if raw and raw != "0":
        try:
            cap = int(raw)
        except ValueError:
            raise ValueError("Предел сообщений в сутки — целое число")
        if not 1 <= cap <= _rt.MAX_PER_DAY_LIMIT:
            raise ValueError(f"Предел сообщений в сутки — от 1 до {_rt.MAX_PER_DAY_LIMIT}")
    res = await pool.execute(
        "UPDATE workflow_definitions SET max_per_day=$3, updated_at=NOW() "
        "WHERE id=$1 AND owner_id=$2", workflow_id, owner_id, cap)
    if isinstance(res, str) and res.endswith(" 0"):
        raise LookupError("Сценарий не найден")
    return cap


async def set_goal(pool: asyncpg.Pool, owner_id: int, workflow_id: int,
                   goal_type: str | None, goal_value: str | None) -> bool:
    """Записать цель сценария. False — сценарий не найден у владельца.

    Цель действует и на уже идущие прогоны: она читается из определения в
    момент события, а не из снимка шагов."""
    res = await pool.execute(
        """UPDATE workflow_definitions
              SET goal_type=$3, goal_value=$4, updated_at=NOW()
            WHERE id=$1 AND owner_id=$2""",
        workflow_id, owner_id, goal_type, goal_value)
    return not (isinstance(res, str) and res.rsplit(" ", 1)[-1] == "0")


async def get_workflow_status(
    pool: asyncpg.Pool,
    owner_id: int,
    run_id: int,
) -> Optional[dict]:
    """Get workflow run status."""
    try:
        row = await pool.fetchrow(
            '''SELECT wr.*, wd.name as workflow_name
               FROM workflow_runs wr
               LEFT JOIN workflow_definitions wd ON wd.id = wr.workflow_id
               WHERE wr.id = $1 AND wr.owner_id = $2''',
            run_id, owner_id)
        if not row:
            return None
        return dict(row)
    except Exception as e:
        log.warning("get_workflow_status error: %s", e)
        return None


async def _set_active(pool: asyncpg.Pool, owner_id: int, workflow_id: int,
                      active: bool) -> dict:
    """Включить/выключить воркфлоу. Нет такого у владельца → LookupError.

    Пауза — это `is_active`, ровно как в PATCH /workflows/{id}: два способа
    нажать одну кнопку не должны означать разное.
    """
    res = await pool.execute(
        "UPDATE workflow_definitions SET is_active=$1, updated_at=NOW() "
        "WHERE id=$2 AND owner_id=$3", active, workflow_id, owner_id)
    # asyncpg возвращает тег команды вида 'UPDATE 0' — ноль строк значит, что
    # воркфлоу либо не существует, либо принадлежит другому владельцу. Разницу
    # НЕ раскрываем: иначе по коду ответа можно перебирать чужие id.
    if isinstance(res, str) and res.rsplit(" ", 1)[-1] == "0":
        raise LookupError("Воркфлоу не найден")
    return {"active": active}


async def pause_workflow(pool: asyncpg.Pool, owner_id: int, workflow_id: int) -> dict:
    """Поставить воркфлоу на паузу (POST /workflow/{id}/pause)."""
    return await _set_active(pool, owner_id, workflow_id, False)


async def resume_workflow(pool: asyncpg.Pool, owner_id: int, workflow_id: int) -> dict:
    """Снять воркфлоу с паузы (POST /workflow/{id}/resume)."""
    return await _set_active(pool, owner_id, workflow_id, True)


async def delete_workflow(pool: asyncpg.Pool, owner_id: int, workflow_id: int) -> bool:
    """Удалить воркфлоу. False — не найден у этого владельца (обработчик даст 404).

    Незавершённые прогоны отменяем: строки workflow_runs ссылаются на
    определение внешним ключом, и без этого удаление упало бы, а с ON DELETE
    оставило бы «выполняющиеся» прогоны несуществующего воркфлоу.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "UPDATE workflow_runs SET status='cancelled', finished_at=NOW() "
                "WHERE workflow_id=$1 AND owner_id=$2 "
                "AND status IN ('pending','running')", workflow_id, owner_id)
            await conn.execute(
                "UPDATE workflow_runs SET workflow_id=NULL "
                "WHERE workflow_id=$1 AND owner_id=$2", workflow_id, owner_id)
            res = await conn.execute(
                "DELETE FROM workflow_definitions WHERE id=$1 AND owner_id=$2",
                workflow_id, owner_id)
    return not (isinstance(res, str) and res.rsplit(" ", 1)[-1] == "0")


async def cancel_workflow(
    pool: asyncpg.Pool,
    owner_id: int,
    run_id: int,
) -> dict:
    """Остановить прогон: идущий или ждущий ответа человека."""
    try:
        result = await pool.execute(
            '''UPDATE workflow_runs
               SET status = 'cancelled', finished_at = NOW(), wait_kind = NULL,
                   error_message = 'Остановлен владельцем'
               WHERE id = $1 AND owner_id = $2
                 AND status IN ('pending', 'running', 'waiting')''',
            run_id, owner_id)
        cancelled = "UPDATE 1" in result
        return {"ok": cancelled}
    except Exception as e:
        log.warning("cancel_workflow error: %s", e)
        return {"ok": False, "error": str(e)}


# ── Повтор упавших прогонов ─────────────────────────────────────────────────
# Упавший прогон раньше был концом: человек, на котором сломался вебхук или
# отвалился бот, выпадал из сценария навсегда, и владелец, починив причину,
# мог только ждать новых людей. Теперь прогон продолжается С ТОГО ЖЕ ШАГА:
# уже отправленное второй раз не уходит.
RETRY_MAX_AGE_DAYS = 7
RETRY_BATCH = 500


async def retry_runs(pool: asyncpg.Pool, owner_id: int, *,
                     run_id: int | None = None,
                     workflow_id: int | None = None) -> dict:
    """Вернуть упавшие прогоны в работу. {"ok", "retried", "skipped"}.

    Один прогон (`run_id`) или все упавшие сценария за последние
    RETRY_MAX_AGE_DAYS суток (`workflow_id`) — старее не трогаем, писать
    человеку через месяц после сбоя хуже, чем не писать.

    Шаги прогона берутся из ТЕКУЩЕЙ версии сценария, если в ней ещё есть
    шаг, на котором прогон упал: владелец чинит адрес вебхука и жмёт
    «Повторить» — повтор должен пойти по исправленному шагу. Если сценарий
    укоротили так, что этого шага больше нет, остаётся снимок со старта.

    Прогон человека, у которого уже идёт другой прогон этого сценария, не
    возвращается (`skipped`): одна живая цепочка на человека.
    """
    if (run_id is None) == (workflow_id is None):
        raise ValueError("Укажите прогон или сценарий")
    where = ["r.owner_id = $1", "r.status = 'failed'"]
    args: list[Any] = [owner_id]
    if run_id is not None:
        args.append(int(run_id))
        where.append(f"r.id = ${len(args)}")
    else:
        args.append(int(workflow_id))
        where.append(f"r.workflow_id = ${len(args)}")
        args.append(RETRY_MAX_AGE_DAYS)
        where.append(f"r.finished_at > now() - make_interval(days => ${len(args)})")
    args.append(RETRY_BATCH)
    rows = await pool.fetch(
        f"""SELECT r.id, r.workflow_id, r.current_step, r.bot_id, r.chat_id,
                   d.steps AS current_steps
              FROM workflow_runs r
              LEFT JOIN workflow_definitions d ON d.id = r.workflow_id
             WHERE {' AND '.join(where)}
             ORDER BY r.id LIMIT ${len(args)}""", *args)
    retried = skipped = 0
    for r in rows:
        steps = r["current_steps"]
        if isinstance(steps, str):
            try:
                steps = json.loads(steps)
            except ValueError:
                steps = None
        idx = int(r["current_step"] or 0)
        fresh = json.dumps(steps) if isinstance(steps, list) and idx < len(steps) else None
        try:
            res = await pool.execute(
                """UPDATE workflow_runs r
                      SET status='running', finished_at=NULL, error_message=NULL,
                          fail_count=0, wait_kind=NULL, wait_until=NULL,
                          claimed_until=NULL, next_run_at=now(),
                          vars = vars - '_wh_try' - '_wh_key',
                          steps_snapshot = COALESCE($2::jsonb, steps_snapshot),
                          total_steps = COALESCE(jsonb_array_length($2::jsonb), total_steps)
                    WHERE r.id=$1 AND r.status='failed'
                      AND NOT EXISTS (
                          SELECT 1 FROM workflow_runs o
                           WHERE o.workflow_id = r.workflow_id
                             AND o.bot_id IS NOT DISTINCT FROM r.bot_id
                             AND o.chat_id = r.chat_id AND o.id <> r.id
                             AND o.status IN ('running', 'waiting'))""",
                r["id"], fresh)
        except asyncpg.UniqueViolationError:
            res = "UPDATE 0"
        if isinstance(res, str) and res.endswith(" 1"):
            retried += 1
            try:
                await pool.execute(
                    """INSERT INTO workflow_step_log
                           (run_id, workflow_id, owner_id, step_idx, step_type, outcome, detail)
                       VALUES ($1, $2, $3, $4, 'retry', 'retried', 'Повтор владельцем')""",
                    r["id"], r["workflow_id"] or 0, owner_id, idx)
            except Exception:
                log.debug("retry_runs: журнал прогона %s", r["id"], exc_info=True)
        else:
            skipped += 1
    if retried:
        from services import workflow_runtime as _rt
        _rt.wake()
    return {"ok": True, "retried": retried, "skipped": skipped}


# ── Проверка шага ────────────────────────────────────────────────────────────
# Сценарий складывался тремя разными языками шагов. Экран добавления шага и все
# шаблоны говорят `{"type": ...}` — и экран деталей умеет рисовать только это:
# `s.type.toUpperCase()`. Обработчик `POST /workflow/create` требовал вместо
# этого ключ `action` и ничего не знал про `type`, а `POST /workflows/{id}/steps`
# принимал ЛЮБОЙ непустой `type`. Словарь допустимых типов (WORKFLOW_STEP_TYPES)
# лежал здесь же с комментарием «шаг неизвестного типа означал бы строку
# ⚫ undefined в деталях сценария» — и не вызывался ниоткуда.
#
# Итог для владельца: шаг, сохранённый без `type`, ронял ВЕСЬ экран деталей
# (TypeError на undefined.toUpperCase попадал в общий catch, и вместо сценария
# показывалась ошибка), а шаг с опечаткой в типе превращался в безымянную
# точку. Язык шагов теперь один, и проверяет его одна функция.

# Предел текста шага — предел сообщения Telegram: писать в шаг больше, чем
# можно отправить, значит заведомо хранить неотправимое.
MAX_STEP_TEXT = 4096
# Задержка между шагами. Экран предлагает вводить до суток (max=1440), но
# шаблон «Дрип-серия» штатно ставит 2880 минут — двое суток, и это нормальная
# дрип-серия, а не ошибка. Поэтому предел здесь шире экранного и означает
# другое: 30 суток — верхняя граница осмысленного, за которой значение почти
# наверняка опечатка (минуты вписали вместо дней, секунды вместо минут).
MAX_STEP_DELAY_MIN = 30 * 24 * 60
# Шагов на сценарий. Шаги лежат ОДНОЙ jsonb-колонкой и целиком читаются на
# каждом открытии экрана: без предела один сценарий растёт без границ и
# утягивает за собой время ответа всего списка.
MAX_WORKFLOW_STEPS = 50


def _goto(step: dict, key: str, where: str) -> int | None:
    """Номер шага для перехода (с единицы) или None. ValueError на мусоре."""
    raw = step.get(key)
    if raw in (None, "", 0, "0"):
        return None
    try:
        n = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{where}: переход — это номер шага, а не «{raw}»")
    if not 1 <= n <= MAX_WORKFLOW_STEPS:
        raise ValueError(f"{where}: переход на шаг от 1 до {MAX_WORKFLOW_STEPS}")
    return n


def _field_name(raw, where: str) -> str:
    name = str(raw or "").strip().lower()
    if not _FIELD_RE.match(name):
        raise ValueError(
            f"{where}: имя поля — латиница, цифры и «_», до 32 символов, "
            "начинается с буквы (например name, phone, city)")
    return name


def _buttons(raw, where: str) -> list[dict]:
    """Кнопки сообщения: ссылка ({text,url}) или ветка ({text,goto})."""
    if raw in (None, "", []):
        return []
    if not isinstance(raw, list):
        raise ValueError(f"{where}: кнопки — список")
    if len(raw) > MAX_BUTTONS:
        raise ValueError(f"{where}: не больше {MAX_BUTTONS} кнопок")
    out = []
    for j, b in enumerate(raw):
        bw = f"{where}, кнопка {j + 1}"
        if not isinstance(b, dict):
            raise ValueError(f"{bw}: ожидается объект")
        text = str(b.get("text") or "").strip()
        if not text:
            raise ValueError(f"{bw}: нет текста")
        if len(text) > MAX_BUTTON_TEXT:
            raise ValueError(f"{bw}: текст длиннее {MAX_BUTTON_TEXT} символов")
        url = str(b.get("url") or "").strip()
        if url:
            if not url.startswith(("https://", "http://", "tg://")):
                raise ValueError(f"{bw}: ссылка должна начинаться с https://")
            out.append({"text": text, "url": url[:500]})
            continue
        goto = _goto(b, "goto", bw)
        # Кнопка без перехода ведёт на следующий шаг — это нормальная
        # кнопка «Дальше», а не ошибка.
        out.append({"text": text, "goto": goto} if goto else {"text": text})
    return out


def validate_step(step, index: int = 0) -> dict:
    """Проверить и нормализовать один шаг сценария.

    Возвращает НОВЫЙ словарь с полями, которые экран умеет показать, а
    исполнитель (`services/workflow_runtime.py`) — выполнить. Непонятный
    шаг — ValueError с русским текстом для владельца (обработчики отдают его
    как 400), а не тихая запись в jsonb.

    Валидация живёт рядом со словарём типов, а не внутри `create_workflow`:
    `create_workflow` — тонкая запись в БД, её зовут и миграции, и тесты
    движка. Вызывать проверку обязаны все пути записи из мини-аппа, и за это
    есть храповик в tests/test_workflow_steps_speak_one_language.py.

    Переходы (`goto`, `else_goto`, `timeout_goto`, `paid_goto`) — номера
    шагов с единицы, как их видит владелец на экране. Шаг, на который
    ссылаются, может быть ещё не добавлен: сценарий собирают по одному шагу.
    Переход за конец сценария исполнитель понимает как «завершить».
    """
    where = f"Шаг {index + 1}"
    if not isinstance(step, dict):
        raise ValueError(f"{where}: ожидается объект с полем type")

    stype = str(step.get("type") or "").strip().lower()
    if not stype:
        raise ValueError(
            f"{where}: укажите type — один из {', '.join(sorted(WORKFLOW_STEP_TYPES))}")
    if stype not in WORKFLOW_STEP_TYPES:
        raise ValueError(
            f"{where}: тип «{stype}» экран не умеет показать. Допустимые: "
            + ", ".join(sorted(WORKFLOW_STEP_TYPES)))

    out: dict[str, Any] = {"type": stype}

    def _text(required: bool = True) -> str:
        text = str(step.get("text") or "").strip()
        if required and not text:
            raise ValueError(f"{where}: сообщение без текста")
        if len(text) > MAX_STEP_TEXT:
            raise ValueError(
                f"{where}: текст длиннее {MAX_STEP_TEXT} символов — "
                "Telegram такое сообщение не примет")
        return text

    if stype == "message":
        media = str(step.get("media") or "").strip()
        # С картинкой или файлом текст — подпись: её можно не писать.
        out["text"] = _text(required=not media)
        buttons = _buttons(step.get("buttons"), where)
        if buttons:
            out["buttons"] = buttons
        if media:
            kind = str(step.get("media_kind") or "photo").strip().lower()
            if kind not in MEDIA_KINDS:
                raise ValueError(f"{where}: вложение — одно из: " + ", ".join(MEDIA_KINDS.values()))
            if not (media.startswith("https://") or _FILE_ID_RE.match(media)):
                raise ValueError(f"{where}: вложение — ссылка https:// или file_id Telegram")
            if len(media) > 500:
                raise ValueError(f"{where}: ссылка на вложение длиннее 500 символов")
            if len(out["text"]) > MAX_CAPTION:
                raise ValueError(f"{where}: подпись к вложению длиннее {MAX_CAPTION} символов — "
                                 "Telegram её не примет; вынесите текст в отдельный шаг")
            out["media"] = media
            out["media_kind"] = kind

    elif stype == "delay":
        until = str(step.get("until_time") or "").strip()
        ufield = str(step.get("until_field") or "").strip()
        if ufield:
            # Напоминание к дате из поля: «за час до записи» — поле appointment,
            # сдвиг -60. Дату пишет человек или вебхук, разбирается при исполнении.
            out["until_field"] = _field_name(ufield, where)
            try:
                offset = int(step.get("offset_minutes") or 0)
            except (TypeError, ValueError):
                raise ValueError(f"{where}: сдвиг — число минут, например -60")
            if abs(offset) > MAX_STEP_DELAY_MIN:
                raise ValueError(f"{where}: сдвиг не больше {MAX_STEP_DELAY_MIN // 1440} суток")
            out["offset_minutes"] = offset
        elif until:
            # Умная задержка: до ближайшего «ЧЧ:ММ» по времени человека,
            # по желанию только в будни.
            if not _HHMM_RE.match(until):
                raise ValueError(f"{where}: время задержки — в виде ЧЧ:ММ, например 10:00")
            out["until_time"] = until.zfill(5)
            out["weekdays_only"] = bool(step.get("weekdays_only"))
            tz = str(step.get("tz") or "").strip()
            if tz:
                try:
                    from zoneinfo import ZoneInfo
                    ZoneInfo(tz)
                except Exception:
                    raise ValueError(f"{where}: неизвестный часовой пояс «{tz}»")
                out["tz"] = tz
        else:
            try:
                minutes = int(step.get("delay_minutes"))
            except (TypeError, ValueError):
                raise ValueError(f"{where}: задержка в минутах не указана")
            if not 1 <= minutes <= MAX_STEP_DELAY_MIN:
                raise ValueError(
                    f"{where}: задержка от 1 до {MAX_STEP_DELAY_MIN} минут "
                    f"({MAX_STEP_DELAY_MIN // 1440} суток)")
            out["delay_minutes"] = minutes

    elif stype == "split":
        raw = step.get("branches")
        if not isinstance(raw, list) or not 2 <= len(raw) <= 4:
            raise ValueError(f"{where}: A/B-деление — от 2 до 4 веток")
        branches = []
        for i, b in enumerate(raw):
            if not isinstance(b, dict):
                raise ValueError(f"{where}: ветка {i + 1} описана неверно")
            try:
                weight = int(b.get("weight") or 0)
            except (TypeError, ValueError):
                raise ValueError(f"{where}: доля ветки {i + 1} — целое число")
            if not 1 <= weight <= 100:
                raise ValueError(f"{where}: доля ветки {i + 1} от 1 до 100")
            goto = _goto(b, "goto", where)
            if goto is None:
                raise ValueError(f"{where}: у ветки {i + 1} нет шага перехода")
            label = str(b.get("label") or chr(ord("A") + i)).strip()[:20]
            branches.append({"label": label, "weight": weight, "goto": goto})
        out["branches"] = branches

    elif stype == "condition":
        cond = str(step.get("condition") or "").strip().lower()
        if not cond:
            raise ValueError(f"{where}: условие не выбрано")
        if cond not in WORKFLOW_CONDITIONS:
            raise ValueError(
                f"{where}: условие «{cond}» исполнитель проверить не умеет. "
                "Допустимые: " + ", ".join(sorted(WORKFLOW_CONDITIONS)))
        out["condition"] = cond
        value = str(step.get("condition_value") or "").strip()
        if cond in _FIELD_COMPARE:
            fname, sep, want = value.partition("=")
            if not sep or not want.strip():
                raise ValueError(f"{where}: значение условия — «поле=значение»")
            if cond in ("field_gt", "field_lt") and _to_number(want) is None:
                raise ValueError(f"{where}: для сравнения нужно число, а не «{want.strip()}»")
            value = f"{_field_name(fname, where)}={want.strip()}"
        if cond == "work_hours":
            from services import workflow_runtime as _rt
            try:
                value = _rt.format_work_hours(*_rt.parse_work_hours(value))
            except ValueError as e:
                raise ValueError(f"{where}: {e}")
        if value:
            out["condition_value"] = value[:200]
        elif cond not in ("paid", "event"):
            raise ValueError(f"{where}: для условия «{WORKFLOW_CONDITIONS[cond]}» "
                             "нужно значение")
        else_goto = _goto(step, "else_goto", where)
        if else_goto:
            out["else_goto"] = else_goto

    elif stype == "webhook":
        url = str(step.get("url") or "").strip()
        if not url.startswith("https://"):
            raise ValueError(
                f"{where}: вебхук только по https — по http уйдёт открытым текстом")
        if len(url) > 500:
            raise ValueError(f"{where}: адрес вебхука длиннее 500 символов")
        out["url"] = url
        # Повторы при сбое внешнего сервиса (нет ответа, 5xx, 429): раньше
        # одна неудача — и шаг молча пропускался, заявка в CRM терялась.
        raw_retries = step.get("retries")
        try:
            retries = (WEBHOOK_DEFAULT_RETRIES if raw_retries in (None, "")
                       else int(raw_retries))
        except (TypeError, ValueError):
            raise ValueError(f"{where}: число повторов вебхука — целое число")
        if not 0 <= retries <= WEBHOOK_MAX_RETRIES:
            raise ValueError(
                f"{where}: повторов вебхука от 0 до {WEBHOOK_MAX_RETRIES}")
        out["retries"] = retries
        # Ответ сервиса — в переменные прогона: {{<save_as>_<ключ>}}.
        if str(step.get("save_as") or "").strip():
            out["save_as"] = _field_name(step.get("save_as"), where)
        else_goto = _goto(step, "else_goto", where)
        if else_goto:
            out["else_goto"] = else_goto

    elif stype == "ai":
        # «Ответ ИИ»: инструкция владельца + данные человека → ответ модели.
        # С save_as ответ не отправляется, а пишется в переменную (например,
        # ИИ определяет намерение, а условие дальше ведёт по ветке).
        prompt = str(step.get("text") or step.get("prompt") or "").strip()
        if not prompt:
            raise ValueError(f"{where}: напишите, что должен сделать ИИ")
        if len(prompt) > MAX_AI_PROMPT:
            raise ValueError(f"{where}: инструкция длиннее {MAX_AI_PROMPT} символов")
        out["text"] = prompt
        if str(step.get("save_as") or "").strip():
            out["save_as"] = _field_name(step.get("save_as"), where)
        fallback = str(step.get("fallback") or "").strip()
        if fallback:
            out["fallback"] = fallback[:MAX_STEP_TEXT]

    elif stype == "wait":
        # «Ждать события»: прогон стоит, пока с ЭТИМ человеком не случится
        # событие (оплатил, получил тег, ответил на вопрос другого сценария),
        # или пока не выйдет срок — тогда переход по timeout_goto или конец.
        event = str(step.get("event") or "").strip()
        if not event:
            raise ValueError(f"{where}: выберите, какого события ждать")
        if len(event) > 64 or not re.match(r"^[a-z][a-z0-9_.]*$", event):
            raise ValueError(f"{where}: имя события — как в списке, например payment.received")
        out["event"] = event
        value = str(step.get("value") or "").strip()
        if value:
            out["value"] = value[:200]
        try:
            timeout = int(step.get("timeout_minutes") or 1440)
        except (TypeError, ValueError):
            raise ValueError(f"{where}: срок ожидания в минутах — число")
        if not 1 <= timeout <= MAX_STEP_DELAY_MIN:
            raise ValueError(f"{where}: срок ожидания от 1 до {MAX_STEP_DELAY_MIN} минут")
        out["timeout_minutes"] = timeout
        tgo = _goto(step, "timeout_goto", where)
        if tgo:
            out["timeout_goto"] = tgo

    elif stype == "ask":
        out["text"] = _text()
        out["save_as"] = _field_name(step.get("save_as"), where)
        check = str(step.get("validate") or "any").strip().lower()
        if check not in ASK_VALIDATORS:
            raise ValueError(f"{where}: проверка ответа — одна из "
                             + ", ".join(ASK_VALIDATORS))
        out["validate"] = check
        try:
            timeout = int(step.get("timeout_minutes") or 1440)
        except (TypeError, ValueError):
            raise ValueError(f"{where}: срок ответа в минутах — число")
        if not 1 <= timeout <= MAX_STEP_DELAY_MIN:
            raise ValueError(f"{where}: срок ответа от 1 до {MAX_STEP_DELAY_MIN} минут")
        out["timeout_minutes"] = timeout
        tgo = _goto(step, "timeout_goto", where)
        if tgo:
            out["timeout_goto"] = tgo

    elif stype == "invoice":
        title = str(step.get("title") or "").strip()
        if not title:
            raise ValueError(f"{where}: у счёта нет названия")
        out["title"] = title[:32]  # предел Telegram для title счёта
        desc = str(step.get("text") or step.get("description") or "").strip()
        out["text"] = (desc or title)[:255]  # предел Telegram для description
        currency = str(step.get("currency") or "XTR").strip().upper()
        if currency not in INVOICE_CURRENCIES:
            raise ValueError(f"{where}: валюта — одна из " + ", ".join(INVOICE_CURRENCIES))
        out["currency"] = currency
        try:
            amount = int(step.get("amount"))
        except (TypeError, ValueError):
            raise ValueError(f"{where}: сумма счёта не указана")
        # XTR — целые звёзды; прочие валюты — в копейках/центах (как требует
        # Telegram), предел — 1 млн единиц валюты.
        if not 1 <= amount <= 100_000_000:
            raise ValueError(f"{where}: сумма счёта вне допустимых пределов")
        out["amount"] = amount
        try:
            timeout = int(step.get("timeout_minutes") or 1440)
        except (TypeError, ValueError):
            raise ValueError(f"{where}: срок оплаты в минутах — число")
        if not 1 <= timeout <= MAX_STEP_DELAY_MIN:
            raise ValueError(f"{where}: срок оплаты от 1 до {MAX_STEP_DELAY_MIN} минут")
        out["timeout_minutes"] = timeout
        tgo = _goto(step, "timeout_goto", where)
        if tgo:
            out["timeout_goto"] = tgo

    else:  # action
        name = str(step.get("action") or "").strip().lower()
        if not name:
            raise ValueError(f"{where}: действие не указано")
        if name not in WORKFLOW_ACTIONS:
            raise ValueError(
                f"{where}: действие «{name}» исполнитель выполнить не умеет. "
                "Допустимые: " + ", ".join(sorted(WORKFLOW_ACTIONS)))
        out["action"] = name
        value = str(step.get("value") or "").strip()
        if name == "goto" and not value and step.get("goto") not in (None, ""):
            # Сохранённый шаг «перейти» хранит номер в `goto`, а не в `value`:
            # повторная проверка (удаление соседнего шага, правка) не должна
            # отбрасывать его как «без значения».
            value = str(step.get("goto")).strip()
        if name in _ACTIONS_NEED_VALUE and not value:
            raise ValueError(f"{where}: для действия «{WORKFLOW_ACTIONS[name]}» "
                             "нужно значение")
        if name == "goto":
            out["goto"] = _goto({"goto": value}, "goto", where)
        elif name == "move_deal":
            stage = value.lower()
            stage = {v: k for k, v in DEAL_STAGES.items()}.get(stage, stage)
            if stage not in DEAL_STAGES:
                raise ValueError(f"{where}: этап сделки — один из "
                                 + ", ".join(f"{k} ({v})" for k, v in DEAL_STAGES.items()))
            out["value"] = stage
        elif name in ("start_workflow", "stop_workflows"):
            if value:
                try:
                    n = int(value.lstrip("#"))
                except ValueError:
                    raise ValueError(f"{where}: укажите номер сценария, например 12")
                if n <= 0:
                    raise ValueError(f"{where}: номер сценария — положительное число")
                out["value"] = str(n)
        elif name == "set_field":
            fname, sep, fval = value.partition("=")
            if not sep:
                raise ValueError(f"{where}: значение — «поле=значение»")
            out["value"] = f"{_field_name(fname, where)}={fval.strip()[:500]}"
        elif name == "notify_chat":
            from services import workflow_runtime as _rt
            try:
                chat, title = _rt.parse_notify_chat(value)
            except ValueError as e:
                raise ValueError(f"{where}: {e}")
            out["value"] = chat + (f" | {title}" if title else "")
        elif name == "add_to_field":
            fname, sep, num = value.partition("+=")
            delta = _to_number(num) if sep else None
            if delta is None or abs(delta) > 1_000_000:
                raise ValueError(f"{where}: значение — «поле+=число», например score+=10 "
                                 "(или score+=-5, чтобы отнять)")
            out["value"] = f"{_field_name(fname, where)}+={_fmt_number(delta)}"
        elif value:
            out["value"] = value[:200]

    return out


_HHMM_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def validate_steps(steps, already: int = 0) -> list[dict]:
    """Проверить список шагов целиком с учётом уже сохранённых (`already`)."""
    if not isinstance(steps, list):
        raise ValueError("Шаги должны быть списком")
    total = already + len(steps)
    if total > MAX_WORKFLOW_STEPS:
        raise ValueError(
            f"В сценарии не больше {MAX_WORKFLOW_STEPS} шагов "
            f"(уже {already}, добавляется {len(steps)})")
    return [validate_step(s, i) for i, s in enumerate(steps)]


async def latest_run(pool: asyncpg.Pool, owner_id: int, workflow_id: int) -> Optional[dict]:
    """Последний прогон сценария — или None, если сценарий ни разу не запускали.

    `GET /workflow/{wf_id}/status` звал `get_workflow_status(pool, uid, wf_id)`,
    а третий аргумент там — id ПРОГОНА (`workflow_runs.id`), не сценария.
    Совпадение id двух разных таблиц — обычное дело, так что эндпоинт отдавал
    чужой прогон как состояние этого сценария; когда совпадения не было,
    отдавалось тело `null` с кодом 200.
    """
    row = await pool.fetchrow(
        """SELECT wr.*, wd.name AS workflow_name
             FROM workflow_runs wr
             LEFT JOIN workflow_definitions wd ON wd.id = wr.workflow_id
            WHERE wr.workflow_id = $1 AND wr.owner_id = $2
            ORDER BY wr.id DESC LIMIT 1""",
        workflow_id, owner_id)
    return dict(row) if row else None


async def recent_runs(pool: asyncpg.Pool, owner_id: int, workflow_id: int,
                      limit: int = 5) -> list[dict]:
    """Последние прогоны для блока «📊 Последние запуски» на экране деталей.

    Блок в мини-аппе был написан по полю `runs`, которого обработчик деталей не
    отдавал вовсе: разметка существовала и не показывалась никогда.
    """
    rows = await pool.fetch(
        """SELECT id, status, current_step, total_steps, started_at, finished_at,
                  error_message, chat_id, source, goal_reached_at, steps_version
             FROM workflow_runs
            WHERE workflow_id = $1 AND owner_id = $2
            ORDER BY id DESC LIMIT $3""",
        workflow_id, owner_id, max(1, min(int(limit or 5), 20)))
    return [dict(r) for r in rows]


# Подписи исходов шага для журнала и статистики экрана.
STEP_OUTCOMES: dict[str, str] = {
    "sent": "отправлено", "send_failed": "не доставлено", "delay": "пауза",
    "yes": "условие да", "no": "условие нет", "answered": "ответил",
    "paid": "оплатил", "timeout": "не ответил в срок", "done": "выполнено",
    "error": "ошибка", "goal": "цель достигнута", "completed": "завершён",
    "failed": "сбой", "expired": "истёк срок", "stopped": "остановлен",
    "cancelled": "отменён", "retry": "повтор позже",
    "retried": "повторён владельцем", "waiting": "ждёт события", "event": "событие пришло", "night_hold": "ждёт утра", "cap_hold": "ждёт: предел сообщений за сутки",
}


async def run_log(pool: asyncpg.Pool, owner_id: int, run_id: int) -> Optional[dict]:
    """Журнал одного прогона: что сценарий сделал с человеком, по шагам.

    None — прогона нет у владельца. Чужой прогон не отличим от
    несуществующего."""
    run = await pool.fetchrow(
        """SELECT r.id, r.workflow_id, r.status, r.chat_id, r.bot_id, r.source,
                  r.started_at, r.finished_at, r.goal_reached_at, r.error_message,
                  u.username, u.first_name
             FROM workflow_runs r
             LEFT JOIN bot_users u ON u.bot_id = r.bot_id AND u.user_id = r.chat_id
            WHERE r.id=$1 AND r.owner_id=$2""", run_id, owner_id)
    if not run:
        return None
    rows = await pool.fetch(
        """SELECT step_idx, step_type, outcome, detail, amount, currency, created_at
             FROM workflow_step_log
            WHERE run_id=$1 AND owner_id=$2
            ORDER BY id LIMIT 300""", run_id, owner_id)
    out = dict(run)
    out["steps_log"] = [
        {"step_idx": r["step_idx"], "step_type": r["step_type"],
         "outcome": r["outcome"], "detail": r["detail"],
         "amount": r["amount"], "currency": r["currency"],
         "created_at": r["created_at"]}
        for r in rows]
    return out


def _conversion_key(goal_type: str | None) -> str:
    return "goal" if goal_type else "completed"


async def workflow_stats(pool: asyncpg.Pool, owner_id: int, workflow_id: int,
                         goal_type: str | None = None) -> dict:
    """Цифры сценария для экрана: прогоны, люди на каждом шаге, исходы,
    выручка, источники и A/B-ветки с лидером.

    Строится по журналу шагов (workflow_step_log): «на шаге» — число
    РАЗНЫХ прогонов, дошедших до него, а не число строк (переход назад
    исполняет шаг повторно).
    """
    from services import ab_engine
    runs = await pool.fetchrow(
        """SELECT count(*) AS total,
                  count(*) FILTER (WHERE status IN ('running','waiting')) AS active,
                  count(*) FILTER (WHERE status='completed') AS completed,
                  count(*) FILTER (WHERE goal_reached_at IS NOT NULL) AS goal,
                  count(*) FILTER (WHERE status IN ('failed','expired','stopped','cancelled')) AS dropped
             FROM workflow_runs WHERE workflow_id=$1 AND owner_id=$2""",
        workflow_id, owner_id)
    step_rows = await pool.fetch(
        """SELECT step_idx, outcome, count(DISTINCT run_id) AS n
             FROM workflow_step_log
            WHERE workflow_id=$1 AND owner_id=$2
            GROUP BY step_idx, outcome""", workflow_id, owner_id)
    steps: dict[int, dict] = {}
    for r in step_rows:
        st = steps.setdefault(int(r["step_idx"]), {"outcomes": {}})
        st["outcomes"][r["outcome"]] = int(r["n"])
    reached = await pool.fetch(
        """SELECT step_idx, count(DISTINCT run_id) AS n FROM workflow_step_log
            WHERE workflow_id=$1 AND owner_id=$2 GROUP BY step_idx""",
        workflow_id, owner_id)
    for r in reached:
        steps.setdefault(int(r["step_idx"]), {"outcomes": {}})["reached"] = int(r["n"])
    # «Отвалились на шаге»: прогон, закончившийся НЕ по сценарию (ошибка,
    # истёк, остановлен, отменён), считается на последнем шаге, который он
    # успел исполнить. Это шаг, после которого человек ушёл, то есть место,
    # которое надо чинить. Прогон, дошедший до конца, сюда не попадает.
    dropped_at = await pool.fetch(
        """SELECT last_idx AS step_idx, count(*) AS n FROM (
               SELECT DISTINCT ON (l.run_id) l.run_id, l.step_idx AS last_idx
                 FROM workflow_step_log l
                 JOIN workflow_runs r ON r.id = l.run_id
                WHERE l.workflow_id=$1 AND l.owner_id=$2
                  AND r.status IN ('failed','expired','stopped','cancelled')
                ORDER BY l.run_id, l.id DESC) t
            GROUP BY last_idx""", workflow_id, owner_id)
    for r in dropped_at:
        steps.setdefault(int(r["step_idx"]), {"outcomes": {}})["dropped"] = int(r["n"])
    revenue = [dict(r) for r in await pool.fetch(
        """SELECT currency, sum(amount) AS amount, count(*) AS payments
             FROM workflow_step_log
            WHERE workflow_id=$1 AND owner_id=$2 AND outcome='paid'
              AND amount IS NOT NULL
            GROUP BY currency ORDER BY 2 DESC""", workflow_id, owner_id)]
    # count(DISTINCT r.id), а не count(*): соединение с журналом оплат
    # размножает прогон на число его оплат — два счёта в одном сценарии
    # показывали одного человека как двоих и цель «достигнутой» дважды.
    sources = [dict(r) for r in await pool.fetch(
        """SELECT COALESCE(NULLIF(r.source, ''), '') AS source,
                  count(DISTINCT r.id) AS runs,
                  count(DISTINCT r.id) FILTER (WHERE r.goal_reached_at IS NOT NULL) AS goals,
                  count(DISTINCT l.run_id) AS paid
             FROM workflow_runs r
             LEFT JOIN workflow_step_log l ON l.run_id = r.id AND l.outcome='paid'
            WHERE r.workflow_id=$1 AND r.owner_id=$2
            GROUP BY 1 ORDER BY 2 DESC LIMIT 10""", workflow_id, owner_id)]
    # Воронка по шагам для каждого источника: сколько его людей дошло до
    # шага. Видно, какой трафик отваливается на втором сообщении, а какой
    # доходит до оплаты. Только верхние источники — экран, а не выгрузка.
    top = [s["source"] for s in sources[:FUNNEL_SOURCES]]
    funnel_rows = await pool.fetch(
        """SELECT COALESCE(NULLIF(r.source, ''), '') AS source, l.step_idx,
                  count(DISTINCT l.run_id) AS n
             FROM workflow_step_log l
             JOIN workflow_runs r ON r.id = l.run_id
            WHERE l.workflow_id=$1 AND l.owner_id=$2
              AND COALESCE(NULLIF(r.source, ''), '') = ANY($3::text[])
            GROUP BY 1, 2""", workflow_id, owner_id, top) if top else []
    by_src: dict[str, dict[str, int]] = {}
    for r in funnel_rows:
        by_src.setdefault(r["source"], {})[str(int(r["step_idx"]))] = int(r["n"])
    funnel = [{"source": s["source"], "runs": int(s["runs"]),
               "steps": dict(sorted(by_src.get(s["source"], {}).items(),
                                    key=lambda kv: int(kv[0])))}
              for s in sources[:FUNNEL_SOURCES]]
    # A/B: для каждого шага-деления — сколько прогонов ушло в ветку и
    # сколько из них дошло до цели (или до конца, если цели нет).
    conv = _conversion_key(goal_type)
    split_rows = await pool.fetch(
        f"""SELECT l.step_idx, l.outcome, count(DISTINCT l.run_id) AS sent,
                   count(DISTINCT l.run_id) FILTER (
                       WHERE {'r.goal_reached_at IS NOT NULL' if conv == 'goal'
                              else "r.status='completed'"}) AS converted
              FROM workflow_step_log l
              JOIN workflow_runs r ON r.id = l.run_id
             WHERE l.workflow_id=$1 AND l.owner_id=$2 AND l.step_type='split'
               AND l.outcome LIKE 'branch:%'
             GROUP BY l.step_idx, l.outcome""", workflow_id, owner_id)
    splits: dict[int, dict] = {}
    for r in split_rows:
        try:
            variant = int(str(r["outcome"]).split(":", 1)[1])
        except (IndexError, ValueError):
            continue
        sp = splits.setdefault(int(r["step_idx"]), {"branches": []})
        sp["branches"].append({"variant": variant, "sent": int(r["sent"]),
                               "converted": int(r["converted"])})
    for sp in splits.values():
        sp["branches"].sort(key=lambda b: b["variant"])
        sp["result"] = ab_engine.pick_winner(sp["branches"])
        sp["result"]["rates"] = {str(k): round(v, 4)
                                 for k, v in sp["result"].get("rates", {}).items()}
    return {
        "runs": dict(runs) if runs else {},
        "steps": {str(k): v for k, v in sorted(steps.items())},
        "revenue": revenue,
        "sources": sources,
        "funnel_by_source": funnel,
        "splits": {str(k): v for k, v in sorted(splits.items())},
        "conversion": conv,
        "outcome_labels": STEP_OUTCOMES,
    }
