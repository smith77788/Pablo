"""Сигналы атаки на аккаунт флота: чужой код входа, сброс и смена пароля 2FA.

Дозор чужих сеансов (`session_watch`) видит угон, когда он УЖЕ случился: чужой
вход появился в списке авторизаций. Но у угона есть признаки раньше и рядом,
и все они видны тем же соединением, которым обход флота и так ходит в аккаунт
раз в 6 часов:

* **Код входа, после которого никто не вошёл.** Telegram пишет код в служебный
  чат 777000. Если в аккаунт после этого не вошёл ни наш, ни доверенный, ни
  чужой сеанс — кто-то знает номер и пробовал войти (его остановил пароль 2FA
  или он не дождался кода). Это предупреждение, а не тревога угона.
* **Код входа на my.telegram.org** — там можно удалить аккаунт целиком.
* **Сброс пароля 2FA** (account.getPassword → pending_reset_date). Угонщик,
  которого не пускает пароль, просит сброс; Telegram ждёт 7 дней. Отменить
  можно из любого живого сеанса — то есть из нашего (account.declinePasswordReset).
  Автоотмена включена по умолчанию (`shield_configs.guard_reset`).
* **Пароль 2FA сменили или сняли не мы.** Пароль, который ставили мы, лежит
  зашифрованным (`twofa_enc`); проверяем его без смены
  (account.getPasswordSettings). Не подошёл — его сменил кто-то другой, и при
  следующем перелогине флот аккаунт потеряет.
* **Почта восстановления** появилась или сменилась — тот, кто её привязал,
  сбросит пароль без ожидания.

Ложные тревоги гасим, а не объясняем: коды, после которых появился наш,
доверенный или запасной сеанс, — наши; код, который владелец прочитал кнопкой
«Код входа» (`login_code_read_at`), — тоже его. Новые входы с чужих устройств
здесь не тревожат вовсе: о них уже сообщает дозор сеансов.

Всё, что найдено, ложится в `account_security_events` — журнал, который
показывают карточка аккаунта и экран «Защита».
"""
from __future__ import annotations

import asyncio
import hashlib
import html
import logging
import re
from datetime import datetime, timezone

log = logging.getLogger(__name__)

SERVICE_ID = 777000
_TIMEOUT = 15.0
_SERVICE_FETCH = 15
# Вход, начатый кодом, укладывается в эти минуты: код живёт недолго.
_CODE_TO_LOGIN_S = 15 * 60
# Код, прочитанный владельцем, мог прийти за столько до чтения.
_CODE_BEFORE_READ_S = 15 * 60
# Первый проход по аккаунту: старее этого служебные сообщения не тревожат —
# иначе включение дозора выдало бы владельцу всю историю кодов разом.
_BASELINE_FRESH_S = 2 * 3600
# Пароль, который не подошёл, не проверяем снова неделю: повторные неверные
# проверки Telegram считает перебором.
_MISMATCH_RECHECK_S = 7 * 86400

KIND_LOGIN_ATTEMPT = "login_attempt"
KIND_WEB_LOGIN = "web_login"
KIND_DELETE_REQUEST = "delete_request"
KIND_RESET_REQUESTED = "reset_requested"
KIND_RESET_DECLINED = "reset_declined"
KIND_PASSWORD_CHANGED = "password_changed"
KIND_PASSWORD_REMOVED = "password_removed"
KIND_RECOVERY_EMAIL = "recovery_email"

