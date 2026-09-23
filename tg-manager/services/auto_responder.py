"""Background auto-reply polling service."""

from __future__ import annotations
import asyncio
import logging
import time
from datetime import datetime

import aiohttp
import asyncpg
from database import db
from services import bot_api
from services import brand_injection
from services import routing_engine
from services.logger import log_exc_swallow
from bot.utils.template_validator import replace_placeholders

log = logging.getLogger(__name__)

# Rate limiter: prevent one user from triggering too many automation rules at once.
# Maps (bot_id, chat_id) → count of rules fired in the current polling cycle.
# Reset per-cycle in _process_bot.
_MAX_RULES_PER_USER_PER_CYCLE = 5
# Tracks rule execution counts within a single _process_bot call.
# Structure: {(bot_id, chat_id): int}
_cycle_rule_counts: dict[tuple[int, int], int] = {}

# Strong reference to фоновый inactivity-sweep. Event loop держит на задачу лишь
# СЛАБУЮ ссылку — несохранённый create_task может быть собран GC до завершения, и
# фоновый цикл авто-неактивности молча умер бы. Держим ссылку на уровне модуля.
_inactivity_sweep_task: asyncio.Task | None = None
# Тот же приём для фонового дожима (followup) менеджера по продажам.
_sales_followup_task: asyncio.Task | None = None
# И для самовосстановления облака (избыточность кусков после банов хранителей).
_cloud_reconcile_task: asyncio.Task | None = None

# Cooldown for bots whose token getUpdates rejects as Unauthorized (revoked/invalid
# token). Without this, a single dead-token bot gets re-polled every 10s forever,
# spamming warnings and hammering Telegram's API with calls that can only ever fail.
# Structure: {bot_id: (consecutive_fail_count, retry_after_monotonic)}
_dead_token_cooldown: dict[int, tuple[int, float]] = {}
_DEAD_TOKEN_BASE_COOLDOWN = 60.0  # seconds
_DEAD_TOKEN_MAX_COOLDOWN = 3600.0  # cap at 1h between retries

# Троттлинг уведомлений «новый подписчик» ПО БОТУ (анти-накрутка). Раньше каждый
# новый юзер слал владельцу отдельный DM через main-bot; при накрутке ботами
# поток из тысяч фейков насыщал main-bot (FloodWait) — и Infragram переставал
# отвечать всем. Теперь копим счётчик за окно и шлём одно агрегированное
# уведомление на бота, предупреждая о возможной накрутке.
# Структура: {bot_id: {"count": int, "last": monotonic}}
_new_user_notify: dict[int, dict] = {}
_NEW_USER_NOTIFY_COOLDOWN = 60.0  # не чаще одного уведомления на бота в минуту


def _new_user_notify_decide(bot_id: int) -> int | None:
    """Сколько новых подписчиков отправить в уведомлении сейчас, либо None если
    рано (throttle). ЧИСТАЯ по эффекту (только in-memory счётчик процесса).

    Первый подписчик после паузы уведомляется сразу (count=1); последующие в
    пределах окна копятся и уходят одним агрегированным уведомлением после окна."""
    now = time.monotonic()
    st = _new_user_notify.get(bot_id)
    if st is None:
        st = {"count": 0, "last": 0.0}
        _new_user_notify[bot_id] = st
    st["count"] += 1
    if now - st["last"] >= _NEW_USER_NOTIFY_COOLDOWN:
        n = st["count"]
        st["count"] = 0
        st["last"] = now
        return n
    return None


def _is_dm_update(upd: dict) -> bool:
    """True только если сообщение пришло из ЛИЧНОГО чата с ботом.

    Весь пользовательский путь автоответчика держит chat_id как id пользователя:
    учёт нового юзера, подписка на воронку, add_bot_user, авто-ответы, /start,
    релей «входящих» оператору. В личке chat.id == user.id — верно. В ГРУППЕ
    chat.id — это id группы, поэтому дочерний бот, попавший в чат (обычное дело
    при инвайте Мать-Дочка), отвечал бы прямо в группу (спам, выгоняющий только
    что приглашённых) и засорял бы базу подписчиков id-ами групп. Группового
    поведения у этого пути нет вовсе — только лички.
    """
    msg = upd.get("message")
    if not isinstance(msg, dict):
        return False
    # Отсутствие типа (Telegram его всегда шлёт) трактуем как личку, чтобы не
    # отломить штатный путь при неожиданной форме апдейта.
    ctype = (msg.get("chat") or {}).get("type") or "private"
    return ctype == "private"


def _render_text(text: str, from_user: dict, bot_row: dict | None = None) -> str:
    """Render {{PLACEHOLDER}} tokens in text with user/bot context."""
    if not text or "{{" not in text:
        return text
    username = from_user.get("username", "") or ""
    first_name = from_user.get("first_name", "") or ""
    last_name = from_user.get("last_name", "") or ""
    bot_name = (
        (bot_row.get("username") or bot_row.get("first_name") or "") if bot_row else ""
    )
    now = datetime.now()
    return replace_placeholders(
        text,
        {
            "USERNAME": f"@{username}" if username else first_name,
            "FIRST_NAME": first_name,
            "LAST_NAME": last_name,
            "FULL_NAME": f"{first_name} {last_name}".strip(),
            "BOT_NAME": bot_name,
            "DATE": now.strftime("%d.%m.%Y"),
            "DATE_SHORT": now.strftime("%d.%m"),
            "TIME": now.strftime("%H:%M"),
        },
    )


def _rule_buttons(rule: dict) -> list | None:
    """Инлайн-кнопки авто-ответа из JSONB-колонки buttons: [{text,url}] или None."""
    raw = rule.get("buttons")
    if not raw:
        return None
    if isinstance(raw, str):
        try:
            import json as _json
            raw = _json.loads(raw)
        except Exception as e:
            log.warning('auto_responder: trigger_words parse failed: %s', e)
            return None
    if isinstance(raw, list) and raw:
        out = [b for b in raw if isinstance(b, dict) and b.get("text") and b.get("url")]
        return out or None
    return None


def _match_rule(rule: dict, text: str) -> bool:
    if not text:
        return False
    t = rule["trigger_type"]
    if t == "start":
        return text.strip().lower().startswith("/start")
    if t == "keyword":
        kw = (rule.get("keyword") or "").lower().strip()
        if not kw:
            return False
        low = text.lower().strip()
        mode = (rule.get("match_mode") or "contains").lower()
        parts = [p.strip() for p in kw.split(",") if p.strip()] or [kw]
        if mode == "exact":
            return any(low == p for p in parts)
        if mode == "starts":
            return any(low.startswith(p) for p in parts)
        # contains (по умолчанию); поддержка нескольких ключей через запятую
        return any(p in low for p in parts)
    if t == "any":
        return True
    return False


# Слова, которыми человек просит прекратить цепочку. Сознательно узкий и
# ТОЧНЫЙ список: срабатывание по вхождению («стоп» внутри «стоп-кран»,
# «остановка») отписывало бы людей, которые об этом не просили. Поэтому —
# сравнение целой (очищенной) строки, а не поиск подстроки.
_STOP_WORDS = frozenset({
    "стоп", "стоп!", "/stop", "stop", "отписаться", "отписка", "отписаться!",
    "unsubscribe", "не писать", "отпишите", "отпишись",
})


def _is_stop_word(text: str) -> bool:
    """Просьба прекратить рассылку — команда или короткая фраза целиком."""
    if not text:
        return False
    s = text.strip().lower().rstrip(".!… ")
    if not s or len(s) > 32:   # длинное сообщение — это разговор, а не команда
        return False
    return s in _STOP_WORDS


