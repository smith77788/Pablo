"""Код Infragram владельца: вторая стена перед сливом данных.

Волна 3 плана «Защита аккаунтов». Вход в мини-апп подтверждает только Telegram
(волна 2 журналирует входы и даёт их завершить), но тот, кто открыл Telegram
владельца, войдёт снова. Настоящая вторая стена — свой код, который знает
только владелец и которого нет у угонщика его Telegram.

По решению владельца продукта (карточка в треде 21.11.2026) код — ПО ЖЕЛАНИЮ:
пока он не поставлен, ничего не меняется, а на экране «Защита» висит подсказка
его включить. Когда код поставлен, он требуется перед опасным действием —
тем, что выносит данные наружу:

* экспорт строки сессии аккаунта (полный доступ к аккаунту),
* показ сохранённых паролей 2FA,
* удаление аккаунта,
* удаление всех данных владельца.

Код проверяется на СЕАНС (services/owner_sessions): подтверждение живёт
``CONFIRM_MINUTES`` минут и только у того сеанса, что его ввёл, — чужой сеанс
угонщика им не воспользуется. Пять промахов подряд запирают ввод на
``LOCK_MINUTES`` минут (перебор четырёхзначного кода иначе — минуты).

И отдельно, ДЛЯ ВСЕХ, а не только с кодом: каждое такое действие шлёт
владельцу сигнал в бот («у вас показали пароли 2FA…»). Код можно не ставить,
но увести данные молча нельзя — владелец узнает и успеет завершить сеанс.

Граница честно: код — защита панели Infragram, а не самого Telegram. Если
угнали Telegram, закрывать сеансы надо и там (об этом говорит тревога волны 2).
"""
from __future__ import annotations

import hashlib
import hmac
import html
import logging
import re
import secrets
from datetime import datetime, timezone

log = logging.getLogger(__name__)

# Пять промахов — замок на 15 минут (четырёхзначный код иначе перебирается за
# минуты). Подтверждение действует 10 минут на том же сеансе.
MAX_FAILS = 5
LOCK_MINUTES = 15
CONFIRM_MINUTES = 10

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 16384, 8, 1

# Опасные действия: ключ → как назвать его владельцу в сигнале.
DANGEROUS = {
    "export_session": "экспорт строки сессии аккаунта",
    "reveal_twofa": "показ сохранённых паролей 2FA",
    "account_delete": "удаление аккаунта",
    "data_delete": "удаление всех данных в Infragram",
    "accounts_export": "выгрузку списка аккаунтов",
}


# ── Код: хеш и нормализация (чистое ядро) ────────────────────────────────────

def normalize_pin(raw) -> str | None:
    """Код — 4–8 цифр. Пробелы и дефисы режем, остальное — отказ (None)."""
    s = re.sub(r"[\s-]", "", str(raw or ""))
    return s if re.fullmatch(r"\d{4,8}", s) else None


def hash_pin(pin: str, salt: bytes | None = None) -> str:
    """`scrypt$<salt_hex>$<hash_hex>` — соль своя у каждого кода."""
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.scrypt(pin.encode("utf-8"), salt=salt,
                        n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32)
    return f"scrypt${salt.hex()}${dk.hex()}"


def verify_pin(pin: str, stored: str | None) -> bool:
    """Сверка в постоянное время. Пустой код/хеш — не совпадение."""
    if not pin or not stored:
        return False
    try:
        algo, salt_hex, want_hex = stored.split("$", 2)
    except ValueError:
        return False
    if algo != "scrypt":
        return False
    try:
        got = hashlib.scrypt(pin.encode("utf-8"), salt=bytes.fromhex(salt_hex),
                             n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32)
    except (ValueError, MemoryError):
        return False
    return hmac.compare_digest(got.hex(), want_hex)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def is_locked(locked_until, now=None) -> bool:
    now = now or _now()
    try:
        return bool(locked_until) and locked_until > now
    except TypeError:
        return False


def build_drain_alert(kind: str, detail: str | None, when=None) -> str:
    """Сигнал владельцу об опасном действии (для ВСЕХ, не только с кодом)."""
    what = DANGEROUS.get(kind, kind)
    lines = ["🧯 <b>Важное действие в Infragram</b>", "",
             f"Только что: <b>{html.escape(what)}</b>."]
    if detail:
        lines.append(html.escape(str(detail)[:200]))
    lines += ["",
              "Если это вы — всё в порядке. Если нет — завершите чужой вход: "
              "<b>Настройки → Мои входы</b>, и поставьте код Infragram, чтобы "
              "такое спрашивало код."]
    return "\n".join(lines)