# Что видит владелец в журнале — по-русски, коротко.
KIND_TITLES = {
    KIND_LOGIN_ATTEMPT: "Кто-то пытался войти по коду",
    KIND_WEB_LOGIN: "Запрошен вход на my.telegram.org",
    KIND_DELETE_REQUEST: "Запрошено удаление аккаунта",
    KIND_RESET_REQUESTED: "Запрошен сброс пароля 2FA",
    KIND_RESET_DECLINED: "Сброс пароля 2FA отменён",
    KIND_PASSWORD_CHANGED: "Пароль 2FA сменили не мы",
    KIND_PASSWORD_REMOVED: "Пароль 2FA сняли",
    KIND_RECOVERY_EMAIL: "Привязана почта восстановления",
}
# Насколько это срочно: от этого зависит значок и место в журнале.
CRITICAL_KINDS = frozenset({KIND_WEB_LOGIN, KIND_DELETE_REQUEST, KIND_RESET_REQUESTED,
                            KIND_PASSWORD_CHANGED, KIND_PASSWORD_REMOVED,
                            KIND_RECOVERY_EMAIL})


# ── Чистое ядро ──────────────────────────────────────────────────────────────

_RE_WEB = re.compile(r"my\.telegram\.org", re.IGNORECASE)
_RE_DELETE = re.compile(
    r"(delete|deletion|удал\w*)[^.\n]{0,40}(account|аккаунт|учётн|учетн)"
    r"|(account|аккаунт)[^.\n]{0,40}(delet|удал)",
    re.IGNORECASE)
_RE_CODE_WORD = re.compile(r"(login code|код[а-я]*\s+(для\s+)?(входа|подтверждения)|"
                           r"\bcode\b|\bкод\b)", re.IGNORECASE)
_RE_DIGITS = re.compile(r"\b\d{5,6}\b")


def classify_service_message(text: str | None) -> str | None:
    """Что это за сообщение Telegram из чата 777000. None — не про безопасность.

    Тексты Telegram меняет и переводит, поэтому разбор по смыслу, а не по
    дословной фразе: важнее не пропустить код, чем угадать формулировку.
    Новые входы («New login») сюда не попадают — их показывает дозор сеансов.
    """
    t = str(text or "")
    if not t.strip():
        return None
    if _RE_WEB.search(t):
        return KIND_WEB_LOGIN
    if _RE_DELETE.search(t):
        return KIND_DELETE_REQUEST
    if _RE_CODE_WORD.search(t) and _RE_DIGITS.search(t):
        return KIND_LOGIN_ATTEMPT
    return None


def _ts(v) -> int:
    try:
        if v is None:
            return 0
        return int(v.timestamp()) if hasattr(v, "timestamp") else int(v)
    except (TypeError, ValueError, OverflowError, OSError):
        return 0


def code_was_used(msg_ts: int, login_times: list[int] | set[int]) -> bool:
    """После кода появился сеанс (любой: наш, доверенный, чужой) — код не
    «попытка». Чужой вход тревожит дозор сеансов, свой не тревожит вовсе."""
    if not msg_ts:
        return False
    return any(0 <= int(t) - int(msg_ts) <= _CODE_TO_LOGIN_S for t in login_times if t)


def code_read_by_owner(msg_ts: int, read_at: int) -> bool:
    """Код прочитан владельцем кнопкой «Код входа» — значит, входил он."""
    if not msg_ts or not read_at:
        return False
    return 0 <= int(read_at) - int(msg_ts) <= _CODE_BEFORE_READ_S


def pick_service_events(msgs: list[dict], *, seen_id: int | None, login_times,
                        code_read_at: int = 0, now: float | None = None) -> list[dict]:
    """Служебные сообщения → события. msgs: [{id, date, text}].

    seen_id — последнее уже разобранное сообщение; None — аккаунт ещё не
    разбирали: тогда тревожат только свежие (последние 2 часа).
    """
    now = float(now if now is not None else datetime.now(timezone.utc).timestamp())
    out: list[dict] = []
    for m in sorted(msgs, key=lambda x: int(x.get("id") or 0)):
        mid = int(m.get("id") or 0)
        if seen_id is not None and mid <= int(seen_id):
            continue
        ts = int(m.get("date") or 0)
        if seen_id is None and now - ts > _BASELINE_FRESH_S:
            continue
        kind = classify_service_message(m.get("text"))
        if kind is None:
            continue
        if kind == KIND_LOGIN_ATTEMPT and (code_was_used(ts, login_times)
                                           or code_read_by_owner(ts, code_read_at)):
            continue
        out.append({"kind": kind, "at": ts, "msg_id": mid})
    return out