def _within_active_window(hour: int, from_hour, to_hour) -> bool:
    """True, если час `hour` (0-23, UTC) попадает в рабочее окно правила.

    NULL с любой стороны → окно не задано → всегда активно. Поддерживает окна
    через полночь (from > to, напр. 22→6 = 22:00..05:59). Чистая функция.
    """
    if from_hour is None or to_hour is None:
        return True
    try:
        f, t2 = int(from_hour) % 24, int(to_hour) % 24
    except (TypeError, ValueError):
        return True
    if f == t2:
        return True  # вырожденное окно трактуем как «всегда»
    if f < t2:
        return f <= hour < t2
    # окно через полночь
    return hour >= f or hour < t2


async def _init_offset(
    pool: asyncpg.Pool, http: aiohttp.ClientSession, bot_id: int, token: str
) -> int:
    """On first run: skip all pending updates, store current max_id as start point."""
    data = await bot_api._call(http, token, "getUpdates", offset=-1, limit=1, timeout=0)
    updates = data.get("result", []) if data.get("ok") else []
    if updates:
        max_id = updates[-1]["update_id"]
    else:
        max_id = 1  # sentinel: no pending updates, mark as initialized
    await db.set_update_offset(pool, bot_id, max_id)
    return max_id


async def _record_bot_error(pool: asyncpg.Pool, bot_id: int,
                            description: str, error_code=None) -> None:
    """Запомнить, ПОЧЕМУ бот не отвечает.

    Раньше это знал только серверный лог: на экране бот оставался «активным»,
    подписчики ему писали, ответа не было, и владельцу никто не сообщал.
    Запись состояния — вспомогательная: её сбой не должен ломать опрос.
    """
    try:
        from services.bot_health import classify_error

        kind = classify_error(description, error_code)
        await pool.execute(
            """UPDATE managed_bots
                  SET last_error=$2, last_error_at=now(),
                      fail_streak = fail_streak + 1
                WHERE bot_id=$1""",
            bot_id, kind)
    except Exception:
        log.debug("auto_responder: состояние бота %s не записано", bot_id)


async def _record_bot_ok(pool: asyncpg.Pool, bot_id: int) -> None:
    """Успешный опрос снимает жалобу и метку «уже сообщили».

    Метку снимаем тоже: если бота починили, а он сломается снова — об этом
    нужно предупредить заново, а не промолчать из-за старой отметки.
    """
    try:
        await pool.execute(
            """UPDATE managed_bots
                  SET fail_streak=0, last_error=NULL, last_ok_at=now(),
                      dead_notified_at=NULL
                WHERE bot_id=$1 AND (fail_streak > 0 OR last_ok_at IS NULL)""",
            bot_id)
    except Exception:
        log.debug("auto_responder: отметка успеха бота %s не записана", bot_id)


async def notify_broken_bots(pool: asyncpg.Pool, bot) -> int:
    """Сообщить владельцам о ботах, которые молчат и сами не починятся.

    Один раз на поломку (метка `dead_notified_at`) — тот же приём, что у
    сторожа прокси и у прогрева: повторяющееся уведомление перестают читать.
    Возвращает число отправленных.
    """
    from services.bot_health import build_alert, decide_alert

    try:
        rows = await pool.fetch(
            """SELECT bot_id, added_by, username, first_name,
                      last_error, fail_streak
                 FROM managed_bots
                WHERE is_active AND fail_streak > 0
                  AND dead_notified_at IS NULL
                LIMIT 50""")
    except Exception as e:
        log.debug("auto_responder: выборка сломанных ботов не удалась: %s", e)
        return 0

    sent = 0
    for r in rows:
        if not decide_alert(r["last_error"] or "", r["fail_streak"], False):
            continue
        name = (f"@{r['username']}" if r["username"]
                else (r["first_name"] or f"бот #{r['bot_id']}"))
        try:
            await db.notify_if_enabled(
                pool, bot, r["added_by"], "restriction",
                build_alert(name, r["last_error"] or "", r["fail_streak"]),
                dedup_key=f"bot_broken:{r['bot_id']}")
            sent += 1
        except Exception:
            log.debug("auto_responder: уведомление о боте %s не ушло", r["bot_id"])
        try:
            await pool.execute(
                "UPDATE managed_bots SET dead_notified_at=now() WHERE bot_id=$1",
                r["bot_id"])
        except Exception:
            log.debug("auto_responder: метка уведомления бота %s не записана", r["bot_id"])
    return sent


async def _notify_operator(http, token, operator: dict, chat_id: int,
                           from_user: dict, text: str) -> None:
    """Уведомить живого оператора о переводе диалога (через того же бота).

    Бот может написать только в чат, который с ним взаимодействовал, поэтому
    доставка работает по operator_chat_id. Если задан только @username —
    доставить нечем (бот не может инициировать чат по username); клиент при этом
    уже получил сообщение «подключаю специалиста», а перевод помечен в диалоге и
    в auto_reply_log, так что оператор/владелец увидит его в аналитике.
    """
    op_chat = (operator or {}).get("chat_id")
    if not op_chat:
        return
    uname = from_user.get("username")
    who = f"@{uname}" if uname else (from_user.get("first_name") or f"id{chat_id}")
    note = (f"🆘 Клиент {who} (chat_id={chat_id}) просит живого оператора.\n"
            f"Последнее сообщение: {text[:300]}")
    try:
        await bot_api.send_message(http, token, int(op_chat), note)
    except Exception:
        log_exc_swallow(log, "auto_responder: notify operator failed")


async def _notify_operator_order(http, token, operator: dict, chat_id: int,
                                 from_user: dict, summary: str) -> None:
    """Уведомить оператора о новом подтверждённом заказе (диалог остаётся у бота).

    Раньше заказ «повисал»: оператор не знал о нём. Доставка — по operator_chat_id
    (бот не может писать по @username без взаимодействия)."""
    op_chat = (operator or {}).get("chat_id")
    if not op_chat:
        return
    uname = from_user.get("username")
    who = f"@{uname}" if uname else (from_user.get("first_name") or f"id{chat_id}")
    note = (f"🛒 Новый заказ от {who} (chat_id={chat_id}).\n{summary or ''}").strip()
    try:
        await bot_api.send_message(http, token, int(op_chat), note)
    except Exception:
        log_exc_swallow(log, "auto_responder: notify operator (order) failed")


async def _deliver_sales_reply(http, token, chat_id, text, reply_markup=None) -> bool:
    """Ответ менеджера «по-человечески»: печатает… → пауза под длину → короткие
    сообщения (1–2). Кнопки-каналы — на последнем. Возвращает True, если ушло."""
    from services import bot_sales_persona as _bsp
    chunks = _bsp.split_reply(text, max_chunks=2)
    if not chunks:
        return False
    ok_any = False
    for i, chunk in enumerate(chunks):
        try:
            await bot_api._call(http, token, "sendChatAction",
                                chat_id=chat_id, action="typing")
        except Exception:
            pass
        try:
            await asyncio.sleep(_bsp.typing_delay(chunk))
        except Exception:
            pass
        rkb = reply_markup if i == len(chunks) - 1 else None
        try:
            ok, _ = await bot_api.send_message(http, token, chat_id, chunk, reply_markup=rkb)
            ok_any = ok_any or ok
        except Exception:
            log_exc_swallow(log, f"auto_responder: sales deliver bot chat={chat_id}")
    return ok_any