# ── База: состояние кода владельца ───────────────────────────────────────────

async def get_state(pool, owner_id: int) -> dict:
    """{has_pin, failed_attempts, locked_until} — не бросает."""
    try:
        row = await pool.fetchrow(
            "SELECT (pin_hash IS NOT NULL) AS has_pin, failed_attempts, locked_until "
            "FROM owner_guard WHERE owner_id=$1", int(owner_id))
    except Exception as exc:
        log.warning("owner_guard.get_state owner=%s: %s", owner_id, exc)
        return {"has_pin": False, "failed_attempts": 0, "locked_until": None}
    if not row:
        return {"has_pin": False, "failed_attempts": 0, "locked_until": None}
    return dict(row)


async def has_pin(pool, owner_id: int) -> bool:
    return bool((await get_state(pool, owner_id)).get("has_pin"))


async def set_pin(pool, owner_id: int, new_pin: str, *,
                  current_pin: str | None = None) -> dict:
    """Поставить или сменить код. Смена требует текущий код.

    → {ok} либо {ok: False, error}. Постановка первого кода текущего не просит.
    """
    npin = normalize_pin(new_pin)
    if not npin:
        return {"ok": False, "error": "Код — от 4 до 8 цифр"}
    st = await get_state(pool, owner_id)
    if st.get("has_pin"):
        if is_locked(st.get("locked_until")):
            return {"ok": False, "error": "Слишком много попыток — попробуйте позже"}
        row = await pool.fetchrow("SELECT pin_hash FROM owner_guard WHERE owner_id=$1",
                                  int(owner_id))
        if not verify_pin(normalize_pin(current_pin) or "", row and row["pin_hash"]):
            await _bump_fail(pool, owner_id)
            return {"ok": False, "error": "Текущий код неверный"}
    try:
        await pool.execute(
            """INSERT INTO owner_guard(owner_id, pin_hash, failed_attempts,
                                       locked_until, updated_at)
               VALUES($1, $2, 0, NULL, now())
               ON CONFLICT (owner_id) DO UPDATE
                 SET pin_hash=$2, failed_attempts=0, locked_until=NULL, updated_at=now()""",
            int(owner_id), hash_pin(npin))
    except Exception as exc:
        log.warning("owner_guard.set_pin owner=%s: %s", owner_id, exc)
        return {"ok": False, "error": "Не удалось сохранить код"}
    return {"ok": True}


async def remove_pin(pool, owner_id: int, current_pin: str) -> dict:
    """Снять код — нужен текущий код (иначе угонщик снял бы его одним запросом)."""
    st = await get_state(pool, owner_id)
    if not st.get("has_pin"):
        return {"ok": True}
    if is_locked(st.get("locked_until")):
        return {"ok": False, "error": "Слишком много попыток — попробуйте позже"}
    row = await pool.fetchrow("SELECT pin_hash FROM owner_guard WHERE owner_id=$1",
                              int(owner_id))
    if not verify_pin(normalize_pin(current_pin) or "", row and row["pin_hash"]):
        await _bump_fail(pool, owner_id)
        return {"ok": False, "error": "Код неверный"}
    try:
        await pool.execute(
            "UPDATE owner_guard SET pin_hash=NULL, failed_attempts=0, "
            "locked_until=NULL, updated_at=now() WHERE owner_id=$1", int(owner_id))
    except Exception as exc:
        log.warning("owner_guard.remove_pin owner=%s: %s", owner_id, exc)
        return {"ok": False, "error": "Не удалось снять код"}
    return {"ok": True}


async def _bump_fail(pool, owner_id: int) -> datetime | None:
    """Промах: +1 к счётчику, на пятом — замок. → когда отопрёт (или None)."""
    try:
        row = await pool.fetchrow(
            """UPDATE owner_guard
                  SET failed_attempts = failed_attempts + 1,
                      locked_until = CASE WHEN failed_attempts + 1 >= $2
                        THEN now() + make_interval(mins => $3) ELSE locked_until END
                WHERE owner_id=$1
                RETURNING locked_until""",
            int(owner_id), MAX_FAILS, LOCK_MINUTES)
    except Exception as exc:
        log.warning("owner_guard._bump_fail owner=%s: %s", owner_id, exc)
        return None
    return row["locked_until"] if row else None