def email_fp(email: str | None) -> str | None:
    if not email:
        return None
    return hashlib.sha256(str(email).strip().lower().encode()).hexdigest()[:32]


def mask_email(email: str | None) -> str:
    """a***@g***.com — достаточно, чтобы узнать свою почту, и не раскрыть чужую."""
    e = str(email or "").strip()
    if "@" not in e:
        return "***"
    user, dom = e.split("@", 1)
    head, _, tail = dom.partition(".")
    return f"{user[:1]}***@{head[:1]}***{('.' + tail) if tail else ''}"


def password_events(acc: dict, pw: dict | None, *, settings_email: str | None = None,
                    verified: bool | None = None) -> list[dict]:
    """События по состоянию пароля 2FA.

    pw — факты account.getPassword: {has_password, pending_reset, email_unconfirmed}.
    verified — подошёл ли наш сохранённый пароль (None — не проверяли).
    settings_email — почта восстановления (account.getPasswordSettings), если
    проверка прошла.
    """
    if not pw:
        return []
    out: list[dict] = []
    has = pw.get("has_password")
    if pw.get("pending_reset"):
        out.append({"kind": KIND_RESET_REQUESTED, "until": int(pw["pending_reset"])})
    # Сняли: пароль был (мы знали его), а теперь его нет. Наш собственный
    # снятый пароль стирает запись twofa_enc, поэтому сюда не попадает.
    if has is False and acc.get("twofa_on") is True and acc.get("twofa_saved"):
        out.append({"kind": KIND_PASSWORD_REMOVED})
    if verified is False:
        out.append({"kind": KIND_PASSWORD_CHANGED})
    if verified is True:
        fp = email_fp(settings_email)
        if fp and fp != (acc.get("recovery_email_fp") or None):
            out.append({"kind": KIND_RECOVERY_EMAIL, "email": mask_email(settings_email)})
    if pw.get("email_unconfirmed"):
        out.append({"kind": KIND_RECOVERY_EMAIL, "email": str(pw["email_unconfirmed"])[:64],
                    "pending": True})
    return out


def _when(ts: int) -> str:
    if not ts:
        return "время неизвестно"
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
    except (TypeError, ValueError, OverflowError, OSError):
        return "время неизвестно"