async def _maybe_handle_mesh(pool, http, token, bot_id: int, msg: dict) -> bool:
    """Приём сообщения Bot Mesh от другого бота. Возвращает True, если это была
    задача сети (обработана), иначе False (пусть идёт обычным путём).

    Всё, что связано с решением обрабатывать/отбросить, — loop-safe ядро
    bot_mesh. Дедуп петли — через UNIQUE-индекс hops (record_hop=False на
    повторе). Fail-open: ошибка не рушит хот-луп опроса.

    ФИЗИЧЕСКАЯ пересылка следующему боту (bot→bot send) на живых ботах не
    выверена и требует включённого режима у обоих — она изолирована и помечена;
    состояние задачи и защита от петель работают независимо от неё.
    """
    from services import bot_mesh
    text = msg.get("text") or ""
    env = bot_mesh.decode_message(text)
    if env is None:
        return False
    task_id = env.get("task_id")
    step = int(env.get("step", 0))
    from_bot = (msg.get("from") or {}).get("id")
    # Дедуп шага межпроцессно: повтор (task_id, step, этот бот) не вставится.
    fresh = await bot_mesh.record_hop(
        pool, task_id, step, from_bot=from_bot, to_bot=bot_id,
        capability=None, outcome="received")
    if not fresh:
        log.debug("bot_mesh: дубль шага task=%s step=%s — петля погашена", task_id, step)
        return True
    decision = bot_mesh.process_incoming(env)
    action = decision.get("action")
    if action == "drop":
        try:
            await bot_mesh.drop_task(pool, task_id, decision.get("reason") or "dropped")
        except Exception:
            log.debug("bot_mesh: drop persist failed task=%s", task_id)
        return True
    # forward | terminal — сохраняем прогресс и эмитим событие в шину.
    nxt = decision.get("next_env") or env
    try:
        await bot_mesh.save_progress(pool, nxt)
    except Exception:
        log.debug("bot_mesh: progress persist failed task=%s", task_id)
    try:
        from services.organism import spine
        await spine.emit(pool, env.get("owner_id") or 0,
                         "bot_mesh_" + ("done" if action == "terminal" else "hop"),
                         {"task_id": task_id, "to_bot": decision.get("to_bot"),
                          "step": nxt.get("step")})
    except Exception:
        pass
    if action == "forward" and decision.get("to_bot"):
        # СИВ физической пересылки (bot→bot). На живых ботах не выверен: требует
        # включённого bot-to-bot у обоих. Пытаемся, но не полагаемся — состояние
        # уже сохранено, дедуп/петли отработали.
        try:
            await bot_api.send_message(http, token, int(decision["to_bot"]),
                                       bot_mesh.encode_message(nxt))
        except Exception:
            log.debug("bot_mesh: forward send unverified/failed task=%s", task_id)
    return True


