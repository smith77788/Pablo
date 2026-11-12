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

import asyncio
import html
import logging
import secrets
import time
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


async def record(pool, acc: dict, foreign: list[dict] | None, bot=None, *,
                 unsettled=None, twofa_on: bool | None = None,
                 own_created: int = 0) -> dict:
    """Записать чужие сеансы аккаунта и предупредить о новых. Не бросает.

    foreign=None — список узнать не удалось: ничего не трогаем (иначе сбой
    запроса обнулил бы счётчик и стёр отпечатки, и следующий же реальный
    сеанс снова считался бы «новым»).

    unsettled — отпечатки сеансов, которые автозащита закроет, но пока не
    может (наш сеанс моложе суток). Они не запоминаются как «уже показанные»:
    иначе на следующем проходе они перестали бы быть новыми, и автозащита их
    уже не тронула бы.

    twofa_on / own_created — факты того же соединения (есть ли пароль 2FA,
    когда создан наш сеанс); None/0 — не знаем, прежнее значение не трогаем.
    """
    if foreign is None:
        return {"skipped": True}
    try:
        acc_id = int(acc["id"])
    except (KeyError, TypeError, ValueError):
        return {"skipped": True}
    new = new_sessions(foreign, acc.get("foreign_session_hashes"))
    _hold = {int(h) for h in (unsettled or ())}
    hashes = sorted({int(f["hash"]) for f in foreign
                     if f.get("hash") and int(f["hash"]) not in _hold})
    own_at = None
    if own_created:
        try:
            own_at = datetime.fromtimestamp(int(own_created), tz=timezone.utc)
        except (TypeError, ValueError, OverflowError, OSError):
            own_at = None
    try:
        await pool.execute(
            "UPDATE tg_accounts SET foreign_sessions=$1, foreign_session_hashes=$2, "
            "sessions_checked_at=now(), twofa_on=COALESCE($4, twofa_on), "
            "own_session_at=COALESCE($5, own_session_at) WHERE id=$3",
            len(foreign), hashes, acc_id, twofa_on, own_at)
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


# ── Автозащита ───────────────────────────────────────────────────────────────
#
# Дозор выше только предупреждает, а угонщику, получившему код, хватает минут:
# он входит, ставит свой пароль и закрывает наш сеанс, пока владелец читает
# сообщение. Автозащита (включает владелец: «Щит → Автозащита») делает то же,
# что владелец сделал бы руками, — на том же соединении, что и обход флота:
#
# * закрывает НОВЫЕ чужие сеансы по одному (account.resetAuthorization), а не
#   все сразу: сеансы, которые владелец уже видел и оставил, — его решение;
# * ставит аккаунту без пароля 2FA свой случайный пароль; пароль сначала
#   ложится в базу (`db.remember_account_twofa`) и только потом ставится —
#   пароль, который никто не знает, хуже, чем никакого.
#
# Telegram разрешает и то и другое только из сеанса старше суток
# (FRESH_RESET_AUTHORISATION_FORBIDDEN). Пароль на свежем сеансе формально
# поставить можно, но смена пароля сразу после входа — признак угона для
# самой Telegram, поэтому тот же порог держим и для него.

_FRESH_S = 24 * 3600 + 15 * 60          # сутки и запас на расхождение часов
_TWOFA_PER_OWNER_SWEEP = 10             # паролей за проход на владельца
_GUARD_TIMEOUT = 20.0


def session_old_enough(own_created: int, now: float | None = None) -> bool:
    """Старше ли наш сеанс суток (по данным Telegram). 0 — не знаем: нет."""
    if not own_created:
        return False
    return (now if now is not None else time.time()) - int(own_created) >= _FRESH_S


async def _close_one(client, h: int) -> None:
    from telethon.tl.functions.account import ResetAuthorizationRequest
    await asyncio.wait_for(client(ResetAuthorizationRequest(hash=int(h))),
                           timeout=_GUARD_TIMEOUT)