def build_alert(label: str, ev: dict, *, twofa_on: bool | None, declined: bool = False) -> str:
    """Тревога владельцу по одному событию: что случилось и что делать."""
    k = ev["kind"]
    name = html.escape(label)
    never = ("<i>Ни Telegram, ни Infragram никогда не просят прислать код или "
             "пароль. Кто просит — мошенник.</i>")
    if k == KIND_LOGIN_ATTEMPT:
        lines = ["⚠️ <b>Кто-то пытался войти в аккаунт</b>", "",
                 f"Аккаунт: <b>{name}</b>",
                 f"Telegram прислал код входа {_when(ev.get('at', 0))}, но новый вход "
                 "не появился. Если входили не вы — кто-то знает номер и пробует "
                 "забрать аккаунт.", ""]
        if twofa_on is True:
            lines.append("Пароль 2FA стоит — без него по одному коду не войти.")
        else:
            lines.append("❗ <b>Пароля 2FA нет</b> — следующий код может стоить "
                         "аккаунта. Поставьте пароль кнопкой ниже.")
        return "\n".join(lines + ["", never])
    if k == KIND_WEB_LOGIN:
        return "\n".join([
            "🚨 <b>Вход на my.telegram.org</b>", "",
            f"Аккаунт: <b>{name}</b>",
            f"Telegram прислал код для my.telegram.org {_when(ev.get('at', 0))}. "
            "Там можно удалить аккаунт целиком. Если это не вы — закройте чужие "
            "сеансы и проверьте пароль 2FA.", "", never])
    if k == KIND_DELETE_REQUEST:
        return "\n".join([
            "🚨 <b>Запрошено удаление аккаунта</b>", "",
            f"Аккаунт: <b>{name}</b>",
            f"Служебное сообщение Telegram {_when(ev.get('at', 0))} говорит о запросе "
            "на удаление. Если это не вы — закройте чужие сеансы немедленно.", "", never])
    if k == KIND_RESET_REQUESTED:
        head = ("🛡 <b>Чужой сброс пароля 2FA отменён</b>" if declined
                else "🚨 <b>Кто-то сбрасывает пароль 2FA</b>")
        body = ("Infragram сам отменил сброс. Тот, кто его запросил, знает номер "
                "и получил код — закройте чужие сеансы." if declined else
                f"Сброс сработает {_when(ev.get('until', 0))}, и тогда пароль "
                "перестанет защищать аккаунт. Если сброс просили не вы — отмените "
                "его кнопкой ниже.")
        return "\n".join([head, "", f"Аккаунт: <b>{name}</b>", body])
    if k == KIND_PASSWORD_CHANGED:
        return "\n".join([
            "🚨 <b>Пароль 2FA сменили не через Infragram</b>", "",
            f"Аккаунт: <b>{name}</b>",
            "Сохранённый пароль больше не подходит. Если меняли не вы — аккаунт, "
            "скорее всего, угоняют: при следующем перелогине войти не получится. "
            "Закройте чужие сеансы, пока наш сеанс жив, и задайте пароль заново."])
    if k == KIND_PASSWORD_REMOVED:
        return "\n".join([
            "🚨 <b>С аккаунта сняли пароль 2FA</b>", "",
            f"Аккаунт: <b>{name}</b>",
            "Пароль снимали не через Infragram. Теперь аккаунт угоняется одним "
            "кодом. Поставьте пароль заново кнопкой ниже и закройте чужие сеансы."])
    if k == KIND_RECOVERY_EMAIL:
        what = ("ждёт подтверждения" if ev.get("pending") else "привязана")
        return "\n".join([
            "⚠️ <b>Почта восстановления пароля</b>", "",
            f"Аккаунт: <b>{name}</b>",
            f"Почта <b>{html.escape(str(ev.get('email') or ''))}</b> {what}. Через неё "
            "пароль 2FA сбрасывается без ожидания. Если почту привязывали не вы — "
            "закройте чужие сеансы и смените пароль."])
    return f"⚠️ Событие безопасности на аккаунте <b>{name}</b>"


# ── Соединение ───────────────────────────────────────────────────────────────

async def read_password(client) -> dict | None:
    """Факты account.getPassword. None — узнать не удалось."""
    from telethon.tl.functions.account import GetPasswordRequest
    try:
        res = await asyncio.wait_for(client(GetPasswordRequest()), timeout=_TIMEOUT)
    except Exception as exc:
        log.debug("account_alarm.read_password: %s", exc)
        return None
    has = getattr(res, "has_password", None)
    return {
        "has_password": bool(has) if isinstance(has, bool) else None,
        "pending_reset": _ts(getattr(res, "pending_reset_date", None)),
        "email_unconfirmed": str(getattr(res, "email_unconfirmed_pattern", "") or ""),
        "_raw": res,
    }


async def verify_password(client, raw, password: str) -> tuple[bool | None, str | None]:
    """Проверить пароль без смены. → (подошёл?, почта восстановления).

    (None, None) — проверить не удалось (сеть, нет данных): это не «не подошёл».
    """
    from telethon.password import compute_check
    from telethon.tl.functions.account import GetPasswordSettingsRequest
    try:
        check = compute_check(raw, password)
        res = await asyncio.wait_for(client(GetPasswordSettingsRequest(password=check)),
                                     timeout=_TIMEOUT)
    except Exception as exc:
        if type(exc).__name__ == "PasswordHashInvalidError":
            return False, None
        log.debug("account_alarm.verify_password: %s", exc)
        return None, None
    return True, (getattr(res, "email", None) or None)


