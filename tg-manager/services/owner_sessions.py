"""Входы владельца в Infragram: журнал, завершение, тревога о новом устройстве.

До этого модуля вход владельца был невидим и неотзывен. Сессионный токен
самоподписан и живёт свои два часа; кто вошёл, с какого устройства и откуда —
нигде не записывалось, и «выйти на всех устройствах» было нечем. Для продукта,
где один вход открывает весь флот, это главная дыра: владелец не узнал бы, что
в панели работает кто-то ещё.

Теперь:

* каждый вход (Telegram, бот-зеркало, связанное устройство) — строка
  `owner_sessions` с устройством, адресом и временем; повторный вход того же
  устройства продлевает свою строку, а не плодит новые;
* номер сеанса (`sid`) едет внутри токена (`mini_app_auth.make_token`), и
  завершённый сеанс отбивается на каждом запросе: `is_revoked` читает список
  отозванных из памяти, который фоновый цикл раз в 15 с сверяет с базой —
  так завершение работает и между процессами web/worker;
* вход с устройства, которого у владельца не было 90 дней, или из новой
  страны — сообщение в бот с кнопкой «Это не я — завершить».

Честная граница. Вход в мини-апп подтверждает Telegram: кто владеет
Telegram-аккаунтом владельца, тот войдёт снова и после завершения сеанса.
Поэтому тревога о чужом входе прямо говорит, что делать в самом Telegram
(Настройки → Устройства), а настоящая вторая стена — свой код Infragram для
нового устройства — следующая волна. Связанное устройство (PWA) завершается
целиком: его ключ отзывается вместе с сеансом.
"""
from __future__ import annotations

import asyncio
import hashlib
import html
import logging
import re
import secrets
from datetime import datetime, timezone

log = logging.getLogger(__name__)

KINDS = ("telegram", "mirror", "device")
KIND_RU = {"telegram": "Telegram", "mirror": "Бот-зеркало", "device": "Связанное устройство"}
# Повторный вход того же устройства в эти дни продлевает его строку.
_REUSE_DAYS = 30
# Устройство, которого не было столько дней, — «новое».
_NEW_DEVICE_DAYS = 90
# Токен живёт 2 ч, пропуск потока — тоже 2 ч: дольше помнить отзыв незачем.
_REVOKED_WINDOW_H = 3
_SYNC_PERIOD_S = 15.0

# DB_BACKED: истина — owner_sessions.revoked_at; память — кэш, который цикл
# run_revocation_sync сверяет с базой раз в 15 с (tests/test_no_new_mutable_globals).
_REVOKED: set[str] = set()


# ── Чистое ядро ──────────────────────────────────────────────────────────────

def new_sid() -> str:
    return secrets.token_urlsafe(12).replace(":", "_")


def device_fp(user_agent: str | None) -> str:
    """Отпечаток устройства — по строке браузера, без адреса.

    Адрес у телефона меняется на каждом переходе Wi-Fi → мобильная сеть; с ним
    в отпечатке владелец получал бы «новое устройство» несколько раз в день и
    перестал бы читать тревогу.
    """
    ua = re.sub(r"\s+", " ", str(user_agent or "").strip())[:400]
    return hashlib.sha256(ua.encode("utf-8")).hexdigest()[:32]


def device_label(user_agent: str | None) -> str:
    """«iPhone · iOS 17.5 · Telegram» — по-человечески, без строки браузера."""
    ua = str(user_agent or "")
    parts: list[str] = []
    m = re.search(r"\b(iPhone|iPad)\b", ua)
    if m:
        parts.append(m.group(1))
        v = re.search(r"OS (\d+)[_.](\d+)", ua)
        if v:
            parts.append(f"iOS {v.group(1)}.{v.group(2)}")
    elif "Android" in ua:
        v = re.search(r"Android (\d+(?:\.\d+)?)", ua)
        model = re.search(r"Android [^;)]*; ([^;)]+?)(?: Build|\))", ua)
        if model and model.group(1).strip() not in ("K", "wv"):
            parts.append(model.group(1).strip()[:32])
        parts.append("Android " + v.group(1) if v else "Android")
    elif "Windows" in ua:
        parts.append("Windows")
    elif "Macintosh" in ua or "Mac OS X" in ua:
        parts.append("Mac")
    elif "Linux" in ua:
        parts.append("Linux")
    if re.search(r"Telegram", ua, re.IGNORECASE):
        parts.append("Telegram")
    elif "Edg/" in ua:
        parts.append("Edge")
    elif "Chrome/" in ua:
        parts.append("Chrome")
    elif "Firefox/" in ua:
        parts.append("Firefox")
    elif "Safari/" in ua:
        parts.append("Safari")
    return " · ".join(parts) or "Неизвестное устройство"


