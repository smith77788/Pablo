"""
Audience Parser Framework — извлечение аудитории из каналов/групп.

Поддерживает:
- participants: все участники канала/группы
- active: пользователи с недавними сообщениями
- commenters: авторы комментариев к постам
- reaction_givers: пользователи оставившие реакции

Результаты сохраняются в parsed_audiences (дедупликация по owner+source+user_id).
История запусков — в parser_runs.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Optional

from services.logger import log_exc_swallow
from services import infra_memory

import asyncpg

log = logging.getLogger(__name__)

# Потолок на ОТДЕЛЬНЫЙ запрос к Telegram. Значение то же, что в
# account_manager._OP_TIMEOUT, но константа своя: account_manager здесь
# импортируется ЛОКАЛЬНО, по функциям (защита от кольца импортов), и в
# _collect_and_save_senders его в области видимости нет вовсе.
#
# Коннект ограничен своим таймаутом, а мёртвый прокси чаще отдаёт не отказ,
# а half-open сокет: TCP установлен, ответа нет и не будет. Без потолка
# разбор аудитории вешался навсегда, держа слот операции и аккаунт.
_OP_TIMEOUT = 45



# ── Чистые хелперы (тестируемые, без БД/Telethon) ─────────────────────────

# Вид разбора — в родительном падеже, для подписей «Разбор участников из @x».
# Одно место для бота, мини-аппа и итога операции: раньше подпись операции
# выходила «Парсинг members из @…».
PARSE_TYPE_LABELS = {
    "members": "участников",
    "active": "активных",
    "comments": "комментаторов",
}


def normalize_source_ref(raw: str) -> str:
    """Нормализует ссылку/username источника к «голому» username или id.

    Пользователи вставляют что угодно: `@name`, `https://t.me/name`,
    `t.me/name?after=123`, `telegram.me/name/456`. Раньше на входе парсера был
    только `.lstrip("@")` — ссылка `https://t.me/name` уходила как есть и не
    резолвилась. Здесь убираем протокол, хосты t.me/telegram.me, ведущий '@',
    query-хвост и путь после username. Приватные инвайты (`+HASH`, `joinchat/…`)
    сохраняем как есть (это не username).
    """
    s = (raw or "").strip()
    if not s:
        return ""
    for pref in ("https://", "http://"):
        if s.lower().startswith(pref):
            s = s[len(pref):]
    low = s.lower()
    for host in ("t.me/", "telegram.me/", "telegram.dog/"):
        idx = low.find(host)
        if idx != -1:
            s = s[idx + len(host):]
            break
    s = s.lstrip("@")
    # приватные инвайт-ссылки не трогаем (это хэш, не username)
    if s.startswith("+") or s.lower().startswith("joinchat/"):
        return s.split("?", 1)[0].strip()
    for sep in ("?", "/"):
        if sep in s:
            s = s.split(sep, 1)[0]
    return s.strip()


def status_to_days(status) -> int | None:
    """Telethon User.status → примерное число дней с последнего онлайна (Last Seen).

    Telegram отдаёт статус вёдрами (точное время скрыто privacy-настройкой):
    online→0, recently→~2, last_week→~7, last_month→~30, offline→точная дата.
    None/empty/скрыт → None (неизвестно). Используется для фильтра «активные».
    """
    if status is None:
        return None
    name = type(status).__name__
    if name == "UserStatusOnline":
        return 0
    if name == "UserStatusRecently":
        return 2
    if name == "UserStatusLastWeek":
        return 7
    if name == "UserStatusLastMonth":
        return 30
    if name == "UserStatusOffline":
        was = getattr(status, "was_online", None)
        if was is not None:
            try:
                from datetime import datetime, timezone
                now = datetime.now(timezone.utc)
                delta = now - (was if was.tzinfo else was.replace(tzinfo=timezone.utc))
                return max(0, delta.days)
            except Exception:  # noqa: BLE001 — битая дата → неизвестно
                return None
    return None


# ── Чистые хелперы экспорта аудитории (тестируемые, без БД/Telethon) ───────
# Форматы для многоформатного экспорта parsed_audiences. CSV делает эндпоинт
# через _csv_resp; TXT/JSON — эти хелперы (паритет Telegram Expert по экспорту).

def audience_to_txt(users: list[dict]) -> str:
    """Экспорт в TXT: одна строка на пользователя (@username либо tg_user_id)."""
    lines: list[str] = []
    for u in users:
        un = (u.get("username") or "").strip()
        lines.append("@" + un if un else str(u.get("tg_user_id") or ""))
    return "\n".join(lines)


def audience_to_json(users: list[dict]) -> str:
    """Экспорт в JSON (список объектов)."""
    import json as _json

    return _json.dumps(list(users), ensure_ascii=False, default=str, indent=2)


# Сколько ждём флуд на месте. Дольше — обрываем разбор и честно говорим об
# этом: продолжать раньше срока значит получить следующий FloodWait длиннее
# предыдущего и подвести аккаунт под спамблок.
_FLOOD_INLINE_MAX_S = 120

# Сколько авторов подряд может не разрешиться, прежде чем сбор признаёт обрыв.
# Один-два удалённых аккаунта — норма, а десяток отказов подряд — это уже
# мёртвая сеть или прокси: дальше каждый запрос стоит _OP_TIMEOUT впустую.
_SENDER_FAIL_STREAK_MAX = 8


def flood_seconds(exc: BaseException) -> int | None:
    """Сколько Telegram просит подождать, или None если это не пауза.

    Псевдоним общего разбора из flood_engine: второй источник правды о паузах
    Telegram продукту не нужен.
    """
    from services import flood_engine as _fe

    return _fe.flood_seconds(exc)


# Причины обрыва разбора — человеческим языком. Ключ ищется в имени класса и в
# тексте ошибки (Telethon кладёт туда код RPC вида CHAT_ADMIN_REQUIRED).
# Раньше любая из них превращалась в `break` и запуск закрывался «найдено 0»:
# владелец не мог отличить пустой источник от закрытого.
_PARSE_ERROR_REASONS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("ChatAdminRequired", "CHAT_ADMIN_REQUIRED"),
     "Список подписчиков канала Telegram показывает только его администраторам. "
     "Соберите комментаторов канала или участников его группы обсуждений."),
    (("UserNotParticipant", "USER_NOT_PARTICIPANT"),
     "Аккаунт не состоит в этой группе, а Telegram отдаёт её список только участникам."),
    (("ChannelPrivate", "CHANNEL_PRIVATE", "ChatForbidden", "CHAT_FORBIDDEN"),
     "Источник закрыт: он приватный, или аккаунт в нём заблокирован."),
    (("InviteHashExpired", "INVITE_HASH_EXPIRED", "InviteHashInvalid", "INVITE_HASH_INVALID"),
     "Ссылка-приглашение недействительна или истекла."),
    (("UsernameNotOccupied", "USERNAME_NOT_OCCUPIED", "UsernameInvalid", "USERNAME_INVALID",
      "Cannot find any entity", "No user has", "ChannelInvalid", "CHANNEL_INVALID",
      "PeerIdInvalid", "PEER_ID_INVALID"),
     "Источник не найден — проверьте username или ссылку."),
)


def explain_parse_error(exc: BaseException) -> str:
    """Причина обрыва разбора для владельца. Никогда не отдаёт сырой текст.

    Сырой `str(exc)` — английский, с внутренними именами запросов, а у сетевых
    ошибок иногда и с адресом прокси. Владелец должен понять, что делать
    дальше, а не читать трассу Telethon.
    """
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return ("Telegram не ответил вовремя — у аккаунта не работает прокси или "
                "сеть. Повторите позже или проверьте прокси аккаунта.")
    name = type(exc).__name__
    text = str(exc)
    hay = f"{name} {text}"
    _fw = flood_seconds(exc)
    if _fw is not None:
        return f"Telegram ограничил частоту запросов на {_fw} с. Повторите позже."
    try:
        from services.op_errors import is_dead_session_text

        if is_dead_session_text(hay):
            return "Сессия аккаунта больше не действует — переподключите аккаунт."
    except Exception:
        log_exc_swallow(log, "parser: разбор мёртвой сессии не удался")
    for keys, reason in _PARSE_ERROR_REASONS:
        if any(k in hay for k in keys):
            return reason
    if isinstance(exc, (ConnectionError, OSError)) or "connection" in text.lower():
        return ("Связь аккаунта с Telegram оборвалась (прокси или сеть). "
                "Повторите позже или проверьте прокси аккаунта.")
    # Неизвестный отказ: имя класса — единственное, что безопасно показать.
    return f"Разбор прерван сбоем ({name}). Повторите позже."


async def _record_parse_flood(pool, account_id, seconds: int) -> None:
    """Флуд обязан попасть в общий пульс здоровья аккаунта.

    По нему остальные подсистемы понижают темп и держат аккаунт в покое.
    Раньше разбор переживал флуд в одиночку, и для продукта аккаунт оставался
    «спокойным» — следующая операция шла к нему как ни в чём не бывало.
    """
    if not account_id:
        return
    try:
        from services import flood_engine as _fe

        await _fe.record_flood(pool, int(account_id), int(seconds), "parse")
    except Exception:
        log_exc_swallow(log, "parser: FloodWait не записан в пульс здоровья")


# Сколько аккаунтов перебрать, если лучшие заняты. Пять — чтобы разбор не
# упирался в одну занятую сессию и при этом не перебирал весь флот.
_CLAIM_TRIES = 5


async def _claim_best_account(pool: asyncpg.Pool, owner_id: int) -> tuple[dict | None, int]:
    """Лучший СВОБОДНЫЙ аккаунт под разбор + его id для последующего release.

    Разбор аудитории — живое подключение аккаунтом, ничем не отличающееся от
    массовой операции, и берётся оно через того же арбитра
    (`op_worker.try_claim_account`). Без захвата разбор коннектил сессию,
    которую прямо сейчас ведёт операция. Процессный мьютекс сессии это не
    закрывает: роли `INFRAGRAM_ROLE=web` и `worker` — РАЗНЫЕ процессы, карта
    занятости у каждого своя, и единственный межпроцессный арбитр — аренда
    в БД.

    Занятый аккаунт не ждём, а исключаем и просим следующий по качеству:
    `get_best_account` умеет `exclude_ids`. Возвращает (аккаунт, id аренды);
    (None, 0) — свободных нет.
    """
    from services import op_worker as _opw
    from services.flood_engine import get_best_account

    exclude: list[int] = []
    for _ in range(_CLAIM_TRIES):
        acc = await get_best_account(
            pool, owner_id, action_type="parse", exclude_ids=exclude
        )
        if not acc:
            return None, 0
        acc_id = int(acc.get("id") or 0)
        if not acc_id:
            return dict(acc), 0
        if await _opw.try_claim_account(acc_id):
            return dict(acc), acc_id
        exclude.append(acc_id)
    return None, 0


async def _release_account(acc_id: int) -> None:
    """Отпустить аренду аккаунта после разбора. Своя ошибка разбор не роняет,
    но молчать нельзя: неотпущенная аренда паркует аккаунт в этом процессе."""
    if not acc_id:
        return
    try:
        from services import op_worker as _opw

        await _opw.release_accounts([acc_id])
    except Exception:
        log.warning("parser: аренда аккаунта не отпущена", exc_info=True)


async def _create_run(
    pool: asyncpg.Pool,
    owner_id: int,
    source_type: str,
    source_ref: str,
    parse_type: str,
    account_id: int | None,
) -> int:
    # Запуск рождается сразу «в работе»: 'pending' по умолчанию таблицы
    # описывал запуск, который никто ещё не взял, а разбор уже идёт.
    row = await pool.fetchrow(
        """INSERT INTO parser_runs(owner_id, source_type, source_ref, parse_type,
                                   account_id, status)
           VALUES ($1, $2, $3, $4, $5, 'running') RETURNING id""",
        owner_id,
        source_type,
        source_ref,
        parse_type,
        account_id,
    )
    return row["id"]


async def _update_run(
    pool: asyncpg.Pool,
    run_id: int,
    status: str,
    total_found: int = 0,
    total_saved: int = 0,
    error: str | None = None,
    source_id: int | None = None,
) -> None:
    await pool.execute(
        """UPDATE parser_runs
           SET status=$1, total_found=$2, total_saved=$3, error=$4, finished_at=NOW(),
               source_id=COALESCE($6, source_id)
           WHERE id=$5""",
        status,
        total_found,
        total_saved,
        error,
        run_id,
        source_id,
    )


async def _save_users(
    pool: asyncpg.Pool,
    owner_id: int,
    run_id: int,
    source_type: str,
    source_id: int,
    source_title: str,
    source_username: str,
    users: list[dict],
) -> int:
    from database import db as _db
    saved = 0
    for u in users:
        uid = u["id"]
        entity_type = "bot" if u.get("bot") else "user"
        display_name = " ".join(
            p for p in [u.get("first_name"), u.get("last_name")] if p
        ).strip() or None
        # Last Seen: last_seen_days из статуса; активным считаем ≤7 дней (online/
        # recently/last_week) — база для фильтра «активная аудитория».
        last_seen_days = u.get("last_seen_days")
        is_active = None if last_seen_days is None else (last_seen_days <= 7)
        try:
            result = await pool.execute(
                """INSERT INTO parsed_audiences(
                       owner_id, source_type, source_id, source_title, source_username,
                       parse_run_id, tg_user_id, username, first_name, last_name,
                       is_premium, is_bot, last_seen_days, is_active, parsed_at
                   ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,NOW())
                   ON CONFLICT (owner_id, source_id, tg_user_id) DO UPDATE
                   SET parse_run_id=$6, username=$8, first_name=$9, last_name=$10,
                       is_premium=$11,
                       last_seen_days=COALESCE(EXCLUDED.last_seen_days, parsed_audiences.last_seen_days),
                       is_active=COALESCE(EXCLUDED.is_active, parsed_audiences.is_active),
                       parsed_at=NOW()""",
                owner_id,
                source_type,
                source_id,
                source_title,
                source_username,
                run_id,
                uid,
                u.get("username"),
                u.get("first_name"),
                u.get("last_name"),
                bool(u.get("premium")),
                bool(u.get("bot")),
                last_seen_days,
                is_active,
            )
            if "INSERT" in str(result):
                saved += 1
        except Exception as e:
            log.debug("parser save user %s: %s", uid, e)
        # Radar: record sighting with the source chat context
        await _db.record_entity_sighting(pool, uid, entity_type, chat_id=source_id)
        await _db.record_name_snapshot(pool, uid, entity_type, u.get("username"), display_name)
    return saved


def _user_row(u: Any) -> dict:
    return {
        "id": u.id,
        "username": getattr(u, "username", None),
        "first_name": getattr(u, "first_name", None),
        "last_name": getattr(u, "last_name", None),
        "premium": getattr(u, "premium", False),
        "bot": getattr(u, "bot", False),
        "last_seen_days": status_to_days(getattr(u, "status", None)),
    }


# ── Один разбор — одна дверь ───────────────────────────────────────────────


class _ParseState:
    """Что разбор успел сделать и почему остановился.

    Три вида разбора раньше держали этот учёт каждый у себя, и каждый терял
    его по-своему: обрыв внутри цикла становился `break` без причины, а
    исключение вне внутреннего `try` (тайм-аут подключения, сбой посреди
    `iter_messages`) уходило через `finally`, не закрыв запуск, — и тот
    навсегда оставался «в работе».
    """

    def __init__(self, source_ref: str) -> None:
        self.run_id: int | None = None
        self.account_id: int | None = None
        self.total_found = 0
        self.total_saved = 0
        self.flood_wait = 0
        # Почему разбор не дошёл до конца — человеческим языком (или None).
        self.stop_reason: str | None = None
        # Что не обрыв, а честная оговорка о полноте (потолок выдачи Telegram).
        self.note: str | None = None
        self.cancelled = False
        self.source_id = 0
        self.source_title = source_ref
        self.source_username = source_ref.lstrip("@")

    def stop(self, reason: str) -> None:
        if not self.stop_reason:
            self.stop_reason = reason

    @property
    def final_status(self) -> str:
        """Статус запуска в parser_runs.

        cancelled — остановил владелец; failed — не собрано ни одного человека
        из-за ошибки (это НЕ «пусто»); empty — источник честно пуст; done —
        собрано, а неполнота (обрыв, флуд) объяснена в `error`.
        """
        if self.cancelled:
            return "cancelled"
        if self.total_found > 0:
            return "done"
        if self.stop_reason or self.flood_wait:
            return "failed"
        return "empty"

    @property
    def run_note(self) -> str | None:
        """Текст для `parser_runs.error`: причина обрыва или оговорка."""
        if self.cancelled:
            return (f"Остановлено владельцем. Собрано {self.total_found} — "
                    "это не вся аудитория." if self.total_found else
                    "Остановлено владельцем до первого результата.")
        if self.flood_wait:
            return (f"Разбор остановлен: Telegram просит подождать {self.flood_wait} с. "
                    f"Собрано {self.total_found} — это не вся аудитория, повторите позже.")
        if self.stop_reason:
            if self.total_found:
                return (f"Разбор оборван: {self.stop_reason} Собрано "
                        f"{self.total_found} — это не вся аудитория.")
            return self.stop_reason
        return self.note

    @property
    def partial(self) -> bool:
        return self.total_found > 0 and bool(
            self.cancelled or self.flood_wait or self.stop_reason)


async def _check_stop(should_stop, st: "_ParseState") -> bool:
    """Спросить владельца-через-шину, не отменён ли разбор. Сбой опроса —
    не отмена: рвать живой разбор из-за моргнувшей БД нельзя."""
    if should_stop is None:
        return False
    try:
        if await should_stop():
            st.cancelled = True
            return True
    except Exception:
        log_exc_swallow(log, "parser: опрос отмены не удался")
    return False


async def _run_parse(
    pool: asyncpg.Pool,
    owner_id: int,
    source_ref: str,
    *,
    source_type: str,
    parse_type: str,
    collect: Callable[[Any, Any, "_ParseState"], Any],
) -> dict:
    """Захват аккаунта → запуск → подключение → источник → сбор → итог.

    Единственная дверь всех трёх разборов. Её обещание: запуск, созданный
    здесь, ВСЕГДА закрывается терминальным статусом (done / empty / failed /
    cancelled) с причиной на человеческом языке — что бы ни случилось внутри,
    включая отмену задачи. Висящий «в работе» запуск владелец видел вечно.
    """
    from services import account_manager

    acc, _leased = await _claim_best_account(pool, owner_id)
    if not acc:
        return {"status": "error",
                "error": "Нет свободных аккаунтов — все заняты или на паузе. "
                         "Повторите через минуту."}

    st = _ParseState(source_ref)
    st.account_id = acc.get("id")
    client = None
    t0_parse = time.monotonic()

    # Пост-захватный путь под одним try с release в finally: _create_run и
    # _make_client раньше стояли ВНЕ try — их сбой уводил захваченный аккаунт
    # мимо release (корень «весь флот занят без операций»).
    try:
        st.run_id = await _create_run(
            pool, owner_id, source_type, source_ref, parse_type, acc["id"])
        try:
            client = account_manager._make_client(acc["session_str"], acc)
            await asyncio.wait_for(client.connect(), timeout=15)
            entity = await asyncio.wait_for(
                client.get_entity(source_ref), timeout=_OP_TIMEOUT)
            st.source_id = getattr(entity, "id", 0) or 0
            st.source_title = getattr(entity, "title", None) or source_ref
            st.source_username = (getattr(entity, "username", None)
                                  or source_ref.lstrip("@"))
            await collect(client, entity, st)
        except asyncio.CancelledError:
            st.cancelled = True
            raise
        except Exception as e:
            _fw = flood_seconds(e)
            if _fw is not None:
                await _record_parse_flood(pool, acc["id"], _fw)
                st.flood_wait = st.flood_wait or _fw
            else:
                log.warning("parser %s %s: %s", parse_type, source_ref, type(e).__name__)
                st.stop(explain_parse_error(e))
    finally:
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                log_exc_swallow(log, "Сбой disconnect клиента в parser")
        await _release_account(_leased)
        if st.run_id is not None:
            # shield: при отмене задачи запись итога всё равно должна дойти.
            try:
                await asyncio.shield(_update_run(
                    pool, st.run_id, st.final_status, st.total_found,
                    st.total_saved, error=st.run_note,
                    source_id=st.source_id or None))
            except Exception:
                log.warning("parser: итог запуска #%s не записан", st.run_id,
                            exc_info=True)

    _parse_dur = time.monotonic() - t0_parse
    if st.total_found > 0:
        infra_memory.record_account_op(acc["id"], "parse", True, duration_s=_parse_dur)
    else:
        infra_memory.record_account_op(
            acc["id"], "parse", False,
            (st.stop_reason or "no_users_found")[:100], duration_s=_parse_dur)

    status = st.final_status
    out = {
        "run_id": st.run_id,
        "status": "error" if status == "failed" else status,
        "total_found": st.total_found,
        "total_saved": st.total_saved,
        "source_title": st.source_title,
        "flood_wait": st.flood_wait,
        "partial": st.partial,
        "note": st.run_note,
    }
    if status == "failed":
        out["error"] = st.run_note
    return out


async def parse_members(
    pool: asyncpg.Pool,
    owner_id: int,
    source_ref: str,
    limit: int = 5000,
    progress_cb: Optional[Callable[[int, int], Any]] = None,
    should_stop: Optional[Callable[[], Any]] = None,
) -> dict:
    """
    Парсинг участников канала/группы.
    progress_cb(current, total) вызывается каждые 200 пользователей;
    should_stop() — опрос отмены (шина операций), проверяется перед каждой пачкой.
    Возвращает {'run_id', 'total_found', 'total_saved', 'status', 'partial', 'note'}.
    """
    from telethon.tl.functions.channels import GetParticipantsRequest
    from telethon.tl.types import ChannelParticipantsSearch

    async def _collect(client, entity, st: _ParseState) -> None:
        offset = 0
        batch_size = 200
        reported = 0
        while st.total_found < limit:
            if await _check_stop(should_stop, st):
                return
            try:
                result = await asyncio.wait_for(client(
                    GetParticipantsRequest(
                        entity,
                        ChannelParticipantsSearch(""),
                        offset=offset,
                        limit=min(batch_size, limit - st.total_found),
                        hash=0,
                    )
                ), timeout=_OP_TIMEOUT)
            except Exception as e:
                _fw = flood_seconds(e)
                if _fw is None:
                    raise
                await _record_parse_flood(pool, st.account_id, _fw)
                if _fw > _FLOOD_INLINE_MAX_S:
                    log.info("parser: FloodWait %dс дольше порога — разбор оборван", _fw)
                    st.flood_wait = _fw
                    return
                log.info("parser FloodWait %ds, sleeping", _fw)
                await asyncio.sleep(_fw + 5)
                continue

            reported = max(reported, int(getattr(result, "count", 0) or 0))
            if not result.users:
                break

            users = [_user_row(u) for u in result.users if not getattr(u, "deleted", False)]
            st.total_saved += await _save_users(
                pool, owner_id, st.run_id, "channel", st.source_id,
                st.source_title, st.source_username, users,
            )
            st.total_found += len(users)
            offset += len(result.users)

            if progress_cb:
                try:
                    await progress_cb(st.total_found, min(limit, reported or limit))
                except Exception:
                    log_exc_swallow(log, "Сбой progress_cb в parser")

            if len(result.users) < batch_size:
                break

            await asyncio.sleep(1.5)  # Anti-flood pause

        # Потолок выдачи. Telegram отдаёт стороннему аккаунту не больше ~10 тыс.
        # участников (а у группы со скрытым списком — только админов), хотя
        # `count` называет всех. Без оговорки владелец считал собранное полным.
        if (st.total_found < limit and reported > offset
                and not st.stop_reason and not st.flood_wait):
            st.note = (f"Telegram показал {offset} из {reported} участников — "
                       "остальных он сторонним аккаунтам не отдаёт (потолок выдачи "
                       "или скрытый список участников).")

    return await _run_parse(pool, owner_id, source_ref, source_type="channel",
                            parse_type="members", collect=_collect)


async def _collect_and_save_senders(
    pool: asyncpg.Pool,
    owner_id: int,
    run_id: int,
    client: Any,
    entity: Any,
    *,
    source_type: str,
    source_id: int,
    source_title: str,
    source_username: str,
    days_back: int,
    limit: int,
    scan_cap: int = 5000,
    progress_cb: Optional[Callable[[int, int], Any]] = None,
    account_id: int | None = None,
    should_stop: Optional[Callable[[], Any]] = None,
) -> tuple[int, int, int, str | None]:
    """Итерирует сообщения ``entity`` и сохраняет уникальных отправителей.

    Общая логика для парсинга активных участников группы и комментаторов
    (сообщения в привязанной к каналу группе обсуждений). Возвращает
    (total_found, total_saved, flood_wait, stop_reason).

    `flood_wait` — сколько Telegram попросил подождать, если разбор пришлось
    оборвать. Раньше `get_entity` на каждого отправителя стоял под голым
    `except Exception`, то есть FloodWait просто проглатывался: цикл шёл
    дальше и продолжал долбить Telegram под действующим ограничением. Так
    аккаунт и зарабатывает спамблок вместо паузы.

    `stop_reason` — почему сбор оборван не флудом: серия отказов подряд
    (мёртвая сеть, прокси), сбой самого `iter_messages`, отмена владельцем
    ("cancelled").
    """
    from datetime import datetime, timedelta, timezone

    seen_ids: set[int] = set()
    total_found = 0
    total_saved = 0
    flood_wait = 0
    stop_reason: str | None = None
    fail_streak = 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)

    # Сбой самого обхода сообщений (сеть, доступ) не уходит наверх: собранное
    # уже лежит в базе, и счёт ему обязан вернуться вместе с причиной обрыва —
    # иначе часть базы выглядела бы провалом «найдено 0».
    try:
        async for msg in client.iter_messages(entity, limit=scan_cap):
            if msg.date < cutoff:
                break
            # Отрицательный sender_id — канал или чат (пост канала, анонимный
            # админ), а не человек: в аудиторию людей ему не место.
            if not msg.sender_id or msg.sender_id < 0 or msg.sender_id in seen_ids:
                continue
            seen_ids.add(msg.sender_id)

            if should_stop is not None:
                try:
                    if await should_stop():
                        stop_reason = "cancelled"
                        break
                except Exception:
                    log_exc_swallow(log, "parser: опрос отмены не удался")

            user = None
            try:
                user = await asyncio.wait_for(client.get_entity(msg.sender_id), timeout=_OP_TIMEOUT)
                fail_streak = 0
            except Exception as exc:
                _fw = flood_seconds(exc)
                if _fw is not None:
                    await _record_parse_flood(pool, account_id, _fw)
                    if _fw > _FLOOD_INLINE_MAX_S:
                        log.info(
                            "parser: FloodWait %dс дольше порога — сбор оборван", _fw)
                        flood_wait = _fw
                        break
                    log.info("parser: FloodWait %dс при сборе отправителей", _fw)
                    await asyncio.sleep(_fw + 5)
                    continue
                fail_streak += 1
                log_exc_swallow(log, "Сбой get_entity в parser", sender_id=msg.sender_id)
                if fail_streak >= _SENDER_FAIL_STREAK_MAX:
                    stop_reason = explain_parse_error(exc)
                    break

            # У канала и чата есть title, у человека — нет.
            if user is not None and getattr(user, "title", None) is None:
                saved = await _save_users(
                    pool,
                    owner_id,
                    run_id,
                    source_type,
                    source_id,
                    source_title,
                    source_username,
                    [_user_row(user)],
                )
                total_found += 1
                total_saved += saved

            if total_found >= limit:
                break

            # Progress: first update at 1 user found, then every 50 after that.
            # Without this, parses returning <50 users show no progress at all.
            if total_found == 1 or total_found % 50 == 0:
                if progress_cb:
                    try:
                        await progress_cb(total_found, limit)
                    except Exception:
                        log_exc_swallow(log, "Сбой progress_cb в parser")
                await asyncio.sleep(0.5)
    except Exception as exc:
        _fw = flood_seconds(exc)
        if _fw is not None:
            await _record_parse_flood(pool, account_id, _fw)
            flood_wait = _fw
        else:
            log.warning("parser: обход сообщений оборван: %s", type(exc).__name__)
            stop_reason = explain_parse_error(exc)

    return total_found, total_saved, flood_wait, stop_reason


_BROADCAST_REFUSAL = (
    "Это канал, а не группа: в канале пишет только администратор. "
    "Выберите «Комментаторы» — они собираются из обсуждений канала.")
_NO_DISCUSSION = (
    "У канала нет открытых обсуждений — комментаторов собрать неоткуда.")


def _senders_collector(
    pool, owner_id: int, *, source_type: str, days_back: int, limit: int,
    progress_cb, should_stop, target_of=None, refuse_broadcast: bool = False,
):
    """Сбор авторов сообщений как шаг `_run_parse` (активные и комментаторы)."""

    async def _collect(client, entity, st: _ParseState) -> None:
        target = await target_of(client, entity) if target_of else entity
        if getattr(target, "broadcast", False):
            # В канале пишет только админ — авторов-людей там нет по природе.
            # Раньше разбор честно находил 0 и выдавал это за пустой источник.
            st.stop(_BROADCAST_REFUSAL if refuse_broadcast else _NO_DISCUSSION)
            return
        # source_id — id той сущности, из которой реально собираем (для дедупа).
        st.source_id = getattr(target, "id", 0) or st.source_id
        found, saved, fw, reason = await _collect_and_save_senders(
            pool, owner_id, st.run_id, client, target,
            source_type=source_type, source_id=st.source_id,
            source_title=st.source_title, source_username=st.source_username,
            days_back=days_back, limit=limit, progress_cb=progress_cb,
            account_id=st.account_id, should_stop=should_stop,
        )
        st.total_found += found
        st.total_saved += saved
        st.flood_wait = st.flood_wait or fw
        if reason == "cancelled":
            st.cancelled = True
        elif reason:
            st.stop(reason)

    return _collect


async def parse_active_users(
    pool: asyncpg.Pool,
    owner_id: int,
    source_ref: str,
    days_back: int = 30,
    limit: int = 2000,
    progress_cb: Optional[Callable[[int, int], Any]] = None,
    should_stop: Optional[Callable[[], Any]] = None,
) -> dict:
    """
    Парсинг активных пользователей: те, кто писал в группе за последние N дней.
    Работает только для супергрупп (не каналов) — канал отклоняется с причиной.
    """
    collect = _senders_collector(
        pool, owner_id, source_type="group", days_back=days_back, limit=limit,
        progress_cb=progress_cb, should_stop=should_stop, refuse_broadcast=True)
    return await _run_parse(pool, owner_id, source_ref, source_type="group",
                            parse_type="active", collect=collect)


async def _discussion_target(client, entity):
    """Группа обсуждений канала — комментарии живут в ней. Нет её — сам источник."""
    from telethon.tl.functions.channels import GetFullChannelRequest

    target = entity
    try:
        full = await asyncio.wait_for(client(GetFullChannelRequest(entity)), timeout=_OP_TIMEOUT)
        linked_id = getattr(full.full_chat, "linked_chat_id", None)
        if linked_id:
            try:
                target = await asyncio.wait_for(client.get_entity(linked_id), timeout=_OP_TIMEOUT)
            except Exception:
                log_exc_swallow(
                    log, "Не удалось получить группу обсуждений", linked_id=linked_id
                )
    except Exception:
        # Источник — обычная группа без обсуждений: парсим её напрямую.
        log_exc_swallow(log, "GetFullChannel недоступен, парсим источник напрямую")
    return target


async def parse_commenters(
    pool: asyncpg.Pool,
    owner_id: int,
    source_ref: str,
    days_back: int = 90,
    limit: int = 2000,
    progress_cb: Optional[Callable[[int, int], Any]] = None,
    should_stop: Optional[Callable[[], Any]] = None,
) -> dict:
    """
    Парсинг комментаторов канала: авторы сообщений в привязанной к каналу
    группе обсуждений. Если источник сам является группой — парсит её
    сообщения напрямую (эквивалент «активных»).
    """
    collect = _senders_collector(
        pool, owner_id, source_type="comments", days_back=days_back, limit=limit,
        progress_cb=progress_cb, should_stop=should_stop,
        target_of=_discussion_target)
    return await _run_parse(pool, owner_id, source_ref, source_type="comments",
                            parse_type="comments", collect=collect)


async def get_parsed_audience(
    pool: asyncpg.Pool,
    owner_id: int,
    source_id: int | None = None,
    run_id: int | None = None,
    offset: int = 0,
    limit: int = 200,
    active_only: bool = False,
    max_last_seen_days: int | None = None,
) -> list[dict]:
    """Получить сохранённую аудиторию с фильтрами.

    max_last_seen_days: оставить только тех, кто был в сети не позже N дней назад
    (Last Seen фильтр) — пользователи с неизвестным статусом отсекаются.
    """
    conditions = ["owner_id=$1"]
    params: list = [owner_id]
    p = 2

    if source_id:
        conditions.append(f"source_id=${p}")
        params.append(source_id)
        p += 1

    if run_id:
        conditions.append(f"parse_run_id=${p}")
        params.append(run_id)
        p += 1

    if active_only:
        conditions.append("is_active=TRUE")

    if max_last_seen_days is not None:
        conditions.append(f"last_seen_days IS NOT NULL AND last_seen_days <= ${p}")
        params.append(int(max_last_seen_days))
        p += 1

    where = " AND ".join(conditions)
    rows = await pool.fetch(
        f"SELECT * FROM parsed_audiences WHERE {where} "
        f"ORDER BY parsed_at DESC, id OFFSET ${p} LIMIT ${p + 1}",
        *params,
        offset,
        limit,
    )
    return [dict(r) for r in rows]


async def get_run_history(
    pool: asyncpg.Pool, owner_id: int, limit: int = 20
) -> list[dict]:
    rows = await pool.fetch(
        """SELECT id, source_type, source_ref, parse_type, status,
                  total_found, total_saved, started_at, finished_at, error
           FROM parser_runs WHERE owner_id=$1
           ORDER BY started_at DESC LIMIT $2""",
        owner_id,
        limit,
    )
    return [dict(r) for r in rows]


async def delete_audience(
    pool: asyncpg.Pool,
    owner_id: int,
    source_id: int | None = None,
    run_id: int | None = None,
) -> int:
    """Удалить спарсенную аудиторию. Возвращает число удалённых строк."""
    if run_id:
        result = await pool.execute(
            "DELETE FROM parsed_audiences WHERE owner_id=$1 AND parse_run_id=$2",
            owner_id,
            run_id,
        )
    elif source_id:
        result = await pool.execute(
            "DELETE FROM parsed_audiences WHERE owner_id=$1 AND source_id=$2",
            owner_id,
            source_id,
        )
    else:
        result = await pool.execute(
            "DELETE FROM parsed_audiences WHERE owner_id=$1", owner_id
        )
    try:
        return int(str(result).split()[-1])
    except Exception:
        log_exc_swallow(
            log, "Не удалось распарсить количество удалённых строк аудитории"
        )
        return 0