async def _process_bot(
    pool: asyncpg.Pool,
    http: aiohttp.ClientSession,
    bot_id: int,
    token: str,
    main_bot=None,
) -> None:
    # ── ЯДОВИТЫЙ АПДЕЙТ ──────────────────────────────────────────────────────
    # Разбор одного апдейта — длинный путь: авто-ответы, правила автоматизации,
    # воронки, эксперименты, релей оператору. Любое исключение на этом пути
    # раньше улетало в общий `except` внизу, а вместе с ним терялся и сдвиг
    # оффсета — он стоял ПОСЛЕ цикла. Telegram отдаёт апдейты от сохранённого
    # оффсета, значит следующий цикл (через 10 секунд) приносил ТОТ ЖЕ пакет,
    # бот снова падал на том же сообщении и снова ничего не сдвигал.
    #
    # Бот застревал так на сутки — пока Telegram сам не выкинет апдейт из
    # очереди, — и всё это время каждые 10 секунд заново слал авто-ответы,
    # заново запускал воронки и заново пересылал оператору уже обработанные
    # сообщения. Одно кривое сообщение превращало бота в спамер.
    #
    # Поэтому оффсет сдвигается В ЛЮБОМ случае, в finally. `max_update_id`
    # растёт в начале каждой итерации, то есть на момент падения он равен
    # ровно тому апдейту, на котором мы споткнулись: отравленный пропускается,
    # остальной хвост пакета вернётся следующим циклом.
    offset = 0
    max_update_id = 0
    try:
        offset = await db.get_update_offset(pool, bot_id)
        if offset == 0:
            await _init_offset(pool, http, bot_id, token)
            return
        data = await bot_api._call(
            http, token, "getUpdates", offset=offset + 1, limit=100, timeout=0
        )
        if not data.get("ok"):
            # Раньше ошибка getUpdates молча проглатывалась (updates=[]), из-за
            # чего бот переставал видеть сообщения — и новые пользователи не
            # детектировались — без единой записи в лог. Частая причина: на боте
            # активен webhook (409 Conflict) или отозван токен.
            err_desc = str(data.get("description") or data.get("error_code") or "unknown")
            if "conflict" in err_desc.lower() or "webhook" in err_desc.lower():
                # Webhook перехватывает апдейты → polling не нужен; снимаем webhook,
                # чтобы вернуть бота на polling (managed-боты работают через polling).
                try:
                    # drop_pending_updates по умолчанию false — ожидающие апдейты
                    # сохранятся и будут обработаны через polling.
                    await bot_api._call(http, token, "deleteWebhook")
                    log.warning(
                        "auto_responder: bot=%d getUpdates конфликт с webhook — webhook снят, polling восстановлен",
                        bot_id,
                    )
                except Exception:
                    log.warning("auto_responder: bot=%d webhook-конфликт, deleteWebhook не удался", bot_id)
            elif "unauthorized" in err_desc.lower() or data.get("error_code") == 401:
                # Токен отозван/невалиден — getUpdates будет проваливаться на
                # каждом цикле бесконечно. Ставим cooldown с экспоненциальным
                # отступом вместо ретрая каждые 10с (было: бесконечный спам
                # варнингов + бесполезные вызовы Telegram API на каждый цикл).
                fails, _ = _dead_token_cooldown.get(bot_id, (0, 0.0))
                fails += 1
                cooldown = min(
                    _DEAD_TOKEN_BASE_COOLDOWN * (2 ** (fails - 1)),
                    _DEAD_TOKEN_MAX_COOLDOWN,
                )
                _dead_token_cooldown[bot_id] = (fails, time.monotonic() + cooldown)
                if fails == 1 or fails % 10 == 0:
                    log.warning(
                        "auto_responder: bot=%d токен невалиден (Unauthorized), "
                        "попытка #%d, следующая через %.0fс",
                        bot_id, fails, cooldown,
                    )
            else:
                log.warning("auto_responder: bot=%d getUpdates вернул ошибку: %s", bot_id, err_desc[:200])
            # Состояние бота — В БАЗУ, а не только в лог. Раньше молчащий бот
            # выглядел на экране «активным»: подписчики ему писали, он не
            # отвечал, и владельцу об этом никто не говорил.
            await _record_bot_error(pool, bot_id, err_desc, data.get("error_code"))
            return
        await _record_bot_ok(pool, bot_id)
        _dead_token_cooldown.pop(bot_id, None)
        updates = data.get("result", [])
        if not updates:
            return

        # Fetch per-bot data ONCE, outside the per-message loop
        rules = await db.get_active_auto_replies(pool, bot_id)
        funnels = await db.get_active_funnels(pool, bot_id)
        automation_rules = await db.get_active_automation_rules(pool, bot_id)
        bot_row = await pool.fetchrow(
            "SELECT bot_role, swarm_enabled, cluster, added_by, username, first_name, relay_enabled "
            "FROM managed_bots WHERE bot_id=$1",
            bot_id,
        )
        active_exp = await db.get_active_experiment(pool, bot_id, "start_message")

        # Brand injection: cache free-tier status once per polling cycle
        try:
            _is_free = await brand_injection.is_free_tier(pool, bot_id)
        except Exception as e:
            log.warning('auto_responder: is_free_tier check failed: %s', e)
            _is_free = False

        # Reset per-cycle rate-limit counters for this bot
        keys_to_clear = [k for k in _cycle_rule_counts if k[0] == bot_id]
        for k in keys_to_clear:
            del _cycle_rule_counts[k]

        max_update_id = offset

        for upd in updates:
            uid = upd.get("update_id", 0)
            if uid > max_update_id:
                max_update_id = uid

            # Answer callback_query to dismiss the button spinner in managed bots
            cbq = upd.get("callback_query")
            if cbq:
                cbq_id = cbq.get("id")
                if cbq_id:
                    try:
                        await bot_api._call(
                            http, token, "answerCallbackQuery",
                            callback_query_id=cbq_id,
                            text="",
                        )
                    except Exception as e:
                        log_exc_swallow(log, "_process_bot: answer_callback")
                continue

            msg = upd.get("message")
            if not msg:
                continue
            # Bot Mesh: сообщение от ДРУГОГО бота — это не человек. Раньше такой
            # апдейт шёл в человеческую логику (бот регистрировался как «юзер»,
            # получал авто-ответ). Теперь: если это конверт задачи сети —
            # обрабатываем как mesh; в любом случае сообщения от ботов НЕ идут
            # дальше в воронки/авто-ответы.
            if (msg.get("from") or {}).get("is_bot"):
                try:
                    await _maybe_handle_mesh(pool, http, token, bot_id, msg)
                except Exception:
                    log.debug("auto_responder: mesh handling failed bot=%s",
                              bot_id, exc_info=True)
                continue
            # Только личка: в группе chat_id — это id группы, и весь путь ниже
            # (авто-ответы, /start, воронки, релей) отвечал бы В ГРУППУ и портил
            # базу подписчиков. Дочерний бот при инвайте Мать-Дочка попадает в
            # чат — без этой отсечки он спамит в него на каждое сообщение.
            if not _is_dm_update(upd):
                continue
            chat_id = msg.get("chat", {}).get("id")
            text = msg.get("text", "")
            if not chat_id or not text:
                continue

            is_start = text.strip().lower().startswith("/start")

            # Отписка от drip-цепочек. Раньше выйти из воронки было нечем:
            # единственный способ перестать получать шаги — заблокировать бота.
            # Человек, написавший «стоп», продолжал получать цепочку — это и
            # жалобы на спам, и безвозвратная потеря контакта для владельца.
            # Проверяем ДО всех правил автоответчика: на просьбу прекратить
            # нельзя отвечать очередным маркетинговым сообщением.
            if _is_stop_word(text):
                try:
                    _dropped = await db.unsubscribe_user_from_funnels(pool, bot_id, chat_id)
                except Exception:
                    log.exception("auto_responder: отписка от воронок bot=%s user=%s",
                                  bot_id, chat_id)
                    _dropped = 0
                try:
                    await bot_api.send_message(
                        http, token, chat_id,
                        "✅ Вы отписаны — цепочка сообщений остановлена."
                        if _dropped else
                        "✅ Активных цепочек сообщений нет — вам ничего не приходит.",
                    )
                except Exception:
                    log_exc_swallow(log, "auto_responder: подтверждение отписки")
                log.info("auto_responder: bot=%s user=%s отписан от %d воронок",
                         bot_id, chat_id, _dropped)
                continue

            # Extract user info once (used for notification + registration below)
            from_user = msg.get("from") or {}

            # Track user activity — returns True for first-ever message (new user)
            is_new_user = await db.upsert_user_activity(pool, bot_id, chat_id)

            # Notify bot owner about new user
            if is_new_user:
                log.info(
                    "new_user: bot_id=%s chat_id=%s main_bot=%s added_by=%s",
                    bot_id, chat_id, bool(main_bot), bot_row.get("added_by") if bot_row else None,
                )
            if is_new_user and main_bot and bot_row and bot_row.get("added_by"):
                # Анти-накрутка: троттлим уведомления ПО БОТУ, а не по каждому юзеру.
                # Иначе поток фейков (накрутка) слал бы тысячи DM через main-bot,
                # клал его в FloodWait и «ронял» Infragram для всех.
                _n = _new_user_notify_decide(bot_id)
                if _n is not None:
                    owner_id = bot_row["added_by"]
                    bot_name = (
                        bot_row.get("username")
                        or bot_row.get("first_name")
                        or f"id{bot_id}"
                    )
                    if _n <= 1:
                        user_name = (
                            from_user.get("username")
                            or from_user.get("first_name")
                            or f"id{chat_id}"
                        )
                        note = (f"👤 <b>Новый пользователь</b> @{user_name} "
                                f"подписался на @{bot_name}")
                    else:
                        note = (f"👤 <b>+{_n} новых подписчиков</b> на @{bot_name} "
                                f"за последнюю минуту.\n⚠️ Если вы не запускали "
                                f"продвижение — возможно, идёт накрутка ботами.")
                    # dedup_key per-bot: уведомления по этому боту делят один слот
                    # кулдауна (не по каждому юзеру) — второй барьер против флуда.
                    from services.bg_tasks import spawn
                    spawn(
                        db.notify_if_enabled(
                            pool, main_bot, owner_id, "new_user", note,
                            dedup_key=f"{bot_id}:new",
                        )
                    )

            # Register in bot_users so the user appears in broadcast audience
            await db.upsert_users(
                pool,
                bot_id,
                [
                    {
                        "user_id": chat_id,
                        "username": from_user.get("username", ""),
                        "first_name": from_user.get("first_name", ""),
                        "last_name": from_user.get("last_name", ""),
                        "language_code": from_user.get("language_code", ""),
                    }
                ],
            )

            # Защита от накрутки/ботов (per-bot, off по умолчанию). Решение по
            # новому подписчику: detect — только наблюдение/алерт; protect —
            # пометить suspect (вон из аудитории рассылок); block — вдобавок не
            # давать никакой «выгоды» (диплинк/реферал/воронка/автоответ/ИИ).
            if is_new_user and bot_row and bot_row.get("added_by"):
                try:
                    from services import flood_guard as _fg
                    _flood = await _fg.handle_new_user(
                        pool, bot_id, int(bot_row["added_by"]), chat_id)
                except Exception:
                    log_exc_swallow(log, f"flood_guard bot={bot_id}")
                    _flood = None
                if _flood and _flood.get("block"):
                    log.info(
                        "flood_guard: заблокирован подозрительный подписчик "
                        "bot=%s user=%s rate=%s", bot_id, chat_id, _flood.get("rate"))
                    continue  # атакующий не получает ничего

            # Deep link tracking: /start <param>
            if text.strip().lower().startswith("/start "):
                parts = text.strip().split(None, 1)
                if len(parts) == 2:
                    param = parts[1].strip()
                    link_id = await db.record_deep_link_visit(
                        pool, bot_id, param, chat_id
                    )
                    if param.startswith("ref") and param[3:].isdigit():
                        referrer_id = int(param[3:])
                        if referrer_id != chat_id:
                            await db.record_referral(
                                pool, bot_id, referrer_id, chat_id, link_id
                            )

            # Track non-command keywords for SEO analytics
            if not text.startswith("/"):
                await db.record_message_keywords(pool, bot_id, text)

            # Bot admin panel: /admin TOKEN (owner only)
            if (
                text.strip().lower().startswith("/admin ")
                and bot_row
                and bot_row.get("added_by")
            ):
                admin_token = text.strip()[7:].strip()
                if admin_token:
                    admin_row = await db.get_bot_admin_session_by_token(
                        pool, admin_token
                    )
                    if (
                        admin_row
                        and admin_row["bot_id"] == bot_id
                        and chat_id == admin_row["owner_id"]
                    ):
                        user_count = (
                            await pool.fetchval(
                                "SELECT COUNT(*) FROM bot_users WHERE bot_id=$1", bot_id
                            )
                            or 0
                        )
                        reply_count = (
                            await pool.fetchval(
                                "SELECT COUNT(*) FROM auto_replies WHERE bot_id=$1 AND is_active=TRUE",
                                bot_id,
                            )
                            or 0
                        )
                        funnel_count = (
                            await pool.fetchval(
                                "SELECT COUNT(*) FROM funnels WHERE bot_id=$1 AND is_active=true",
                                bot_id,
                            )
                            or 0
                        )
                        panel_text = (
                            "🔧 <b>Панель управления ботом</b>\n\n"
                            f"👥 Пользователей: {user_count}\n"
                            f"💬 Авто-ответов: {reply_count}\n"
                            f"🔄 Активных воронок: {funnel_count}\n\n"
                            "📌 Управление через Infragram:\n"
                            "• Авто-ответы: Настройки → Авто-ответы\n"
                            "• Рассылка: Broadcasts\n"
                            "• Воронки: Настройки → Воронки\n"
                            "• CRM и пользователи: Inbox / Relay\n\n"
                            "<i>Авторизация подтверждена ✅</i>"
                        )
                        ok, _ = await bot_api.send_message(
                            http, token, chat_id, panel_text
                        )
                        if not ok:
                            log.warning(
                                "auto_responder: failed to send admin panel to chat %d bot %d",
                                chat_id,
                                bot_id,
                            )
                        continue  # skip normal auto_replies

            # Relay mode: skip automated responses — relay.py forwards to operator.
            # Exception: /start and /support still get a response so users know how to reach support.
            if bot_row and bot_row.get("relay_enabled"):
                _SUPPORT_TRIGGERS = ("/support", "💬 написать в поддержку")
                if is_start:
                    # Prefer operator's configured start rule; fall back to generic welcome
                    start_rules = [r for r in rules if r["trigger_type"] == "start"]
                    if start_rules:
                        rendered = _render_text(start_rules[0]["response_text"], from_user, bot_row)
                        if _is_free:
                            rendered = brand_injection.add_promo(rendered, html=True, context="broadcast")
                        await bot_api.send_message(http, token, chat_id, rendered, buttons=_rule_buttons(start_rules[0]))
                    else:
                        fname = from_user.get("first_name") or "друг"
                        bot_name = bot_row.get("username") or bot_row.get("first_name") or "бот"
                        welcome = (
                            f"👋 Привет, <b>{fname}</b>!\n\n"
                            f"Добро пожаловать в <b>@{bot_name}</b>.\n\n"
                            "Если вам нужна помощь — нажмите кнопку ниже, чтобы связаться с оператором поддержки."
                        )
                        if _is_free:
                            welcome = brand_injection.add_promo(welcome, html=True, context="broadcast")
                        rkb = {
                            "keyboard": [[{"text": "💬 Написать в поддержку"}]],
                            "resize_keyboard": True,
                            "one_time_keyboard": False,
                        }
                        await bot_api.send_message(http, token, chat_id, welcome, reply_markup=rkb)
                elif text.strip().lower() in _SUPPORT_TRIGGERS:
                    ack = (
                        "✅ <b>Запрос принят!</b>\n\n"
                        "Оператор поддержки скоро ответит вам. "
                        "Вы можете написать детали вашего вопроса прямо здесь."
                    )
                    if _is_free:
                        ack = brand_injection.add_promo(ack, html=True, context="broadcast")
                    await bot_api.send_message(http, token, chat_id, ack)
                continue

            # ── Сущность-менеджер: назначенная боту sales-персона ведёт диалог ──
            # Персона = живой продавец: приоритетнее шаблонных правил. Не для /start
            # (там welcome/воронка) и не в relay-режиме (выше уже continue). Fail-open:
            # любой сбой → падаем в обычные правила, поллер не роняем.
            if not is_start:
                _res = None
                try:
                    from services import bot_sales_persona as _bsp
                    _owner = (bot_row or {}).get("added_by")
                    if _owner:
                        _res = await _bsp.handle_incoming(
                            pool, bot_id, int(_owner), chat_id, text,
                            username=(from_user.get("username") or ""),
                            name=(from_user.get("first_name") or ""))
                except Exception:
                    log_exc_swallow(log, f"auto_responder: sales persona bot={bot_id}")
                    _res = None
                if _res is not None:      # у бота есть активная персона → она ведёт диалог
                    # Диалог уже у живого оператора → бот молчит (не поверх человека).
                    if _res.get("silent"):
                        continue
                    _reply = _res.get("reply") or ""
                    if _is_free and _reply:
                        _reply = brand_injection.add_promo(
                            _reply, html=False, context="broadcast")
                    _rkb = None
                    _rows = [[{"text": (c.get("title") or "Канал"), "url": c["url"]}]
                             for c in (_res.get("channels") or []) if c.get("url")][:4]
                    if _rows:
                        _rkb = {"inline_keyboard": _rows}
                    if _reply:
                        # доставка «по-человечески»: печатает… → пауза → короткие реплики
                        await _deliver_sales_reply(http, token, chat_id, _reply, _rkb)
                    if _res.get("handoff") and _res.get("operator"):
                        await _notify_operator(http, token, _res["operator"],
                                               chat_id, from_user, text)
                    if _res.get("order_confirmed") and _res.get("order_operator"):
                        await _notify_operator_order(
                            http, token, _res["order_operator"],
                            chat_id, from_user, _res.get("order_summary") or "")
                    try:
                        await pool.execute(
                            "INSERT INTO auto_reply_log(bot_id, chat_id, rule_type, "
                            "trigger_type, keyword) VALUES($1,$2,'auto_reply',"
                            "'sales_persona',$3)",
                            bot_id, chat_id,
                            "handoff" if _res.get("handoff") else "reply")
                    except Exception:
                        log.debug("auto_reply_log persona insert failed bot=%d", bot_id)
                    continue      # персона обработала сообщение — правила пропускаем

            # Auto-replies (правила отсортированы по priority DESC; первое
            # совпавшее И попадающее в рабочее окно — выигрывает)
            _cur_hour = datetime.utcnow().hour
            for rule in rules:
                if not _match_rule(rule, text):
                    continue
                # Рабочие часы: вне окна правило молчит (проф. автоответчик).
                if not _within_active_window(
                    _cur_hour, rule.get("active_from_hour"), rule.get("active_to_hour")
                ):
                    continue
                # Человекоподобная задержка перед ответом (анти-детект).
                _delay = rule.get("reply_delay_sec") or 0
                try:
                    _delay = max(0, min(300, int(_delay)))
                except (TypeError, ValueError):
                    _delay = 0
                if _delay:
                    await asyncio.sleep(_delay)
                rendered = _render_text(rule["response_text"], from_user, bot_row)
                if _is_free:
                    rendered = brand_injection.add_promo(rendered, html=True, context="broadcast")
                ok, retry = await bot_api.send_message(
                    http, token, chat_id, rendered, buttons=_rule_buttons(rule)
                )
                if ok:
                    # Log the fired rule to auto_reply_log for analytics
                    try:
                        await pool.execute(
                            """INSERT INTO auto_reply_log
                                   (bot_id, chat_id, rule_id, rule_type, trigger_type, keyword)
                               VALUES ($1, $2, $3, 'auto_reply', $4, $5)""",
                            bot_id,
                            chat_id,
                            rule.get("id"),
                            rule.get("trigger_type"),
                            rule.get("keyword"),
                        )
                    except Exception as _log_err:
                        log.debug(
                            "auto_reply_log insert failed bot=%d: %s", bot_id, _log_err
                        )
                else:
                    log.warning(
                        "auto_responder: failed to send auto-reply to chat %d bot %d%s",
                        chat_id,
                        bot_id,
                        f" (rate-limited {retry}s)" if retry else "",
                    )
                break

            # Passive inbox relay: forward non-/start messages to operator even when
            # relay_enabled=false.  Old bots that have custom auto-replies ("Сообщение
            # успешно отправлено") still need to deliver messages to the bot owner.
            # Uses the same relay_sessions / relay_messages tables → reply-back works.
            if (
                not is_start
                and text
                and bot_row
                and not bot_row.get("relay_enabled")
                and bot_row.get("added_by")
                and main_bot
            ):
                added_by = bot_row["added_by"]
                try:
                    uname = from_user.get("username")
                    fname = from_user.get("first_name", "")
                    lname = from_user.get("last_name", "")
                    user_label = (
                        f"@{uname}"
                        if uname
                        else (f"{fname} {lname}".strip() or f"ID:{chat_id}")
                    )
                    bname = (
                        bot_row.get("username")
                        or bot_row.get("first_name")
                        or str(bot_id)
                    )
                    fwd_text = (
                        f"📨 <b>@{bname}</b>  |  👤 {user_label}\n"
                        f"<i>ID: {chat_id}</i>\n\n"
                        f"{text}\n\n"
                        f"<i>← Reply чтобы ответить пользователю</i>"
                    )
                    session_id = await db.get_or_create_relay_session(
                        pool, bot_id, chat_id, uname, fname
                    )
                    sent = await main_bot.send_message(
                        added_by, fwd_text, parse_mode="HTML"
                    )
                    await db.save_relay_message(
                        pool,
                        session_id,
                        "in",
                        text,
                        sent.message_id if sent else None,
                    )
                except Exception:
                    log.exception(
                        "auto_responder: passive relay failed bot=%d chat=%d",
                        bot_id,
                        chat_id,
                    )

            # Swarm routing: /start on entry bot with swarm enabled
            if (
                is_start
                and bot_row
                and bot_row["swarm_enabled"]
                and bot_row["bot_role"] == "entry"
            ):
                await routing_engine.make_routing_decision(
                    pool,
                    http,
                    bot_id,
                    chat_id,
                    chat_id,
                    token,
                    bot_row["cluster"] or "default",
                )

            # Automation rules
            newly_added_tags: list[str] = []
            _rate_key = (bot_id, chat_id)
            for arule in automation_rules:
                # Rate limit: cap rules fired per user per polling cycle
                if _cycle_rule_counts.get(_rate_key, 0) >= _MAX_RULES_PER_USER_PER_CYCLE:
                    log.debug(
                        "auto_responder: rate-limit hit for bot=%d chat=%d — skipping remaining rules",
                        bot_id,
                        chat_id,
                    )
                    break

                triggered = False
                if arule["trigger_type"] == "message_received":
                    triggered = True
                elif arule["trigger_type"] == "keyword" and arule.get("trigger_value"):
                    triggered = arule["trigger_value"].lower() in text.lower()
                elif arule["trigger_type"] == "user_joined" and is_new_user:
                    triggered = True

                if triggered:
                    _cycle_rule_counts[_rate_key] = _cycle_rule_counts.get(_rate_key, 0) + 1
                    if arule["action_type"] == "send_message":
                        rendered = _render_text(
                            arule["action_value"], from_user, bot_row
                        )
                        if _is_free:
                            rendered = brand_injection.add_promo(rendered, html=True, context="broadcast")
                        ok, _ = await bot_api.send_message(
                            http, token, chat_id, rendered
                        )
                        if not ok:
                            log.warning(
                                "auto_responder: failed to send automation message to chat %d bot %d",
                                chat_id,
                                bot_id,
                            )
                    elif arule["action_type"] == "add_tag":
                        await db.add_user_tag(
                            pool, bot_id, chat_id, arule["action_value"]
                        )
                        newly_added_tags.append(arule["action_value"])
                    elif arule["action_type"] == "remove_tag":
                        await db.remove_user_tag(
                            pool, bot_id, chat_id, arule["action_value"]
                        )
                    elif arule["action_type"] == "subscribe_funnel":
                        try:
                            await db.subscribe_to_funnel(
                                pool, int(arule["action_value"]), chat_id
                            )
                        except (ValueError, TypeError):
                            log_exc_swallow(
                                log,
                                "Неверный funnel_id в правиле авто-ответа (блок 1)",
                                rule_id=arule.get("id"),
                                action_value=arule.get("action_value"),
                            )
                    elif arule["action_type"] == "create_deal":
                        # Create a CRM deal for this user.
                        # action_value is used as deal title prefix; falls back to "Новая заявка".
                        try:
                            title_prefix = arule.get("action_value") or "Новая заявка"
                            user_label = (
                                from_user.get("username")
                                or from_user.get("first_name")
                                or str(chat_id)
                            )
                            deal_title = f"{title_prefix} — {user_label}"
                            # crm_deals owner-scoped (owner_id/title/contact/stage) —
                            # прежний INSERT bot_id/user_id/status ссылался на
                            # несуществующие колонки → падал, сделка не создавалась.
                            _deal_owner = await pool.fetchval(
                                "SELECT added_by FROM managed_bots WHERE bot_id=$1", bot_id
                            )
                            if _deal_owner:
                                await pool.execute(
                                    """INSERT INTO crm_deals
                                           (owner_id, title, contact, stage, created_at)
                                       VALUES ($1, $2, $3, 'new', NOW())""",
                                    _deal_owner,
                                    deal_title,
                                    user_label,
                                )
                        except Exception as exc:
                            log.warning(
                                "auto_responder: create_deal failed bot=%d chat=%d: %s",
                                bot_id,
                                chat_id,
                                exc,
                            )
                    elif arule["action_type"] == "webhook":
                        # action_value = URL to POST to
                        url = arule.get("action_value", "").strip()
                        if url:
                            try:
                                payload = {
                                    "bot_id": bot_id,
                                    "chat_id": chat_id,
                                    "trigger_type": arule["trigger_type"],
                                    "trigger_value": arule.get("trigger_value"),
                                    "user": {
                                        "id": chat_id,
                                        "username": from_user.get("username", ""),
                                        "first_name": from_user.get("first_name", ""),
                                    },
                                    "text": text,
                                }
                                async with http.post(
                                    url,
                                    json=payload,
                                    timeout=aiohttp.ClientTimeout(total=10),
                                ) as resp:
                                    log.debug(
                                        "webhook action: url=%s status=%s",
                                        url,
                                        resp.status,
                                    )
                            except Exception as exc:
                                log.warning(
                                    "webhook action failed: url=%s error=%s", url, exc
                                )
                    elif arule["action_type"] == "send_ai_reply":
                        # action_value = system prompt / persona description
                        system_prompt = (
                            arule.get("action_value") or "Ты полезный ассистент."
                        )
                        try:
                            from config import OPENAI_API_KEY

                            if OPENAI_API_KEY:
                                ai_payload = {
                                    "model": "gpt-4o-mini",
                                    "messages": [
                                        {"role": "system", "content": system_prompt},
                                        {"role": "user", "content": text},
                                    ],
                                    "max_tokens": 300,
                                }
                                headers = {
                                    "Authorization": f"Bearer {OPENAI_API_KEY}",
                                    "Content-Type": "application/json",
                                }
                                async with http.post(
                                    "https://api.openai.com/v1/chat/completions",
                                    json=ai_payload,
                                    headers=headers,
                                    timeout=aiohttp.ClientTimeout(total=30),
                                ) as resp:
                                    if resp.status == 200:
                                        ai_data = await resp.json()
                                        ai_text = ai_data["choices"][0]["message"][
                                            "content"
                                        ].strip()
                                        if _is_free:
                                            ai_text = brand_injection.add_promo(ai_text, html=False, context="broadcast")
                                        await bot_api.send_message(
                                            http, token, chat_id, ai_text
                                        )
                        except Exception as exc:
                            log.warning("send_ai_reply failed: %s", exc)

            # tag_added rules: fire once for each tag added above (one level, no recursion)
            for arule in automation_rules:
                if (
                    arule["trigger_type"] == "tag_added"
                    and arule.get("trigger_value")
                    and arule["trigger_value"] in newly_added_tags
                ):
                    if arule["action_type"] == "send_message":
                        _arule_text = arule["action_value"]
                        if _is_free:
                            _arule_text = brand_injection.add_promo(_arule_text, html=True, context="broadcast")
                        await bot_api.send_message(
                            http, token, chat_id, _arule_text
                        )
                    elif arule["action_type"] == "add_tag":
                        await db.add_user_tag(
                            pool, bot_id, chat_id, arule["action_value"]
                        )
                    elif arule["action_type"] == "remove_tag":
                        await db.remove_user_tag(
                            pool, bot_id, chat_id, arule["action_value"]
                        )
                    elif arule["action_type"] == "subscribe_funnel":
                        try:
                            await db.subscribe_to_funnel(
                                pool, int(arule["action_value"]), chat_id
                            )
                        except (ValueError, TypeError):
                            log_exc_swallow(
                                log,
                                "Неверный funnel_id в правиле авто-ответа (блок 2)",
                                rule_id=arule.get("id"),
                                action_value=arule.get("action_value"),
                            )
                    elif arule["action_type"] == "webhook":
                        url = arule.get("action_value", "").strip()
                        if url:
                            try:
                                payload = {
                                    "bot_id": bot_id,
                                    "chat_id": chat_id,
                                    "trigger_type": arule["trigger_type"],
                                    "trigger_value": arule.get("trigger_value"),
                                    "user": {
                                        "id": chat_id,
                                        "username": from_user.get("username", ""),
                                        "first_name": from_user.get("first_name", ""),
                                    },
                                    "text": text,
                                }
                                async with http.post(
                                    url,
                                    json=payload,
                                    timeout=aiohttp.ClientTimeout(total=10),
                                ) as resp:
                                    log.debug(
                                        "webhook action: url=%s status=%s",
                                        url,
                                        resp.status,
                                    )
                            except Exception as exc:
                                log.warning(
                                    "webhook action failed: url=%s error=%s", url, exc
                                )
                    elif arule["action_type"] == "send_ai_reply":
                        system_prompt = (
                            arule.get("action_value") or "Ты полезный ассистент."
                        )
                        try:
                            from config import OPENAI_API_KEY

                            if OPENAI_API_KEY:
                                ai_payload = {
                                    "model": "gpt-4o-mini",
                                    "messages": [
                                        {"role": "system", "content": system_prompt},
                                        {"role": "user", "content": text},
                                    ],
                                    "max_tokens": 300,
                                }
                                headers = {
                                    "Authorization": f"Bearer {OPENAI_API_KEY}",
                                    "Content-Type": "application/json",
                                }
                                async with http.post(
                                    "https://api.openai.com/v1/chat/completions",
                                    json=ai_payload,
                                    headers=headers,
                                    timeout=aiohttp.ClientTimeout(total=30),
                                ) as resp:
                                    if resp.status == 200:
                                        ai_data = await resp.json()
                                        ai_text = ai_data["choices"][0]["message"][
                                            "content"
                                        ].strip()
                                        if _is_free:
                                            ai_text = brand_injection.add_promo(ai_text, html=False, context="broadcast")
                                        await bot_api.send_message(
                                            http, token, chat_id, ai_text
                                        )
                        except Exception as exc:
                            log.warning("send_ai_reply failed: %s", exc)

            # Funnels: subscribe on /start, keyword, or new-user join
            for funnel in funnels:
                if funnel["trigger_type"] == "start" and is_start:
                    await db.subscribe_to_funnel(pool, funnel["id"], chat_id)
                elif funnel["trigger_type"] == "join" and is_new_user:
                    # Fire once for first-ever message from a new user
                    await db.subscribe_to_funnel(pool, funnel["id"], chat_id)
                elif (
                    funnel["trigger_type"] == "keyword"
                    and funnel["keyword"]
                    and funnel["keyword"].lower() in text.lower()
                ):
                    await db.subscribe_to_funnel(pool, funnel["id"], chat_id)

            # A/B experiment: assign variant on /start and SEND the variant content
            if is_start and active_exp:
                variant = await db.assign_experiment_variant(
                    pool, bot_id, chat_id, active_exp["id"]
                )
                if variant and variant.get("content"):
                    exp_content = variant["content"]
                    if _is_free:
                        exp_content = brand_injection.add_promo(exp_content, html=True, context="broadcast")
                    await bot_api.send_message(http, token, chat_id, exp_content)
            elif not is_start and active_exp:
                # Conversion: any subsequent message from an assigned user counts
                try:
                    await db.record_experiment_conversion(
                        pool, bot_id, chat_id, active_exp["id"]
                    )
                except Exception:
                    log_exc_swallow(
                        log,
                        "Сбой record_experiment_conversion",
                        bot_id=bot_id,
                        chat_id=chat_id,
                    )

            # Relay: forward message to operator if relay is enabled for this bot
            if bot_row and bot_row.get("relay_enabled") and bot_row.get("added_by") and main_bot:
                try:
                    operator_id = bot_row["added_by"]
                    username = from_user.get("username")
                    first_name_r = from_user.get("first_name", "")
                    bot_label = (
                        f"@{bot_row['username']}"
                        if bot_row.get("username")
                        else (bot_row.get("first_name") or str(bot_id))
                    )
                    user_label = (
                        f"@{username}"
                        if username
                        else (f"{first_name_r} {from_user.get('last_name', '')}".strip() or f"ID:{chat_id}")
                    )
                    session_id = await db.get_or_create_relay_session(
                        pool, bot_id, chat_id, username, first_name_r
                    )
                    fwd_text = (
                        f"📨 <b>{bot_label}</b>  |  👤 {user_label}\n"
                        f"<i>ID: {chat_id}</i>\n\n"
                        f"{text}\n\n"
                        f"<i>← Reply здесь чтобы ответить пользователю</i>"
                    )
                    sent = await main_bot.send_message(
                        operator_id, fwd_text, parse_mode="HTML"
                    )
                    await db.save_relay_message(
                        pool, session_id, "in", text,
                        sent.message_id if sent else None,
                    )
                except Exception as _relay_err:
                    log.warning("auto_responder: relay forward failed bot=%d: %s", bot_id, _relay_err)

    except Exception:
        log.exception("Auto-responder error for bot %d", bot_id)
    finally:
        if max_update_id > offset:
            try:
                await db.set_update_offset(pool, bot_id, max_update_id)
            except Exception:
                # Не сдвинули — следующий цикл разберёт пакет заново. Это
                # повтор, а не потеря, и молчать о нём нельзя.
                log.warning(
                    "auto_responder: bot=%d не сохранён оффсет %d — пакет "
                    "будет разобран повторно", bot_id, max_update_id,
                    exc_info=True,
                )


