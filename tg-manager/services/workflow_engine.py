"""Workflow Engine — automated multi-step operation orchestration.

Manages workflow definitions, execution, and status tracking.
"""
from __future__ import annotations

import json
import logging
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
        "steps": [
            {"type": "condition", "condition": "event", "condition_value": "new_subscriber"},
            {"type": "message", "text": "Вижу, вы только присоединились — держите короткий гид."},
        ],
    },
    "condition": {
        "label": "Условная логика",
        "steps": [
            {"type": "condition", "condition": "has_tag", "condition_value": "клиент"},
            {"type": "message", "text": "Для клиентов: отдельные условия и поддержка."},
            {"type": "delay", "delay_minutes": 720},
            {"type": "message", "text": "Напоминание: предложение ещё в силе."},
        ],
    },
}

# Типы шагов, которые экран умеет показать. Шаг неизвестного типа в шаблоне
# означал бы строку «⚫ undefined» в деталях сценария.
WORKFLOW_STEP_TYPES = frozenset({"message", "delay", "condition", "action", "webhook"})


def workflow_template(name: str) -> dict | None:
    """Описание шаблона: {key, label, steps[]}. Чистая функция."""
    tpl = WORKFLOW_TEMPLATES.get((name or "").strip().lower())
    if not tpl:
        return None
    key = (name or "").strip().lower()
    # Копия: вызывающий складывает шаги в jsonb и может их править.
    return {"key": key, "label": tpl["label"],
            "steps": [dict(step) for step in tpl["steps"]]}


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
) -> dict:
    """Execute a workflow run."""
    try:
        wf = await pool.fetchrow(
            'SELECT * FROM workflow_definitions WHERE id = $1 AND owner_id = $2',
            workflow_id, owner_id)
        if not wf:
            return {"ok": False, "error": "Workflow not found"}

        # steps — jsonb; без кодека asyncpg отдаёт СТРОКУ, и len() считал бы
        # символы: воркфлоу из двух шагов получал total_steps под сотню, а
        # прогресс на экране — бессмыслицу.
        _steps = wf.get("steps") or []
        if isinstance(_steps, str):
            try:
                _steps = json.loads(_steps)
            except Exception:
                _steps = []
        total_steps = len(_steps) if isinstance(_steps, list) else 0
        row = await pool.fetchrow(
            '''INSERT INTO workflow_runs
               (owner_id, workflow_id, status, input_data, current_step, total_steps, started_at)
               VALUES ($1, $2, 'running', $3::jsonb, 0, $4, NOW())
               RETURNING id''',
            owner_id, workflow_id, json.dumps(input_data or {}), total_steps)
        return {"ok": True, "run_id": row["id"], "total_steps": total_steps}
    except Exception as e:
        log.warning("execute_workflow error: %s", e)
        return {"ok": False, "error": str(e)}


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
    """Cancel a running workflow."""
    try:
        result = await pool.execute(
            '''UPDATE workflow_runs
               SET status = 'cancelled', finished_at = NOW()
               WHERE id = $1 AND owner_id = $2
                 AND status IN ('pending', 'running')''',
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


def validate_step(step, index: int = 0) -> dict:
    """Проверить и нормализовать один шаг сценария.

    Возвращает НОВЫЙ словарь с полями, которые экран умеет показать. Непонятный
    шаг — ValueError с русским текстом для владельца (обработчики отдают его
    как 400), а не тихая запись в jsonb.

    Валидация живёт рядом со словарём типов, а не внутри `create_workflow`:
    `create_workflow` — тонкая запись в БД, её зовут и миграции, и тесты
    движка. Вызывать проверку обязаны все пути записи из мини-аппа, и за это
    есть храповик в tests/test_workflow_steps_speak_one_language.py.
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

    if stype == "message":
        text = str(step.get("text") or "").strip()
        if not text:
            raise ValueError(f"{where}: сообщение без текста")
        if len(text) > MAX_STEP_TEXT:
            raise ValueError(
                f"{where}: текст длиннее {MAX_STEP_TEXT} символов — "
                "Telegram такое сообщение не примет")
        out["text"] = text

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
        cond = str(step.get("condition") or "").strip()
        if not cond:
            raise ValueError(f"{where}: условие не выбрано")
        out["condition"] = cond[:120]
        value = str(step.get("condition_value") or "").strip()
        if value:
            out["condition_value"] = value[:200]

    elif stype == "webhook":
        url = str(step.get("url") or "").strip()
        if not url.startswith("https://"):
            raise ValueError(
                f"{where}: вебхук только по https — по http уйдёт открытым текстом")
        if len(url) > 500:
            raise ValueError(f"{where}: адрес вебхука длиннее 500 символов")
        out["url"] = url

    else:  # action
        name = str(step.get("action") or step.get("text") or "").strip()
        if not name:
            raise ValueError(f"{where}: действие не указано")
        out["action"] = name[:120]

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
