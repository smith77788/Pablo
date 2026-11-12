"""Дозор чужих сеансов: кто ещё вошёл в аккаунт флота.

Аккаунт теряется не только баном. Продавец купленного аккаунта оставляет себе
сеанс и через сутки выкидывает наш; угонщик, получивший код, входит рядом;
старый вход из прошлой панели живёт месяцами. Всё это видно в
account.getAuthorizations задолго до того, как наша сессия умрёт, — а когда
она умерла, вернуть аккаунт уже нечем.

Фоновая проверка сессий (`account_manager._check_all_sessions`, раз в 6 часов
по всему флоту) снимает список чужих авторизаций тем же соединением и отдаёт
его сюда. Здесь:

* счётчик и отпечатки чужих сеансов ложатся в аккаунт (v340) — их видят
  карточка и дашборд;
* о НОВОМ сеансе владелец узнаёт сразу, с устройством, приложением, страной и
  датой входа, и с тем, что делать. Уже показанные сеансы тревогу не
  повторяют — иначе владелец через сутки перестанет её читать.

Закрыть чужие сеансы — существующая операция оформления «Закрыть чужие
сеансы» (`profile_setter_engine.close_other_sessions`); после неё счётчик
обнуляется.

Ограничение платформы: Telegram не даёт закрывать чужие сеансы из сеанса
моложе 24 часов (FRESH_RESET_AUTHORISATION_FORBIDDEN). Свежекупленный
аккаунт сутки уязвим, и обойти это нельзя ничем — только знать и успеть
закрыть, как только сутки прошли.
"""
from __future__ import annotations

import html
import logging
from datetime import datetime, timezone

log = logging.getLogger(__name__)

_MAX_LISTED = 3


def new_sessions(foreign: list[dict], known_hashes) -> list[dict]:
    """Сеансы, которых не было в прошлый раз."""
    known = {int(h) for h in (known_hashes or []) if h is not None}
    return [f for f in foreign if int(f.get("hash") or 0) not in known]


def _when(ts: int) -> str:
    if not ts:
        return "дата неизвестна"
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
    except (TypeError, ValueError, OverflowError, OSError):
        return "дата неизвестна"


def describe_session(s: dict) -> str:
    """Одна строка о сеансе — для человека, без внутренних полей."""
    what = " · ".join(x for x in (s.get("device"), s.get("app")) if x) or "неизвестное устройство"
    where = " · ".join(x for x in (s.get("country"), s.get("ip")) if x)
    out = what + (f" ({where})" if where else "")
    return f"{out}, вход {_when(int(s.get('created') or 0))}"


def build_alert(label: str, new: list[dict], total: int) -> str:
    lines = [
        "🚨 <b>В аккаунт вошли с другого устройства</b>",
        "",
        f"Аккаунт: <b>{html.escape(label)}</b>",
        f"Новых чужих сеансов: <b>{len(new)}</b> (всего чужих: {total})",
        "",
    ]
    for s in new[:_MAX_LISTED]:
        lines.append("• " + html.escape(describe_session(s)))
    if len(new) > _MAX_LISTED:
        lines.append(f"• …и ещё {len(new) - _MAX_LISTED}")
    lines += [
        "",
        "Если это не вы — закройте чужие сеансы: <b>Аккаунты → Профили → "
        "«Очистка и сеансы»</b>, и поставьте пароль 2FA. Иначе тот, кто "
        "вошёл, может выкинуть сеанс Infragram и забрать аккаунт.",
        "<i>Telegram разрешает закрывать чужие сеансы только из сеанса старше "
        "суток.</i>",
    ]
    return "\n".join(lines)