def request_meta(request) -> dict:
    """Что известно о входящем запросе: устройство, адрес, страна."""
    from services.security import _client_ip
    ua = request.headers.get("User-Agent", "") if request is not None else ""
    try:
        ip = _client_ip(request) if request is not None else ""
    except Exception:
        ip = ""
    country = ""
    if request is not None:
        country = (request.headers.get("CF-IPCountry") or "").strip().upper()[:2]
        if country in ("XX", "T1"):
            country = ""
    return {"ua": ua[:400], "fp": device_fp(ua), "label": device_label(ua),
            "ip": str(ip or "")[:64], "country": country}


def is_alert_worthy(history: list[dict], meta: dict) -> str | None:
    """Почему этот вход стоит тревоги: 'device' | 'country' | None.

    history — прежние входы владельца за 90 дней: [{fp, country}]. Пустая
    история — первый вход после выкатки: тревожить не о чем, иначе каждый
    владелец получил бы «новое устройство» о своём же телефоне.
    """
    if not history:
        return None
    if meta.get("fp") not in {h.get("fp") for h in history}:
        return "device"
    seen = {h.get("country") for h in history if h.get("country")}
    if meta.get("country") and seen and meta["country"] not in seen:
        return "country"
    return None


def is_revoked(sid: str | None) -> bool:
    """Завершён ли сеанс (из памяти, без базы: зовётся на каждом запросе)."""
    return bool(sid) and sid in _REVOKED