async def _set_twofa(pool, client, owner: int, acc_id: int) -> str | None:
    """Поставить свой пароль 2FA. → None, если встал, иначе причина по-русски."""
    from database import db
    prev = None
    try:
        prev = await pool.fetchval(
            "SELECT twofa_enc FROM tg_accounts WHERE id=$1 AND owner_id=$2", acc_id, owner)
    except Exception as exc:
        log.warning("session_watch.guard: прежний пароль acc=%d не прочитан: %s", acc_id, exc)
        return "не удалось прочитать прежнюю запись пароля"
    pw = secrets.token_urlsafe(12)
    if not await db.remember_account_twofa(pool, owner, acc_id, pw):
        return "пароль не сохранился — ставить не стали"
    try:
        await asyncio.wait_for(client.edit_2fa(new_password=pw), timeout=_GUARD_TIMEOUT)
        return None
    except Exception as exc:
        # Ответ мог потеряться ПОСЛЕ того, как пароль встал: тогда запись
        # обязана остаться, иначе аккаунт с паролем, которого никто не знает.
        from services import account_manager as _am
        if await _am.twofa_enabled(client) is True:
            return None
        try:
            await pool.execute(
                "UPDATE tg_accounts SET twofa_enc=$1 WHERE id=$2 AND owner_id=$3",
                prev, acc_id, owner)
        except Exception as exc2:
            log.warning("session_watch.guard: откат записи пароля acc=%d: %s", acc_id, exc2)
        log.warning("session_watch.guard: 2FA acc=%d не встал: %s", acc_id, exc)
        return "Telegram не принял пароль"


async def guard(pool, client, acc: dict, profile: dict, cfg, budget: dict) -> dict:
    """Автозащита одного аккаунта на открытом соединении. Не бросает.

    Меняет profile: закрытые сеансы убираются из `authorizations`, после
    поставленного пароля `twofa_on` = True. Возвращает, что сделано:
    {"closed": [...], "unsettled": {hash, ...}, "twofa": "set"|"<причина>"|None}.
    budget — общий на проход обхода: {owner_id: паролей уже поставлено}.
    """
    out: dict = {"closed": [], "unsettled": set(), "twofa": None, "reserve": None}
    try:
        acc_id, owner = int(acc["id"]), int(acc["owner_id"])
    except (KeyError, TypeError, ValueError):
        return out
    if cfg is None or acc_id in {int(x) for x in (getattr(cfg, "exempt_account_ids", None) or [])}:
        return out
    old_enough = session_old_enough(int(profile.get("own_session_created") or 0))
    foreign = profile.get("authorizations")
    if getattr(cfg, "guard_sessions", False) and foreign:
        new = new_sessions(foreign, acc.get("foreign_session_hashes"))
        if new and not old_enough:
            out["unsettled"] = {int(s["hash"]) for s in new}
        for s in (new if old_enough else []):
            try:
                await _close_one(client, int(s["hash"]))
                out["closed"].append(s)
            except Exception as exc:
                log.warning("session_watch.guard: сеанс acc=%d не закрылся: %s", acc_id, exc)
        if out["closed"]:
            gone = {int(s["hash"]) for s in out["closed"]}
            profile["authorizations"] = [f for f in foreign if int(f["hash"]) not in gone]
    if (getattr(cfg, "guard_twofa", False) and profile.get("twofa_on") is False
            and old_enough and budget.get(owner, 0) < _TWOFA_PER_OWNER_SWEEP):
        budget[owner] = budget.get(owner, 0) + 1
        try:
            why = await _set_twofa(pool, client, owner, acc_id)
        except Exception as exc:
            log.warning("session_watch.guard: 2FA acc=%d: %s", acc_id, exc)
            why = "сбой при установке пароля"
        out["twofa"] = "set" if why is None else why
        if why is None:
            profile["twofa_on"] = True
    # Запасной ключ — после пароля: только что поставленный пароль уже в базе,
    # и новый сеанс войдёт с ним.
    if (getattr(cfg, "guard_reserve", False) and not acc.get("has_reserve") and old_enough
            and budget.get(("reserve", owner), 0) < _RESERVE_PER_OWNER_SWEEP):
        budget[("reserve", owner)] = budget.get(("reserve", owner), 0) + 1
        try:
            out["reserve"] = await _make_reserve(pool, client, acc, profile, owner, acc_id)
        except Exception as exc:
            log.warning("session_watch.guard: запасной ключ acc=%d: %s", acc_id, exc)
            out["reserve"] = "сбой при заведении ключа"
    return out