async def run(pool: asyncpg.Pool, http: aiohttp.ClientSession, main_bot=None) -> None:
    global _inactivity_sweep_task, _sales_followup_task, _cloud_reconcile_task
    _inactivity_sweep_task = asyncio.create_task(run_inactivity_sweep(pool, http))
    _sales_followup_task = asyncio.create_task(run_sales_followup_sweep(pool, http))
    _cloud_reconcile_task = asyncio.create_task(run_cloud_reconcile_sweep(pool))
    # Stagger startup — don't hammer DB immediately alongside other services
    await asyncio.sleep(10)
    _cycle = 0
    while True:
        try:
            # Раз в ~5 минут — рассказать о ботах, которые молчат и сами не
            # починятся. Чаще незачем: метка «уже сообщили» всё равно не даст
            # повторить, а лишний запрос каждые 10 секунд бессмысленен.
            _cycle += 1
            if _cycle % 30 == 1:
                # Сначала самолечение: снять зависший вебхук у ботов в CONFLICT и
                # вернуть их на polling ДО того, как о поломке сообщат владельцу.
                try:
                    from services import bot_healer
                    healed = await bot_healer.heal_broken_bots(pool, http)
                    if healed:
                        log.info("auto_responder: самолечением возвращено ботов: %d", healed)
                except Exception:
                    log.debug("auto_responder: самолечение ботов не выполнено")
                # Сверка имени и @username с Telegram. Их записывали один раз при
                # подключении бота и больше никогда: переименование в @BotFather
                # до списка ботов не доходило. Сам проход редкий (см.
                # bot_profile_refresh.REFRESH_AFTER_HOURS) и только читает.
                try:
                    from services import bot_profile_refresh as _bpr
                    _upd = await _bpr.refresh_bot_profiles(pool, http)
                    if _upd:
                        log.info("auto_responder: профиль обновлён у ботов: %d", _upd)
                except Exception:
                    log.debug("auto_responder: сверка профилей ботов не выполнена")
            if main_bot is not None and _cycle % 30 == 1:
                try:
                    await notify_broken_bots(pool, main_bot)
                except Exception:
                    log.debug("auto_responder: уведомления о сломанных ботах не отправлены")
            bots = await db.get_bots_for_polling(pool)
            if bots:
                now = time.monotonic()
                pollable = [
                    b for b in bots
                    if now >= _dead_token_cooldown.get(b["bot_id"], (0, 0.0))[1]
                ]
                await asyncio.gather(
                    *(
                        _process_bot(pool, http, b["bot_id"], b["token"], main_bot)
                        for b in pollable
                    ),
                    return_exceptions=True,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Auto-responder loop error")
        await asyncio.sleep(10)


async def run_cloud_reconcile_sweep(pool: asyncpg.Pool) -> None:
    """Фоновое самовосстановление облака (флот-бэкенд): гасим локации забаненных
    хранителей и доливаем избыточность на живые аккаунты — доступ к файлам держится
    сам, без ручных действий после банов. На БД-бэкенде делать нечего (куски не на
    аккаунтах) — цикл просто спит. Сбой одного прохода не роняет цикл."""
    await asyncio.sleep(900)  # старт через 15 мин после подъёма
    while True:
        try:
            from services import tg_cloud
            if tg_cloud._backend() == "fleet":
                from services import tg_cloud_fleet
                rep = await tg_cloud_fleet.reconcile(pool)
                if rep.get("marked_dead") or rep.get("restored") or rep.get("unhealable"):
                    log.info("cloud reconcile: %s", rep)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("cloud_reconcile_sweep error")
        await asyncio.sleep(1800)  # каждые 30 минут


async def run_inactivity_sweep(pool: asyncpg.Pool, http: aiohttp.ClientSession) -> None:
    """Background sweep: fire inactivity automation rules."""
    await asyncio.sleep(600)  # startup delay 10 min
    while True:
        try:
            await _inactivity_sweep(pool, http)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("inactivity_sweep error")
        await asyncio.sleep(3600)  # hourly


async def run_sales_followup_sweep(pool: asyncpg.Pool, http: aiohttp.ClientSession) -> None:
    """Фоновый дожим: клиент проявил интерес и замолчал → менеджер сам пишет.

    Идёт раз в ~5 минут. Антидубль — followup_sent_at (шлём один раз на диалог).
    Молчание/сбой одного бота не роняет цикл."""
    await asyncio.sleep(120)  # старт со сдвигом
    while True:
        try:
            from services import bot_sales_persona as _bsp
            due = await _bsp.list_followup_due(pool, limit=200)
            for d in due:
                token = d.get("token"); chat_id = d.get("customer_chat_id")
                msg = (d.get("followup_message") or "").strip()
                if not (token and chat_id and msg):
                    # нечего слать — всё равно помечаем, чтобы не крутить вечно
                    await _bsp.mark_followup_sent(pool, d["dialog_id"])
                    continue
                try:
                    await _deliver_sales_reply(http, token, chat_id, msg)
                    await _bsp.mark_followup_sent(pool, d["dialog_id"])
                except Exception:
                    log_exc_swallow(log, "auto_responder: followup send failed")
            # Защита от накрутки: закрываем протухшие эпизоды атак (последний
            # всплеск был давно) — чтобы статус на карточке бота был точным и
            # следующая атака открывала новый эпизод. Дёшево, весь флот разом.
            try:
                from services import flood_guard as _fg
                ended = await _fg.end_stale_episodes(pool)
                if ended:
                    log.info("flood_guard: закрыто протухших эпизодов: %d", ended)
            except Exception:
                log_exc_swallow(log, "auto_responder: flood episode sweep")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("sales followup sweep error")
        await asyncio.sleep(300)  # каждые 5 минут


async def _inactivity_sweep(pool: asyncpg.Pool, http: aiohttp.ClientSession) -> None:
    """Find users inactive for N days and fire matching rules."""
    # Get all active inactivity rules
    from database.db import fetch_bots as _fetch_bots_ar
    rules = await _fetch_bots_ar(
        pool,
        """SELECT ar.*, mb.token
           FROM automation_rules ar
           JOIN managed_bots mb ON mb.bot_id = ar.bot_id
           WHERE ar.trigger_type = 'inactivity'
             AND ar.is_active = true
             AND mb.is_active = true""",
    )
    if not rules:
        return

    for rule in rules:
        try:
            inactivity_days = int(rule["trigger_value"] or "3")
        except (ValueError, TypeError):
            inactivity_days = 3

        # Find users inactive for N days (use user_activity table)
        inactive_users = await pool.fetch(
            """SELECT ua.user_id FROM user_activity ua
               WHERE ua.bot_id = $1
                 AND ua.last_seen < NOW() - ($2 * INTERVAL '1 day')
                 AND NOT EXISTS (
                     SELECT 1 FROM inactivity_alerts_sent ias
                     WHERE ias.bot_id = $1
                       AND ias.chat_id = ua.user_id
                       AND ias.rule_id = $3
                       AND ias.sent_at > NOW() - ($2 * INTERVAL '1 day')
                 )
               LIMIT 100""",
            rule["bot_id"],
            inactivity_days,
            rule["id"],
        )

        _rule_is_free = False
        try:
            _rule_is_free = await brand_injection.is_free_tier(pool, rule["bot_id"])
        except Exception as e:
            log_exc_swallow(log, "_inactivity_sweep: check_free_tier")

        for user in inactive_users:
            chat_id = user["user_id"]
            try:
                if rule["action_type"] == "send_message":
                    _inact_text = rule["action_value"]
                    if _rule_is_free:
                        _inact_text = brand_injection.add_promo(_inact_text, html=True, context="broadcast")
                    await bot_api.send_message(
                        http, rule["token"], chat_id, _inact_text
                    )
                elif rule["action_type"] == "add_tag":
                    await db.add_user_tag(
                        pool, rule["bot_id"], chat_id, rule["action_value"]
                    )
                elif rule["action_type"] == "remove_tag":
                    await db.remove_user_tag(
                        pool, rule["bot_id"], chat_id, rule["action_value"]
                    )
                elif rule["action_type"] == "create_deal":
                    title_prefix = rule.get("action_value") or "Реактивация"
                    # crm_deals owner-scoped — прежний INSERT bot_id/user_id/status
                    # ссылался на несуществующие колонки → падал.
                    _deal_owner = await pool.fetchval(
                        "SELECT added_by FROM managed_bots WHERE bot_id=$1", rule["bot_id"]
                    )
                    if _deal_owner:
                        await pool.execute(
                            """INSERT INTO crm_deals
                                   (owner_id, title, contact, stage, created_at)
                               VALUES ($1, $2, $3, 'new', NOW())""",
                            _deal_owner,
                            f"{title_prefix} — id{chat_id}",
                            str(chat_id),
                        )
                elif rule["action_type"] == "webhook":
                    url = (rule["action_value"] or "").strip()
                    if url:
                        await http.post(
                            url,
                            json={
                                "bot_id": rule["bot_id"],
                                "chat_id": chat_id,
                                "trigger_type": "inactivity",
                                "inactivity_days": inactivity_days,
                            },
                            timeout=aiohttp.ClientTimeout(total=10),
                        )

                # Mark as sent (prevent duplicate)
                await pool.execute(
                    """INSERT INTO inactivity_alerts_sent(bot_id, chat_id, rule_id)
                       VALUES($1, $2, $3)
                       ON CONFLICT(bot_id, chat_id, rule_id) DO UPDATE SET sent_at=NOW()""",
                    rule["bot_id"],
                    chat_id,
                    rule["id"],
                )
                await asyncio.sleep(0.1)  # tiny delay between messages
            except Exception as exc:
                log.warning("inactivity_sweep: failed for chat=%s: %s", chat_id, exc)
