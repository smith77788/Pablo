"""Bot Mesh — координация задач между ботами поверх Telegram bot-to-bot.

Telegram РЕАЛЬНО поддерживает bot-to-bot: бот включает режим в @BotFather, и
тогда получает апдейты от других ботов (в группах — по `/cmd@bot` или reply, в
ЛС — при обоюдном opt-in, в бизнес-аккаунтах — только у отправителя). Владелец
видит цепочку Sales→Qualification→CRM как один workflow.

Но одну вещь Telegram оставляет НА НАС и прямо предупреждает о ней:
«bot-message handling must terminate predictably» — гашение петель. Два бота,
отвечающие друг другу, дают бесконечный цикл; сеть ботов без этой защиты
складывается в лавину сообщений и флуд-бан всего флота. Поэтому сердце модуля —
не «отправить боту», а РЕШЕНИЕ обрабатывать шаг или отбросить.

Здесь только чистая логика этого решения и маршрутизации (её и проверяют тесты;
заглушка пула типы связывания не ловит — CLAUDE.md). Реальная отправка/приём
через хендлеры ботов — следующий инкремент и требует включённого режима в
@BotFather у каждого участника; ядро безопасности строим первым, потому что
ошибиться в нём — уронить флот, а построить его сейчас безопасно.

Четыре независимых предохранителя (лавину рвёт любой из них):
* **глубина**: не больше MAX_DEPTH передач на задачу;
* **дедлайн**: у задачи есть срок — просроченная не идёт дальше;
* **дедуп шага**: один и тот же (задача, шаг, получатель) не обрабатывается
  дважды — прямой признак петли;
* **цикл в трассе**: один бот не появляется в пути чаще MAX_VISITS раз.

СТАТУС: НЕ ПОДКЛЮЧЁН. Построено ядро координации и защиты от петель; транспорта
(приём/отправка bot-message) ещё нет. Причина: транспорт требует включённого
режима bot-to-bot в @BotFather у каждого бота-участника — это ручной шаг на
живых ботах, из кода его не сделать. Ядро строится первым осознанно: ошибка в
гашении петель роняет флот, поэтому его безопаснее иметь и покрыть тестами до
транспорта. Подключается вместе с хендлерами приёма bot-message (см. TASK_QUEUE,
блок Virtual Layer → Bot Mesh) — тогда убрать из KNOWN_ORPHANS.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

# Потолок передач на одну задачу. Реальные цепочки коротки (3–5 шагов);
# всё сверх — почти наверняка петля.
MAX_DEPTH = 6
# Сколько раз один бот может встретиться в пути задачи. >1 допускаем (бот-
# оркестратор может получить результат назад), но не бесконечно.
MAX_VISITS = 2
# Срок жизни задачи по умолчанию: незавершённая за это время — брошена.
DEFAULT_DEADLINE_SEC = 300

# Причины отбрасывания (в колонке drop_reason / hops.reason).
DROP_EXPIRED = "expired"
DROP_MAX_DEPTH = "max_depth"
DROP_DUPLICATE = "duplicate_step"
DROP_CYCLE = "cycle"
DROP_NO_ROUTE = "no_route"


def _now(now: datetime | None = None) -> datetime:
    return now or datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def make_envelope(owner_id: int, origin_bot: int, route: list[dict],
                  payload: dict | None = None, *,
                  deadline_sec: int = DEFAULT_DEADLINE_SEC,
                  now: datetime | None = None) -> dict:
    """Создать задачу-конверт для запуска по маршруту.

    route — упорядоченные шаги [{"bot": <id>, "capability": <str>}]. Первый шаг
    (step=0) — первый получатель. origin_bot уже в трассе: он инициатор.
    """
    now = _now(now)
    return {
        "task_id": uuid.uuid4().hex,
        "owner_id": int(owner_id),
        "origin_bot": int(origin_bot),
        "route": list(route or []),
        "step": 0,
        "depth": 0,
        "trace": [int(origin_bot)],
        "payload": dict(payload or {}),
        "deadline_at": now + timedelta(seconds=max(1, int(deadline_sec))),
        "status": "running",
    }


def target_of(env: dict) -> dict | None:
    """Текущий получатель шага, либо None если маршрут исчерпан."""
    route = env.get("route") or []
    step = int(env.get("step", 0))
    if 0 <= step < len(route):
        return route[step]
    return None


def should_process(env: dict, *, seen_steps: set | None = None,
                   now: datetime | None = None,
                   max_depth: int = MAX_DEPTH,
                   max_visits: int = MAX_VISITS) -> tuple[bool, str | None]:
    """Обрабатывать этот шаг задачи или отбросить. (ok, причина_отброса).

    seen_steps — множество уже обработанных (task_id, step, to_bot); в проде это
    роль UNIQUE-индекса hops, здесь — для чистой проверки. Любой сработавший
    предохранитель рвёт цепочку — в этом и смысл «предсказуемого завершения».
    """
    now = _now(now)
    deadline = _aware(env.get("deadline_at"))
    if deadline is not None and now > deadline:
        return False, DROP_EXPIRED
    if int(env.get("depth", 0)) > max_depth:
        return False, DROP_MAX_DEPTH

    tgt = target_of(env)
    if tgt is None:
        return False, DROP_NO_ROUTE
    to_bot = tgt.get("bot")

    trace = env.get("trace") or []
    if to_bot is not None and trace.count(to_bot) >= max_visits:
        return False, DROP_CYCLE

    if seen_steps is not None:
        key = (env.get("task_id"), int(env.get("step", 0)), to_bot)
        if key in seen_steps:
            return False, DROP_DUPLICATE

    return True, None


def advance(env: dict, *, now: datetime | None = None) -> dict:
    """Продвинуть задачу на следующий шаг маршрута (после успешной обработки).

    Возвращает НОВЫЙ конверт. Не проверяет предохранители — это делает
    should_process на приёме следующим ботом. Терминальность (маршрут исчерпан)
    видна по target_of(next) is None и отражается в статусе.
    """
    tgt = target_of(env)
    nxt = dict(env)
    nxt["step"] = int(env.get("step", 0)) + 1
    nxt["depth"] = int(env.get("depth", 0)) + 1
    trace = list(env.get("trace") or [])
    if tgt and tgt.get("bot") is not None:
        trace.append(int(tgt["bot"]))
    nxt["trace"] = trace
    if target_of(nxt) is None:
        nxt["status"] = "done"
    return nxt


def is_terminal(env: dict) -> bool:
    return target_of(env) is None


# ── Персистенция (тонкая; вся логика — выше) ───────────────────────────────

async def create_task(pool, env: dict) -> str:
    import json
    await pool.execute(
        """INSERT INTO bot_mesh_tasks
             (task_id, owner_id, origin_bot, route, step, depth, trace, payload,
              status, deadline_at)
           VALUES ($1,$2,$3,$4::jsonb,$5,$6,$7::jsonb,$8::jsonb,$9,$10)""",
        env["task_id"], env["owner_id"], env.get("origin_bot"),
        json.dumps(env.get("route") or [], ensure_ascii=False),
        int(env.get("step", 0)), int(env.get("depth", 0)),
        json.dumps(env.get("trace") or []),
        json.dumps(env.get("payload") or {}, ensure_ascii=False, default=str),
        env.get("status", "running"), env["deadline_at"])
    return env["task_id"]


async def record_hop(pool, task_id: str, step: int, *, from_bot=None,
                     to_bot=None, capability=None, outcome: str,
                     reason: str | None = None) -> bool:
    """Записать передачу. Возвращает False, если шаг уже был (UNIQUE-индекс) —
    это и есть межпроцессный дедуп петли: повтор просто не вставится."""
    try:
        await pool.execute(
            """INSERT INTO bot_mesh_hops
                 (task_id, step, from_bot, to_bot, capability, outcome, reason)
               VALUES ($1,$2,$3,$4,$5,$6,$7)""",
            task_id, int(step), from_bot, to_bot, capability, outcome, reason)
        return True
    except Exception:
        # UniqueViolation (шаг уже обработан) или иная ошибка — шаг не берём.
        return False


async def drop_task(pool, task_id: str, reason: str) -> None:
    await pool.execute(
        "UPDATE bot_mesh_tasks SET status='dropped', drop_reason=$2, updated_at=now() "
        "WHERE task_id=$1 AND status='running'", task_id, reason)


async def save_progress(pool, env: dict) -> None:
    import json
    await pool.execute(
        "UPDATE bot_mesh_tasks SET step=$2, depth=$3, trace=$4::jsonb, "
        "status=$5, updated_at=now() WHERE task_id=$1",
        env["task_id"], int(env.get("step", 0)), int(env.get("depth", 0)),
        json.dumps(env.get("trace") or []), env.get("status", "running"))