async def record(pool, acc: dict, foreign: list[dict] | None, bot=None) -> dict:
    """Записать чужие сеансы аккаунта и предупредить о новых. Не бросает.

    foreign=None — список узнать не удалось: ничего не трогаем (иначе сбой
    запроса обнулил бы счётчик и стёр отпечатки, и следующий же реальный
    сеанс снова считался бы «новым»).
    """
    if foreign is None:
        return {"skipped": True}
    try:
        acc_id = int(acc["id"])
    except (KeyError, TypeError, ValueError):
        return {"skipped": True}
    new = new_sessions(foreign, acc.get("foreign_session_hashes"))
    hashes = sorted({int(f["hash"]) for f in foreign if f.get("hash")})
    try:
        await pool.execute(
            "UPDATE tg_accounts SET foreign_sessions=$1, foreign_session_hashes=$2, "
            "sessions_checked_at=now() WHERE id=$3",
            len(foreign), hashes, acc_id)
    except Exception as exc:
        # Без записи отпечатков тревога повторилась бы на каждом проходе —
        # не шлём её, пока запись не пройдёт.
        log.warning("session_watch: запись acc=%d не прошла: %s", acc_id, exc)
        return {"foreign": len(foreign), "new": len(new), "written": False}
    if new and bot is not None and acc.get("owner_id"):
        label = (f"@{acc['username']}" if acc.get("username")
                 else (acc.get("first_name") or acc.get("phone") or f"#{acc_id}"))
        try:
            from database import db
            sig = hash(tuple(sorted(int(s["hash"]) for s in new))) & 0xFFFFFFFF
            key = f"foreign_sess:{acc_id}:{sig:x}"
            if await db.notify_dedup_ok(pool, int(acc["owner_id"]), key, 7 * 86400):
                delivered = await db.notify_if_enabled(
                    pool, bot, int(acc["owner_id"]), "restriction",
                    build_alert(str(label), new, len(foreign)), dedup_key=key)
                if delivered is False:
                    await db.notify_dedup_forget(pool, int(acc["owner_id"]), key)
        except Exception as exc:
            log.warning("session_watch: тревога acc=%d не ушла: %s", acc_id, exc)
    return {"foreign": len(foreign), "new": len(new), "written": True}


async def forget(pool, owner_id: int, account_id: int) -> None:
    """Чужие сеансы закрыты — обнулить счётчик (fail-soft)."""
    try:
        await pool.execute(
            "UPDATE tg_accounts SET foreign_sessions=0, foreign_session_hashes=NULL, "
            "sessions_checked_at=now() WHERE id=$1 AND owner_id=$2",
            account_id, owner_id)
    except Exception as exc:
        log.warning("session_watch.forget acc=%s: %s", account_id, exc)


_LOST_RU = {
    "session_expired": "сессия отозвана",
    "banned": "аккаунт заблокирован",
    "deactivated": "аккаунт удалён",
}


def build_lost_alert(label: str, status: str, foreign: int, twofa_saved: bool) -> str:
    lines = [
        "🔴 <b>Аккаунт выпал из флота</b>",
        "",
        f"Аккаунт: <b>{html.escape(label)}</b>",
        f"Что случилось: <b>{_LOST_RU.get(status, html.escape(status))}</b>",
    ]
    if foreign and status == "session_expired":
        lines += ["", f"⚠️ До этого на аккаунте были чужие сеансы ({foreign}) — "
                      "похоже на угон: тот, кто вошёл, выкинул сеанс Infragram."]
    if status == "session_expired":
        lines += ["", "Вернуть: войдите в аккаунт заново (Аккаунты → Добавить) "
                      "и закройте чужие сеансы."]
        if twofa_saved:
            lines.append("Пароль 2FA этого аккаунта сохранён — он в «Профили → Пароли 2FA».")
    return "\n".join(lines)


async def notify_lost(pool, acc: dict, status: str, bot=None) -> None:
    """Сообщить владельцу, что аккаунт выпал (fail-soft, раз в сутки на аккаунт)."""
    if bot is None or not acc.get("owner_id"):
        return
    try:
        from database import db
        owner = int(acc["owner_id"])
        key = f"sess_expired:{int(acc['id'])}"
        if not await db.notify_dedup_ok(pool, owner, key, 86400):
            return
        label = (f"@{acc['username']}" if acc.get("username")
                 else (acc.get("first_name") or acc.get("phone") or f"#{acc['id']}"))
        foreign = len(acc.get("foreign_session_hashes") or [])
        delivered = await db.notify_if_enabled(
            pool, bot, owner, "restriction",
            build_lost_alert(str(label), status, foreign, bool(acc.get("twofa_saved"))),
            dedup_key=key)
        if delivered is False:
            await db.notify_dedup_forget(pool, owner, key)
    except Exception as exc:
        log.warning("session_watch.notify_lost acc=%s: %s", acc.get("id"), exc)