def drop_own_reserve(profile: dict, acc: dict) -> None:
    """Запасной ключ — НАШ сеанс: из списка чужих он убирается до всего
    остального, иначе дозор тревожил бы о нём, а автозащита его закрыла бы."""
    h = acc.get("reserve_auth_hash")
    auths = profile.get("authorizations")
    if h and auths:
        profile["authorizations"] = [f for f in auths if int(f.get("hash") or 0) != int(h)]


_RESERVE_PER_OWNER_SWEEP = 10


async def _make_reserve(pool, client, acc: dict, profile: dict, owner: int, acc_id: int) -> str:
    """Завести и записать запасной ключ. → "made" или причина по-русски."""
    from services import account_manager as am
    from services.token_vault import decrypt_token, encrypt_plaintext_token
    pw = None
    if profile.get("twofa_on") is not False:
        try:
            enc = await pool.fetchval(
                "SELECT twofa_enc FROM tg_accounts WHERE id=$1 AND owner_id=$2", acc_id, owner)
            pw = decrypt_token(enc) if enc else None
        except Exception as exc:
            log.warning("session_watch: пароль 2FA acc=%d не прочитан: %s", acc_id, exc)
    try:
        sess, h = await am.mint_reserve_session(client, acc, pw)
    except am.ReserveKeyError as exc:
        return str(exc)
    enc = encrypt_plaintext_token(sess)
    for _attempt in (1, 2):
        try:
            await pool.execute(
                "UPDATE tg_accounts SET reserve_session_enc=$1, reserve_auth_hash=$2, "
                "reserve_made_at=now(), reserve_checked_at=now() WHERE id=$3 AND owner_id=$4",
                enc, int(h), acc_id, owner)
            return "made"
        except Exception as exc:
            log.warning("session_watch: запасной ключ acc=%d не записан (%d): %s",
                        acc_id, _attempt, exc)
    # Незаписанный ключ — безымянный вход в аккаунт: закрываем его сами.
    try:
        await _close_one(client, int(h))
    except Exception as exc:
        log.warning("session_watch: незаписанный запасной сеанс acc=%d не закрыт: %s",
                    acc_id, exc)
    return "ключ не записался в базу — сеанс закрыт"


def acc_label(acc: dict) -> str:
    return (f"@{acc['username']}" if acc.get("username")
            else (acc.get("first_name") or acc.get("phone") or f"#{acc.get('id')}"))