def _when(dt) -> str:
    if not dt:
        return ""
    try:
        return dt.astimezone(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
    except (AttributeError, ValueError, OverflowError):
        return ""


def owner_identity(owner_id: int | None) -> list[str]:
    lines = []
    if owner_id is not None:
        uid = int(owner_id)
        lines.append(f'Аккаунт владельца: <a href="tg://user?id={uid}">Telegram ID {uid}</a>')
    lines.append("Это вход в панель Infragram, а не новое устройство подключённого Telegram-аккаунта.")
    return lines


def session_details(row: dict, owner_id: int) -> str:
    """Public session fields only; never reveal sid or pairing credentials."""
    lines = ["🔎 <b>Сведения о входе в Infragram</b>", *owner_identity(owner_id), "",
             f"Сеанс: <b>№{int(row['id'])}</b>",
             f"Устройство: {html.escape(row.get('device_label') or 'Неизвестное устройство')}",
             f"Способ: {html.escape(KIND_RU.get(row.get('kind'), 'Неизвестно'))}",
             f"Откуда: {html.escape(' · '.join(str(row.get(k) or '') for k in ('country', 'ip')).strip(' ·') or 'неизвестно')}",
             f"Первый вход: {_when(row.get('created_at')) or 'неизвестно'}",
             f"Последняя активность: {_when(row.get('last_seen_at')) or 'неизвестно'}",
             "Состояние: " + ("завершён" if row.get('revoked_at') else "не завершён")]
    return "\n".join(lines)


async def get_session(pool, owner_id: int, session_id: int) -> dict | None:
    row = await pool.fetchrow(
        """SELECT id, kind, device_label, ip, country, created_at, last_seen_at, revoked_at
           FROM owner_sessions WHERE owner_id=$1 AND id=$2""",
        int(owner_id), int(session_id))
    return dict(row) if row else None


def build_alert(meta: dict, kind: str, why: str, at=None, *, owner_id: int | None = None) -> str:
    where = " · ".join(x for x in (meta.get("country"), meta.get("ip")) if x)
    head = ("🔐 <b>Вход в Infragram с нового устройства</b>" if why == "device"
            else "🌍 <b>Вход в Infragram из новой страны</b>")
    lines = [head, "", *owner_identity(owner_id),
             f"Устройство: <b>{html.escape(meta.get('label') or 'неизвестно')}</b>",
             f"Способ: {html.escape(KIND_RU.get(kind, kind))}"]
    if where:
        lines.append(f"Откуда: {html.escape(where)}")
    lines.append(f"Когда: {_when(at or datetime.now(timezone.utc))}")
    lines += ["", "Если это вы — ничего делать не нужно.",
              "Если нет — нажмите «Это не я». Вход через Telegram значит, что "
              "кто-то открыл ваш Telegram: завершите чужие сеансы и там — "
              "<b>Настройки → Устройства</b>, и включите облачный пароль."]
    return "\n".join(lines)


# ── База ─────────────────────────────────────────────────────────────────────

async def open_session(pool, owner_id: int, kind: str, meta: dict, *,
                       pairing_id: int | None = None, notify: bool = True) -> str | None:
    """Записать вход и вернуть номер сеанса для токена. Не бросает.

    None — записать не удалось: вход всё равно проходит (токен без номера, как
    до этого модуля). Запереть владельца из-за сбоя журнала хуже, чем пропустить
    одну строку журнала.
    """
    kind = kind if kind in KINDS else "telegram"
    try:
        row = await pool.fetchrow(
            """SELECT id, sid FROM owner_sessions
               WHERE owner_id=$1 AND device_fp=$2 AND kind=$3 AND revoked_at IS NULL
                 AND last_seen_at > now() - make_interval(days => $4)
                 AND ($5::bigint IS NULL OR pairing_id = $5)
               ORDER BY last_seen_at DESC LIMIT 1""",
            int(owner_id), meta["fp"], kind, _REUSE_DAYS, pairing_id)
        if row:
            await pool.execute(
                "UPDATE owner_sessions SET last_seen_at=now(), ip=$2, "
                "country=COALESCE(NULLIF($3, ''), country) WHERE id=$1",
                row["id"], meta.get("ip") or "", meta.get("country") or "")
            return row["sid"]
        history = [dict(r) for r in await pool.fetch(
            """SELECT device_fp AS fp, country FROM owner_sessions
               WHERE owner_id=$1 AND created_at > now() - make_interval(days => $2)""",
            int(owner_id), _NEW_DEVICE_DAYS)]
        sid = new_sid()
        await pool.execute(
            """INSERT INTO owner_sessions(owner_id, sid, kind, device_fp, device_label,
                                          user_agent, ip, country, pairing_id)
               VALUES($1, $2, $3, $4, $5, $6, $7, $8, $9)""",
            int(owner_id), sid, kind, meta["fp"], meta.get("label") or "",
            meta.get("ua") or "", meta.get("ip") or "", meta.get("country") or "",
            pairing_id)
    except Exception as exc:
        log.warning("owner_sessions.open_session owner=%s: %s", owner_id, exc)
        return None
    why = is_alert_worthy(history, meta)
    if why and notify:
        # Тревога уходит в фоне: вход не ждёт Telegram (повторы доставки
        # длятся секундами).
        from services.bg_tasks import spawn
        spawn(_alert(pool, int(owner_id), sid, kind, meta, why), name="owner_login_alert")
    return sid


async def _alert(pool, owner_id: int, sid: str, kind: str, meta: dict, why: str) -> None:
    """Сообщение о входе — от основного бота (мини-апп своего бота не держит)."""
    from aiogram import Bot
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode
    from aiogram.utils.keyboard import InlineKeyboardBuilder

    from bot.callbacks import OwnSessCb
    from config import BOT_TOKEN
    from database import db
    bot = None
    try:
        sess_id = await pool.fetchval("SELECT id FROM owner_sessions WHERE sid=$1 AND owner_id=$2", sid, owner_id)
        kb = InlineKeyboardBuilder()
        kb.button(text="🔎 Посмотреть сеанс",
                  callback_data=OwnSessCb(action="view", session_id=int(sess_id or 0)))
        kb.button(text="🚫 Это не я — завершить вход",
                  callback_data=OwnSessCb(action="kill", session_id=int(sess_id or 0)))
        kb.adjust(1)
        bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        # Секретное слово в хвост (services/owner_safeword): тревога без него —
        # подделка, по ссылке переходить нельзя.
        from services import owner_safeword
        text = build_alert(meta, kind, why, owner_id=owner_id) + await owner_safeword.line(pool, owner_id)
        await db.notify_if_enabled(pool, bot, owner_id, "restriction", text,
                                   reply_markup=kb.as_markup(),
                                   dedup_key=f"owner_login:{sid}")
    except Exception as exc:
        log.warning("owner_sessions: тревога о входе owner=%s не ушла: %s", owner_id, exc)
    finally:
        if bot is not None:
            try:
                await bot.session.close()
            except Exception as exc:
                log.debug("owner_sessions: закрытие бота: %s", exc)


async def list_sessions(pool, owner_id: int, limit: int = 50, *, strict: bool = False) -> list[dict]:
    try:
        rows = await pool.fetch(
            """SELECT id, sid, kind, device_label, ip, country, created_at,
                      last_seen_at, revoked_at
               FROM owner_sessions WHERE owner_id=$1
               ORDER BY (revoked_at IS NULL) DESC, last_seen_at DESC LIMIT $2""",
            int(owner_id), int(limit))
    except Exception as exc:
        log.warning("owner_sessions.list owner=%s: %s", owner_id, exc)
        if strict:
            raise
        return []
    return [dict(r) for r in rows]


async def revoke(pool, owner_id: int, *, session_id: int | None = None,
                 all_except_sid: str | None = None) -> int:
    """Завершить один сеанс владельца или все, кроме текущего. → сколько.

    Связанное устройство завершается целиком: его ключ (device_pairings)
    отзывается, иначе приложение тут же обменяло бы его на новый сеанс.
    """
    if session_id is None and all_except_sid is None:
        return 0
    rows = await pool.fetch(
        """UPDATE owner_sessions SET revoked_at=now()
           WHERE owner_id=$1 AND revoked_at IS NULL
             AND (($2::bigint IS NOT NULL AND id=$2)
                  OR ($2::bigint IS NULL AND sid <> $3))
           RETURNING sid, pairing_id""",
        int(owner_id), session_id, all_except_sid or "")
    for r in rows:
        _REVOKED.add(r["sid"])
        if r["pairing_id"]:
            try:
                from services import device_pairing
                await device_pairing.revoke_device(pool, int(owner_id), int(r["pairing_id"]))
            except Exception as exc:
                log.warning("owner_sessions.revoke: ключ устройства %s: %s", r["pairing_id"], exc)
    return len(rows)


async def revoke_by_pairing(pool, owner_id: int, pairing_id: int) -> None:
    """Ключ устройства отозван — живой сеанс этого устройства тоже завершить,
    а не ждать, пока истекут его два часа."""
    try:
        rows = await pool.fetch(
            "UPDATE owner_sessions SET revoked_at=now() WHERE owner_id=$1 AND pairing_id=$2 "
            "AND revoked_at IS NULL RETURNING sid", int(owner_id), int(pairing_id))
        _REVOKED.update(r["sid"] for r in rows)
    except Exception as exc:
        log.warning("owner_sessions.revoke_by_pairing %s: %s", pairing_id, exc)


async def sync_revoked(pool) -> None:
    rows = await pool.fetch(
        "SELECT sid FROM owner_sessions WHERE revoked_at > now() - make_interval(hours => $1)",
        _REVOKED_WINDOW_H)
    fresh = {r["sid"] for r in rows}
    _REVOKED.intersection_update(fresh)
    _REVOKED.update(fresh)


async def run_revocation_sync(pool) -> None:
    """Сверять список завершённых сеансов с базой — для всех процессов."""
    while True:
        try:
            await sync_revoked(pool)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("owner_sessions.sync: %s", exc)
        await asyncio.sleep(_SYNC_PERIOD_S)