async def read_service_messages(client, min_id: int | None) -> list[dict] | None:
    try:
        msgs = await asyncio.wait_for(
            client.get_messages(SERVICE_ID, limit=_SERVICE_FETCH, min_id=int(min_id or 0)),
            timeout=_TIMEOUT)
    except Exception as exc:
        log.debug("account_alarm.read_service_messages: %s", exc)
        return None
    out = []
    for m in msgs or []:
        out.append({"id": int(getattr(m, "id", 0) or 0),
                    "date": _ts(getattr(m, "date", None)),
                    "text": str(getattr(m, "message", "") or "")[:600]})
    return out


async def decline_reset(client) -> bool:
    """Отменить запрошенный сброс пароля 2FA. Нет сброса — тоже успех."""
    from telethon.tl.functions.account import DeclinePasswordResetRequest
    try:
        await asyncio.wait_for(client(DeclinePasswordResetRequest()), timeout=_TIMEOUT)
        return True
    except Exception as exc:
        if type(exc).__name__ == "ResetRequestMissingError":
            return True
        log.warning("account_alarm.decline_reset: %s", exc)
        return False


def login_times(profile: dict, acc: dict, all_auths: list[dict] | None) -> list[int]:
    """Когда в аккаунт входили (наш сеанс + все остальные из списка)."""
    out = [int(profile.get("own_session_created") or 0)]
    for a in all_auths or []:
        out.append(int(a.get("created") or 0))
    return [t for t in out if t]


async def inspect(client, acc: dict, profile: dict, cfg, all_auths: list[dict] | None) -> dict:
    """Снять сигналы на живом соединении. Не бросает.

    → {"events": [...], "seen_id": int|None, "declined": bool,
       "email_fp": str|None, "mismatch": bool|None, "twofa_on": bool|None}
    """
    out: dict = {"events": [], "seen_id": None, "declined": False,
                 "email_fp": None, "mismatch": None, "twofa_on": None}
    try:
        seen = acc.get("service_seen_msg_id")
        seen = int(seen) if seen is not None else None
        read_at = _ts(acc.get("login_code_read_at"))
        msgs = await read_service_messages(client, seen)
        if msgs is not None:
            out["events"] += pick_service_events(
                msgs, seen_id=seen, login_times=login_times(profile, acc, all_auths),
                code_read_at=read_at)
            ids = [m["id"] for m in msgs if m.get("id")]
            # Пустой чат — тоже «разобрано»: 0, и следующие сообщения уже не
            # считаются историей первого прохода.
            out["seen_id"] = max(ids + [seen or 0])

        pw = await read_password(client)
        if pw is None:
            return out
        out["pw_read"] = True
        out["twofa_on"] = pw.get("has_password")
        verified, email = None, None
        mismatch_at = _ts(acc.get("twofa_mismatch_at"))
        recently_bad = mismatch_at and (datetime.now(timezone.utc).timestamp() - mismatch_at
                                        < _MISMATCH_RECHECK_S)
        if pw.get("has_password") and acc.get("twofa_enc") and not recently_bad:
            from services.token_vault import decrypt_token
            secret = decrypt_token(acc["twofa_enc"]) or ""
            if secret:
                verified, email = await verify_password(client, pw["_raw"], secret)
        out["mismatch"] = None if verified is None else (verified is False)
        if verified is True:
            out["email_fp"] = email_fp(email)
        evs = password_events(acc, pw, settings_email=email, verified=verified)
        if (any(e["kind"] == KIND_RESET_REQUESTED for e in evs)
                and cfg is not None and getattr(cfg, "guard_reset", True)
                and int(acc.get("id") or 0) not in {
                    int(x) for x in (getattr(cfg, "exempt_account_ids", None) or [])}):
            out["declined"] = await decline_reset(client)
        out["events"] += evs
    except Exception as exc:
        log.warning("account_alarm.inspect acc=%s: %s", acc.get("id"), exc)
    return out