async def verify_and_confirm(pool, owner_id: int, sid: str | None, pin: str) -> dict:
    """Проверить код и, если верен, открыть подтверждение этому сеансу.

    → {ok} | {ok: False, error, locked_until?}. Подтверждение пишется в
    owner_sessions.pin_ok_until у строки сеанса — у чужого сеанса его нет.
    """
    st = await get_state(pool, owner_id)
    if not st.get("has_pin"):
        return {"ok": True}            # кода нет — подтверждать нечего
    if is_locked(st.get("locked_until")):
        return {"ok": False, "error": "Слишком много попыток — попробуйте позже",
                "locked_until": st.get("locked_until")}
    npin = normalize_pin(pin)
    row = await pool.fetchrow("SELECT pin_hash FROM owner_guard WHERE owner_id=$1",
                              int(owner_id))
    if not npin or not verify_pin(npin, row and row["pin_hash"]):
        locked = await _bump_fail(pool, owner_id)
        left = max(0, MAX_FAILS - int(st.get("failed_attempts") or 0) - 1)
        out = {"ok": False, "error": "Код неверный"}
        if locked and is_locked(locked):
            out["error"] = "Слишком много попыток — попробуйте позже"
            out["locked_until"] = locked
        else:
            out["attempts_left"] = left
        return out
    try:
        await pool.execute(
            "UPDATE owner_guard SET failed_attempts=0, locked_until=NULL WHERE owner_id=$1",
            int(owner_id))
        if sid:
            await pool.execute(
                "UPDATE owner_sessions SET pin_ok_until = now() + make_interval(mins => $2) "
                "WHERE owner_id=$1 AND sid=$3", int(owner_id), CONFIRM_MINUTES, sid)
    except Exception as exc:
        log.warning("owner_guard.verify_and_confirm owner=%s: %s", owner_id, exc)
        return {"ok": False, "error": "Не удалось подтвердить — попробуйте снова"}
    return {"ok": True}


async def guard_dangerous(pool, owner_id: int, sid: str | None) -> dict:
    """Пускать ли опасное действие. → {allowed, need_pin, locked_until?}.

    Кода нет — пускаем (по желанию): {allowed: True, need_pin: False}.
    Код есть и сеанс подтверждён в окне — пускаем. Иначе — need_pin.
    """
    st = await get_state(pool, owner_id)
    if not st.get("has_pin"):
        return {"allowed": True, "need_pin": False}
    if is_locked(st.get("locked_until")):
        return {"allowed": False, "need_pin": True, "locked_until": st.get("locked_until")}
    ok = False
    if sid:
        try:
            ok = bool(await pool.fetchval(
                "SELECT pin_ok_until > now() FROM owner_sessions "
                "WHERE owner_id=$1 AND sid=$2", int(owner_id), sid))
        except Exception as exc:
            log.warning("owner_guard.guard_dangerous owner=%s: %s", owner_id, exc)
    return {"allowed": ok, "need_pin": not ok}


async def pin_suggested(pool, owner_id: int) -> bool:
    """Стоит ли подсказать поставить код: есть аккаунты, кода ещё нет."""
    try:
        if await has_pin(pool, owner_id):
            return False
        n = await pool.fetchval(
            "SELECT 1 FROM tg_accounts WHERE owner_id=$1 LIMIT 1", int(owner_id))
        return bool(n)
    except Exception as exc:
        log.warning("owner_guard.pin_suggested owner=%s: %s", owner_id, exc)
        return False


# ── Сигнал об опасном действии (для всех) ────────────────────────────────────

async def alert_drain(pool, owner_id: int, kind: str, detail: str | None = None) -> None:
    """Сигнал владельцу, что из Infragram что-то вынесли. Не бросает.

    Идёт всем, с кодом и без: код можно не ставить, но молча увести данные
    нельзя. Дедуп короткий — по виду действия, чтобы серия (экспорт десяти
    сессий) не завалила бота, но разные действия не глушили друг друга.
    """
    try:
        from aiogram import Bot
        from aiogram.client.default import DefaultBotProperties
        from aiogram.enums import ParseMode

        from config import BOT_TOKEN
        from database import db
    except Exception as exc:
        log.warning("owner_guard.alert_drain import owner=%s: %s", owner_id, exc)
        return
    bot = None
    try:
        bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        await db.notify_if_enabled(pool, bot, int(owner_id), "restriction",
                                   build_drain_alert(kind, detail),
                                   dedup_key=f"owner_drain:{owner_id}:{kind}")
    except Exception as exc:
        log.warning("owner_guard.alert_drain owner=%s: %s", owner_id, exc)
    finally:
        if bot is not None:
            try:
                await bot.session.close()
            except Exception as exc:
                log.debug("owner_guard.alert_drain close: %s", exc)