def build_guard_report(rows: list[tuple[str, dict]]) -> str | None:
    """Одно сообщение владельцу за проход: что автозащита сделала. None — нечего."""
    closed = [(lbl, r) for lbl, r in rows if r.get("closed")]
    twofa = [(lbl, r) for lbl, r in rows if r.get("twofa") == "set"]
    failed = [(lbl, r) for lbl, r in rows if r.get("twofa") not in (None, "set")]
    reserve = [(lbl, r) for lbl, r in rows if r.get("reserve") == "made"]
    no_reserve = [(lbl, r) for lbl, r in rows if r.get("reserve") not in (None, "made")]
    if not (closed or twofa or failed or reserve or no_reserve):
        return None
    lines = ["🛡 <b>Автозащита флота</b>", ""]
    if closed:
        n = sum(len(r["closed"]) for _l, r in closed)
        lines.append(f"Закрыто чужих сеансов: <b>{n}</b> на {len(closed)} акк.")
        for lbl, r in closed[:_MAX_LISTED]:
            lines.append(f"• {html.escape(lbl)}: " + html.escape(describe_session(r["closed"][0]))
                         + (f" и ещё {len(r['closed']) - 1}" if len(r["closed"]) > 1 else ""))
        if len(closed) > _MAX_LISTED:
            lines.append(f"• …и ещё {len(closed) - _MAX_LISTED} акк.")
        lines.append("")
    if twofa:
        lines.append(f"Поставлен пароль 2FA: <b>{len(twofa)}</b> акк. — у каждого свой, "
                     "они в «Профили → Пароли 2FA».")
        lines.append("")
    if failed:
        lines.append(f"Пароль не встал: {len(failed)} акк.")
        for lbl, r in failed[:_MAX_LISTED]:
            lines.append(f"• {html.escape(lbl)}: {html.escape(str(r['twofa']))}")
        lines.append("")
    if reserve:
        lines.append(f"Заведён запасной ключ: <b>{len(reserve)}</b> акк. — если основная "
                     "сессия умрёт, флот поднимет аккаунт сам.")
        lines.append("")
    if no_reserve:
        lines.append(f"Запасной ключ не завёлся: {len(no_reserve)} акк.")
        for lbl, r in no_reserve[:_MAX_LISTED]:
            lines.append(f"• {html.escape(lbl)}: {html.escape(str(r['reserve']))}")
        lines.append("")
    lines.append("<i>Выключить: «Щит → Автозащита».</i>")
    return "\n".join(lines)


async def send_guard_report(pool, bot, owner_id: int, rows: list[tuple[str, dict]]) -> None:
    text = build_guard_report(rows)
    if not text or bot is None:
        return
    try:
        from database import db
        await db.notify_if_enabled(pool, bot, int(owner_id), "restriction", text,
                                   dedup_key=f"guard:{int(time.time())}")
    except Exception as exc:
        log.warning("session_watch: отчёт автозащиты owner=%s не ушёл: %s", owner_id, exc)


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


# ── Запасной ключ: поднять аккаунт, чей основной сеанс умер ──────────────────

_KEEPER_PERIOD_S = 600
_KEEPER_BATCH = 20
_RESERVE_RECHECK_DAYS = 7


def build_revived_alert(label: str) -> str:
    return "\n".join([
        "♻️ <b>Аккаунт поднят из запасного ключа</b>",
        "",
        f"Аккаунт: <b>{html.escape(label)}</b>",
        "Основная сессия умерла, аккаунт вернулся в работу на запасной. Новый "
        "запасной ключ автозащита заведёт на ближайшем обходе.",
    ])


async def _clear_reserve(pool, acc_id: int, enc: str) -> None:
    try:
        await pool.execute(
            "UPDATE tg_accounts SET reserve_session_enc=NULL, reserve_auth_hash=NULL, "
            "reserve_made_at=NULL, reserve_checked_at=NULL "
            "WHERE id=$1 AND reserve_session_enc=$2", acc_id, enc)
    except Exception as exc:
        log.warning("session_watch: запасной ключ acc=%d не снят: %s", acc_id, exc)