# ── Запись и тревоги ─────────────────────────────────────────────────────────

def acc_label(acc: dict) -> str:
    return (f"@{acc['username']}" if acc.get("username")
            else (acc.get("first_name") or acc.get("phone") or f"#{acc.get('id')}"))


def alert_keyboard(acc_id: int, kind: str, *, twofa_on: bool | None, declined: bool):
    """Кнопки под тревогой: одно нажатие — одно защитное действие."""
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    from bot.callbacks import AccSecCb
    kb = InlineKeyboardBuilder()
    if kind == KIND_RESET_REQUESTED and not declined:
        kb.button(text="🛡 Отменить сброс пароля",
                  callback_data=AccSecCb(action="decline", account_id=acc_id))
    if kind in (KIND_PASSWORD_REMOVED,) or (kind == KIND_LOGIN_ATTEMPT and twofa_on is not True):
        kb.button(text="🔐 Поставить пароль 2FA",
                  callback_data=AccSecCb(action="twofa", account_id=acc_id))
    if kind != KIND_LOGIN_ATTEMPT or twofa_on is not True:
        kb.button(text="🚪 Закрыть чужие сеансы",
                  callback_data=AccSecCb(action="close", account_id=acc_id))
    kb.adjust(1)
    return kb.as_markup()


async def record(pool, acc: dict, found: dict, bot=None) -> int:
    """Записать отметки и события, отправить тревоги. → сколько событий. Не бросает."""
    try:
        acc_id, owner = int(acc["id"]), int(acc["owner_id"])
    except (KeyError, TypeError, ValueError):
        return 0
    events = list(found.get("events") or [])
    reset_until = None
    for e in events:
        if e["kind"] == KIND_RESET_REQUESTED and not found.get("declined"):
            reset_until = datetime.fromtimestamp(int(e["until"]), tz=timezone.utc)
    try:
        await pool.execute(
            """UPDATE tg_accounts SET
                 service_seen_msg_id = COALESCE($3, service_seen_msg_id),
                 twofa_reset_until = CASE WHEN $7::boolean THEN $4 ELSE twofa_reset_until END,
                 twofa_mismatch_at = CASE WHEN $5::boolean IS TRUE THEN now()
                                          WHEN $5::boolean IS FALSE THEN NULL
                                          ELSE twofa_mismatch_at END,
                 recovery_email_fp = COALESCE($6, recovery_email_fp)
               WHERE id=$1 AND owner_id=$2""",
            acc_id, owner, found.get("seen_id"), reset_until, found.get("mismatch"),
            found.get("email_fp"), bool(found.get("pw_read")))
    except Exception as exc:
        # Без отметки «разобрано» тревога повторится на следующем проходе —
        # не шлём её сейчас.
        log.warning("account_alarm.record: отметки acc=%d не записались: %s", acc_id, exc)
        return 0
    if not events:
        return 0
    from database import db
    label = acc_label(acc)
    twofa_on = found.get("twofa_on")
    for e in events:
        kind = e["kind"]
        declined = kind == KIND_RESET_REQUESTED and bool(found.get("declined"))
        detail = {k: v for k, v in e.items() if k != "kind"}
        await add_event(pool, owner, acc_id, KIND_RESET_DECLINED if declined else kind, detail)
        if bot is None:
            continue
        key = f"accsec:{acc_id}:{kind}:{e.get('msg_id') or e.get('until') or ''}"
        try:
            if not await db.notify_dedup_ok(pool, owner, key, 7 * 86400):
                continue
            delivered = await db.notify_if_enabled(
                pool, bot, owner, "restriction",
                build_alert(label, e, twofa_on=twofa_on, declined=declined),
                reply_markup=alert_keyboard(acc_id, kind, twofa_on=twofa_on, declined=declined),
                dedup_key=key)
            if delivered is False:
                await db.notify_dedup_forget(pool, owner, key)
        except Exception as exc:
            log.warning("account_alarm: тревога acc=%d kind=%s не ушла: %s", acc_id, kind, exc)
    return len(events)


