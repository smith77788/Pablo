"""Workflow Engine — automated multi-step operation orchestration.

Manages workflow definitions, execution, and status tracking.
"""
from __future__ import annotations

import json
import logging
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
                                 "ask", "invoice"})

# Действия шага `action` — ровно те, что исполнитель (services/workflow_runtime.py)
# умеет выполнить. Раньше имя действия было произвольным текстом: шаг
# «⚡ warmup» сохранялся, показывался и не означал ничего, потому что
# исполнять его было некому. Теперь шаг исполняется, и действие, которого
# исполнитель не знает, — отказ при сохранении, а не тихий пропуск в прогоне.
WORKFLOW_ACTIONS: dict[str, str] = {
    "add_tag": "Поставить тег",
    "remove_tag": "Снять тег",
    "set_field": "Записать поле (поле=значение)",
    "create_deal": "Создать сделку в CRM",
    "notify_owner": "Уведомить меня",
    "goto": "Перейти к шагу",
    "stop": "Завершить сценарий",
}
# Действия, которым значение обязательно.
_ACTIONS_NEED_VALUE = frozenset({"add_tag", "remove_tag", "set_field", "goto"})

# Условия шага `condition` — те, что исполнитель умеет проверить.
WORKFLOW_CONDITIONS: dict[str, str] = {
    "tag_has": "Есть тег",
    "has_tag": "Есть тег",
    "tag_not": "Нет тега",
    "field_equals": "Поле равно (поле=значение)",
    "field_set": "Поле заполнено",
    "clicked": "Нажал кнопку (текст кнопки)",
    "inactive_days": "Дней неактивен (≥)",
    "subscribed": "Подписан на канал (@канал)",
    "paid": "Оплатил",
    "event": "Сценарий запущен событием",
}

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
            row = await conn.fetchrow(
                "SELECT steps FROM workflow_definitions "
                "WHERE id=$1 AND owner_id=$2 FOR UPDATE", workflow_id, owner_id)
            if row is None:
                return None
            steps = row["steps"]
            if isinstance(steps, str):
                steps = json.loads(steps or "[]")
            steps = steps if isinstance(steps, list) else []
            if not 0 <= index < len(steps):
                return None
            del steps[index]
            kept = []
            for i, st in enumerate(steps):
                try:
                    kept.append(validate_steps([st])[0])
                except ValueError:
                    log.info("delete_step: шаг %s сценария %s со старым языком "
                             "отброшен", i + 1, workflow_id)
            await conn.execute(
                "UPDATE workflow_definitions SET steps=$1::jsonb, updated_at=NOW() "
                "WHERE id=$2 AND owner_id=$3", json.dumps(kept), workflow_id, owner_id)
    return len(kept)


async def set_trigger(pool: asyncpg.Pool, owner_id: int, workflow_id: int,
                      trigger_type: str | None, trigger_value: str | None,
                      allow_reentry: bool | None = None) -> bool:
    """Записать способ запуска. False — сценарий не найден у владельца."""
    res = await pool.execute(
        """UPDATE workflow_definitions
              SET trigger_type=$3, trigger_value=$4,
                  allow_reentry=COALESCE($5, allow_reentry), updated_at=NOW()
            WHERE id=$1 AND owner_id=$2""",
        workflow_id, owner_id, trigger_type, trigger_value, allow_reentry)
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
        out["text"] = _text()
        buttons = _buttons(step.get("buttons"), where)
        if buttons:
            out["buttons"] = buttons

    elif stype == "delay":
        try:
            minutes = int(step.get("delay_minutes"))
        except (TypeError, ValueError):
            raise ValueError(f"{where}: задержка в минутах не указана")
        if not 1 <= minutes <= MAX_STEP_DELAY_MIN:
            raise ValueError(
                f"{where}: задержка от 1 до {MAX_STEP_DELAY_MIN} минут "
                f"({MAX_STEP_DELAY_MIN // 1440} суток)")
        out["delay_minutes"] = minutes

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
        if name in _ACTIONS_NEED_VALUE and not value:
            raise ValueError(f"{where}: для действия «{WORKFLOW_ACTIONS[name]}» "
                             "нужно значение")
        if name == "goto":
            out["goto"] = _goto({"goto": value}, "goto", where)
        elif name == "set_field":
            fname, sep, fval = value.partition("=")
            if not sep:
                raise ValueError(f"{where}: значение — «поле=значение»")
            out["value"] = f"{_field_name(fname, where)}={fval.strip()[:500]}"
        elif value:
            out["value"] = value[:200]

    return out


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
                  error_message
             FROM workflow_runs
            WHERE workflow_id = $1 AND owner_id = $2
            ORDER BY id DESC LIMIT $3""",
        workflow_id, owner_id, max(1, min(int(limit or 5), 20)))
    return [dict(r) for r in rows]