async def revive_from_reserve(pool, acc: dict, bot=None) -> str:
    """Проверить запасной ключ аккаунта и, если основной сеанс мёртв, поднять.

    → "revived" | "alive" (ключ жив, основной тоже) | "dead" (ключ умер — снят)
    | "skip" (ничего не известно). Не бросает.
    """
    from services import account_manager as am
    try:
        acc_id = int(acc["id"])
        enc = acc["reserve_session_enc"]
    except (KeyError, TypeError, ValueError):
        return "skip"
    if not enc:
        return "skip"
    try:
        res = await am.check_account_status_full(enc, _acc=dict(acc), check_spambot=False)
    except Exception as exc:
        log.warning("session_watch: проверка запасного ключа acc=%d: %s", acc_id, exc)
        return "skip"
    st = res.get("status")
    if st == "session_expired":
        await _clear_reserve(pool, acc_id, enc)
        return "dead"
    if st != "active" or res.get("session_busy"):
        return "skip"
    if str(acc.get("acc_status") or "") != "session_expired":
        try:
            await pool.execute("UPDATE tg_accounts SET reserve_checked_at=now() "
                               "WHERE id=$1 AND reserve_session_enc=$2", acc_id, enc)
        except Exception as exc:
            log.warning("session_watch: отметка запасного ключа acc=%d: %s", acc_id, exc)
        return "alive"
    from services.token_vault import session_fingerprint
    try:
        row = await pool.fetchrow(
            "UPDATE tg_accounts SET session_str=reserve_session_enc, session_fp=$3, "
            "own_session_at=reserve_made_at, reserve_session_enc=NULL, "
            "reserve_auth_hash=NULL, reserve_made_at=NULL, reserve_checked_at=NULL, "
            "session_conflict_at=NULL, is_active=TRUE "
            "WHERE id=$1 AND reserve_session_enc=$2 AND acc_status='session_expired' "
            "RETURNING id", acc_id, enc, session_fingerprint(enc))
    except Exception as exc:
        log.warning("session_watch: подъём acc=%d не записан: %s", acc_id, exc)
        return "skip"
    if not row:
        return "skip"
    from services import account_status as _st
    await _st.set_status(pool, acc_id, "active",
                         reason="Основная сессия умерла — аккаунт поднят из запасного ключа",
                         source="reserve_key")
    log.info("session_watch: acc=%d поднят из запасного ключа", acc_id)
    if bot is not None and acc.get("owner_id"):
        try:
            from database import db
            await db.notify_if_enabled(pool, bot, int(acc["owner_id"]), "restriction",
                                       build_revived_alert(acc_label(acc)),
                                       dedup_key=f"revived:{acc_id}:{int(time.time())}")
        except Exception as exc:
            log.warning("session_watch: сообщение о подъёме acc=%d: %s", acc_id, exc)
    return "revived"


async def keep_reserves(pool, bot=None) -> dict:
    """Один круг: поднять умершие аккаунты с запасным ключом и проверить
    давно не проверенные ключи живых. Аккаунт берётся в аренду у op_worker,
    как при любом коннекте."""
    from database.db import telethon_accounts_query as _tq
    extra = "a.reserve_session_enc"
    dead = await pool.fetch(
        _tq("a.acc_status = 'session_expired' AND a.reserve_session_enc IS NOT NULL "
            "ORDER BY a.id LIMIT $1", extra_cols=extra), _KEEPER_BATCH)
    stale = await pool.fetch(
        _tq("a.reserve_session_enc IS NOT NULL "
            "AND COALESCE(a.acc_status, 'active') <> 'session_expired' "
            "AND COALESCE(a.reserve_checked_at, 'epoch') < now() - make_interval(days => $2) "
            "ORDER BY a.reserve_checked_at NULLS FIRST LIMIT $1", extra_cols=extra),
        _KEEPER_BATCH, _RESERVE_RECHECK_DAYS)
    tally: dict = {}
    for acc in list(dead) + list(stale):
        try:
            from services import op_worker as _opw
            leased = await _opw.try_claim_account(int(acc["id"]))
        except Exception:
            _opw, leased = None, True
        if not leased:
            continue
        try:
            r = await revive_from_reserve(pool, dict(acc), bot=bot)
            tally[r] = tally.get(r, 0) + 1
        finally:
            if _opw is not None:
                try:
                    await _opw.release_accounts([int(acc["id"])])
                except Exception:
                    log.debug("reserve_keeper: release acc=%s", acc["id"])
    return tally


async def run_reserve_keeper(pool, bot=None) -> None:
    """Фоновый круг запасных ключей: раз в 10 минут."""
    await asyncio.sleep(120)
    while True:
        try:
            tally = await keep_reserves(pool, bot)
            if tally:
                log.info("reserve_keeper: %s", tally)
        except Exception as exc:
            log.error("reserve_keeper: %s", exc)
        await asyncio.sleep(_KEEPER_PERIOD_S)