async def add_event(pool, owner_id: int, account_id: int | None, kind: str,
                    detail: dict | None = None) -> None:
    """Запись в журнал безопасности (fail-soft)."""
    import json
    try:
        await pool.execute(
            "INSERT INTO account_security_events(owner_id, account_id, kind, detail) "
            "VALUES($1, $2, $3, $4)",
            int(owner_id), account_id, kind, json.dumps(detail or {}, ensure_ascii=False))
    except Exception as exc:
        log.warning("account_alarm.add_event owner=%s kind=%s: %s", owner_id, kind, exc)


def describe_event(kind: str, detail: dict | None) -> str:
    d = detail or {}
    title = KIND_TITLES.get(kind, kind)
    if kind == KIND_RESET_REQUESTED and d.get("until"):
        return f"{title} — сработает {_when(int(d['until']))}"
    if kind == KIND_RECOVERY_EMAIL and d.get("email"):
        return f"{title}: {d['email']}"
    return title


async def recent_events(pool, owner_id: int, account_id: int | None = None,
                        limit: int = 30) -> list[dict]:
    """Журнал безопасности для экрана: свежие сверху, с подписью по-русски."""
    import json
    try:
        rows = await pool.fetch(
            """SELECT e.id, e.account_id, e.kind, e.detail, e.created_at,
                      a.username, a.first_name, a.phone
               FROM account_security_events e
               LEFT JOIN tg_accounts a ON a.id = e.account_id AND a.owner_id = e.owner_id
               WHERE e.owner_id=$1 AND ($2::bigint IS NULL OR e.account_id=$2)
               ORDER BY e.created_at DESC LIMIT $3""",
            int(owner_id), account_id, int(limit))
    except Exception as exc:
        log.warning("account_alarm.recent_events owner=%s: %s", owner_id, exc)
        return []
    out = []
    for r in rows:
        try:
            detail = json.loads(r["detail"]) if isinstance(r["detail"], str) else (r["detail"] or {})
        except (TypeError, ValueError):
            detail = {}
        out.append({
            "id": int(r["id"]),
            "account_id": r["account_id"],
            "account": acc_label(dict(r)) if r["account_id"] else "",
            "kind": r["kind"],
            "critical": r["kind"] in CRITICAL_KINDS,
            "text": describe_event(r["kind"], detail),
            "at": r["created_at"].isoformat() if r["created_at"] else None,
        })
    return out


# ── Действия по кнопке ───────────────────────────────────────────────────────

async def _with_client(pool, owner_id: int, account_id: int, fn) -> dict:
    """Открыть соединение аккаунта владельца и выполнить fn(client, acc).

    Аккаунт берётся общей выборкой для Telethon (`get_account_for_telethon`):
    в ней релей и прокси аккаунта. Своя выборка без релея пошла бы с другого
    адреса, чем операции, — это AUTH_KEY_DUPLICATED.
    """
    from database import db
    from services import profile_setter_engine as pse
    acc = await db.get_account_for_telethon(pool, int(account_id), int(owner_id))
    if not acc or not acc.get("session_str"):
        return {"ok": False, "error": "Аккаунт недоступен"}
    try:
        client = await pse._connect(acc["session_str"], {**acc, "_low_risk": True})
    except pse.ConnectFailed as exc:
        return {"ok": False, "error": str(exc)}
    try:
        return await fn(client, acc)
    except Exception as exc:
        log.warning("account_alarm: действие acc=%s: %s", account_id, exc)
        return {"ok": False, "error": "Telegram не ответил — попробуйте ещё раз"}
    finally:
        try:
            await pse._safe_disconnect(client)
        except Exception as exc:
            log.debug("account_alarm: disconnect acc=%s: %s", account_id, exc)


async def decline_reset_now(pool, owner_id: int, account_id: int) -> dict:
    """Отменить сброс пароля 2FA по кнопке владельца. → {ok, error}."""
    async def _do(client, acc):
        if not await decline_reset(client):
            return {"ok": False, "error": "Telegram не принял отмену — попробуйте ещё раз"}
        return {"ok": True, "error": None}

    res = await _with_client(pool, owner_id, account_id, _do)
    if res.get("ok"):
        try:
            await pool.execute(
                "UPDATE tg_accounts SET twofa_reset_until=NULL WHERE id=$1 AND owner_id=$2",
                int(account_id), int(owner_id))
        except Exception as exc:
            log.warning("account_alarm.decline_reset_now: отметка acc=%s: %s", account_id, exc)
        await add_event(pool, owner_id, int(account_id), KIND_RESET_DECLINED, {"by": "owner"})
    return res


async def set_twofa_now(pool, owner_id: int, account_id: int) -> dict:
    """Поставить свой пароль 2FA по кнопке (пароль сохраняется до установки)."""
    from services import account_manager as am
    from services import session_watch as sw

    async def _do(client, acc):
        if await am.twofa_enabled(client) is True:
            return {"ok": True, "error": None, "already": True}
        why = await sw._set_twofa(pool, client, int(owner_id), int(account_id))
        if why is not None:
            return {"ok": False, "error": why}
        await pool.execute("UPDATE tg_accounts SET twofa_on=TRUE WHERE id=$1 AND owner_id=$2",
                           int(account_id), int(owner_id))
        return {"ok": True, "error": None}

    return await _with_client(pool, owner_id, account_id, _do)


async def close_foreign_now(pool, owner_id: int, account_id: int) -> dict:
    """Закрыть чужие сеансы по кнопке — кроме доверенных («это я») и запасного ключа.

    «Закрыть все» из оформления закрыло бы и телефон владельца; здесь закрываем
    ровно то, что дозор считает чужим.
    """
    from services import account_manager as am
    from services import session_watch as sw

    async def _do(client, acc):
        row = await pool.fetchrow(
            "SELECT trusted_session_hashes, reserve_auth_hash FROM tg_accounts "
            "WHERE id=$1 AND owner_id=$2", int(account_id), int(owner_id))
        meta = dict(row) if row else {}
        snap = await am.authorizations_snapshot(client)
        if snap is None:
            return {"ok": False, "error": "Не удалось получить список сеансов"}
        foreign, own_created = snap
        if not sw.session_old_enough(own_created):
            return {"ok": False, "error": "Сеанс Infragram на этом аккаунте моложе суток — "
                                          "Telegram пока не даёт закрывать чужие сеансы"}
        reserve = int(meta.get("reserve_auth_hash") or 0)
        targets = [f for f in sw.drop_trusted(foreign, meta) if int(f["hash"]) != reserve]
        closed = 0
        for f in targets:
            try:
                await sw._close_one(client, int(f["hash"]))
                closed += 1
            except Exception as exc:
                log.warning("account_alarm.close_foreign_now acc=%s: %s", account_id, exc)
        if closed == len(targets):
            await sw.forget(pool, int(owner_id), int(account_id))
        return {"ok": closed == len(targets), "closed": closed, "total": len(targets),
                "error": None if closed == len(targets) else
                f"Закрыто {closed} из {len(targets)} — повторите позже"}

    return await _with_client(pool, owner_id, account_id, _do)


async def note_code_read(pool, owner_id: int, account_id: int) -> None:
    """Владелец прочитал код входа кнопкой — этот код его, не тревожим."""
    try:
        await pool.execute(
            "UPDATE tg_accounts SET login_code_read_at=now() WHERE id=$1 AND owner_id=$2",
            int(account_id), int(owner_id))
    except Exception as exc:
        log.warning("account_alarm.note_code_read acc=%s: %s", account_id, exc)
