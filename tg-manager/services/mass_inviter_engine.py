"""Движок массового инвайтинга.

Поддерживает:
  - Добавление по user_id / @username
  - Добавление по номерам телефонов (import contact → invite → delete contact)

Обрабатывает:
  - UserPrivacyRestricted → пропустить
  - PeerFloodError → аккаунт перегрет, переключиться
  - UserNotMutualContact → только для закрытых групп, пропустить
  - FloodWaitError → пауза + flood_engine
  - UserAlreadyParticipant → считать как успех
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
from typing import Any
from services.logger import log_exc_swallow

log = logging.getLogger(__name__)

_CONNECT_TIMEOUT = 15.0
_ACTION_TIMEOUT = 15.0
_BATCH_SIZE = 5  # Telegram разрешает добавлять до 5 за раз без флуда
# FloodWait длиннее этого (сек) — стоп-сигнал: продолжать инвайты во время
# активного длинного флуда = эскалация (Telegram усиливает ограничение). Короткий
# флуд пережидаем инлайн, длинный — прекращаем батч и сигналим наверх для cooldown.
_MAX_FLOOD_INLINE = 60


async def _resolve_group_entity(client: Any, group_ref: str) -> Any:
    """Resolve a group reference to an entity usable by InviteToChannelRequest.

    Handles both public (@username / t.me/username) and private invite-link
    (t.me/+HASH, t.me/joinchat/HASH) references. The naive `client.get_entity()`
    on a raw invite-link string either mismatches "joinchat" as a bogus username
    or fails outright for `+HASH` links — Telethon can't resolve an invite hash
    to a peer without ImportChatInviteRequest/CheckChatInviteRequest.
    """
    from services.account_manager import normalize_telegram_join_ref

    ref_kind, ref_value = normalize_telegram_join_ref(group_ref)
    if ref_kind != "invite":
        return await asyncio.wait_for(
            client.get_entity(ref_value or group_ref), timeout=_ACTION_TIMEOUT
        )

    from telethon.tl.functions.messages import (
        ImportChatInviteRequest,
        CheckChatInviteRequest,
    )
    from telethon.errors import UserAlreadyParticipantError

    try:
        result = await asyncio.wait_for(
            client(ImportChatInviteRequest(hash=ref_value)), timeout=_ACTION_TIMEOUT
        )
        chats = getattr(result, "chats", None) or []
        if chats:
            return chats[0]
        raise ValueError("ImportChatInviteRequest returned no chat")
    except UserAlreadyParticipantError:
        # Account is already a member — peek instead of re-joining to get the entity.
        info = await asyncio.wait_for(
            client(CheckChatInviteRequest(hash=ref_value)), timeout=_ACTION_TIMEOUT
        )
        chat = getattr(info, "chat", None)
        if chat is None:
            raise
        return chat


# ── Добавление пачки user_id/username ────────────────────────────────────────

async def invite_batch(
    session_string: str,
    _acc: dict | None,
    group_ref: str,
    user_refs: list[str | int],
) -> dict[str, Any]:
    """Добавить список пользователей (ID или @username) в группу.

    Возвращает:
      {"ok": int, "failed": int, "peer_flood": bool, "errors": list[str]}
    """
    from services.account_manager import connect_client
    from telethon.tl.functions.channels import InviteToChannelRequest
    from telethon.errors import (
        UserPrivacyRestrictedError,
        UserAlreadyParticipantError,
        PeerFloodError,
        UserNotMutualContactError,
        FloodWaitError,
        ChatWriteForbiddenError,
        ChannelPrivateError,
        ChatAdminRequiredError,
    )
    try:
        from telethon.errors import UsersTooMuchError
    except Exception:  # имя может отличаться между версиями telethon
        UsersTooMuchError = ()

    client = None
    ok, failed = 0, 0
    errors: list[str] = []
    peer_flood = False
    flood_wait = 0
    # Цели, которые отклонены приватностью/не-взаимностью — их прямой инвайт не
    # берёт, но может взять «добавление через выдачу админки» (промоут-трюк).
    privacy_failed: list = []

    try:
        client = await connect_client(session_string, _acc, "invite")
        group = await _resolve_group_entity(client, group_ref)

        for ref in user_refs:
            try:
                user = await asyncio.wait_for(client.get_entity(ref), timeout=_ACTION_TIMEOUT)
                await asyncio.wait_for(
                    client(InviteToChannelRequest(channel=group, users=[user])),
                    timeout=_ACTION_TIMEOUT,
                )
                ok += 1
                await asyncio.sleep(random.uniform(2.0, 4.0))
            except UserAlreadyParticipantError:
                ok += 1  # уже в группе = успех
            except UserPrivacyRestrictedError:
                failed += 1
                privacy_failed.append(ref)
                errors.append(f"{ref}: privacy restricted")
            except UserNotMutualContactError:
                failed += 1
                privacy_failed.append(ref)
                errors.append(f"{ref}: not mutual contact")
            except PeerFloodError:
                peer_flood = True
                failed += 1
                errors.append(f"{ref}: peer flood — аккаунт ограничен")
                break  # аккаунт перегрет, дальше не пробуем
            except FloodWaitError as e:
                _fw = int(getattr(e, "seconds", 60) or 60)
                failed += 1
                errors.append(f"{ref}: flood wait {_fw}s")
                if _fw > _MAX_FLOOD_INLINE:
                    # Длинный флуд: НЕ инвайтим во время активного флуда (эскалация).
                    flood_wait = _fw
                    break
                await asyncio.sleep(min(_fw, 60))
            except ChatAdminRequiredError:
                # Не про пользователя, а про ПРАВА аккаунта в этом чате. Для канала
                # добавлять участников может только админ с правом «Добавлять
                # подписчиков»; без него так падает КАЖДАЯ попытка → 0 из тысяч.
                # Останавливаем сразу (group error), чтобы не молотить вхолостую.
                failed += 1
                errors.append("group error: у аккаунта нет прав добавлять участников "
                              "— для канала нужен админ с правом «Добавлять подписчиков», "
                              "в группе — снять ограничение «Добавление участников: только админы»")
                break
            except UsersTooMuchError:
                failed += 1
                errors.append("group error: в чате достигнут лимит участников Telegram")
                break
            except (ChatWriteForbiddenError, ChannelPrivateError) as e:
                failed += 1
                errors.append(f"group error: нет доступа к чату ({type(e).__name__})")
                break  # нет прав/группа закрыта
            except Exception as e:
                log.warning('invite failed: %s', e)
                failed += 1
                errors.append(f"{ref}: {str(e)[:80]}")

    except Exception as exc:
        log.warning("invite_batch connect/group error: %s", exc)
        errors.append(f"connect: {str(exc)[:100]}")
    finally:
        try:
            await client.disconnect()
        except Exception as e:
            log_exc_swallow(log, "invite_batch: disconnect")

    return {"ok": ok, "failed": failed, "peer_flood": peer_flood,
            "flood_wait": flood_wait, "errors": errors,
            "privacy_failed": privacy_failed}


# ── Автовыдача прав админа инвайтерам + «промоут-трюк» ───────────────────────

async def channel_admin_status(session_string: str, _acc: dict | None,
                               group_ref: str) -> dict[str, Any]:
    """Права ЭТОГО аккаунта в целевом чате: создатель / админ с add_admins / инвайт.

    Нужно, чтобы выбрать «промоутера» — аккаунт, который вправе выдавать админку
    остальным инвайтерам (и добавлять через промоут-трюк). Только чтение.
    """
    from services.account_manager import connect_client
    from telethon.tl.functions.channels import GetParticipantRequest
    from telethon.tl.types import ChannelParticipantCreator, ChannelParticipantAdmin

    client = None
    try:
        client = await connect_client(session_string, _acc, "invite")
        group = await _resolve_group_entity(client, group_ref)
        me = await asyncio.wait_for(client.get_me(), timeout=_ACTION_TIMEOUT)
        _chan_id = getattr(group, "id", None)  # id чата — чтобы вызвать promote_all_admins
        part = await asyncio.wait_for(
            client(GetParticipantRequest(channel=group, participant="me")),
            timeout=_ACTION_TIMEOUT)
        p = part.participant
        if isinstance(p, ChannelParticipantCreator):
            return {"ok": True, "user_id": me.id, "creator": True,
                    "can_promote": True, "can_invite": True, "channel_id": _chan_id}
        if isinstance(p, ChannelParticipantAdmin):
            r = getattr(p, "admin_rights", None)
            return {"ok": True, "user_id": me.id, "creator": False,
                    "can_promote": bool(getattr(r, "add_admins", False)),
                    "can_invite": bool(getattr(r, "invite_users", False)),
                    "channel_id": _chan_id}
        return {"ok": True, "user_id": me.id, "creator": False,
                "can_promote": False, "can_invite": False, "channel_id": _chan_id}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:120]}
    finally:
        try:
            await client.disconnect()
        except Exception:
            log_exc_swallow(log, "channel_admin_status: disconnect")


async def add_via_promote(session_string: str, _acc: dict | None, group_ref: str,
                          user_refs: list[str | int]) -> dict[str, Any]:
    """«Промоут-трюк»: добавить пользователя, выдав ему админку и тут же сняв.

    Когда прямой InviteToChannel блокируется приватностью, аккаунт-АДМИН с правом
    add_admins может добавить человека через EditAdmin (он становится участником),
    после чего снять права → остаётся обычным участником. Требует прав add_admins
    у вызывающего аккаунта (создатель/админ). Медленнее и заметнее обычного
    инвайта — только как запасной путь для заблокированных целей.
    """
    from services.account_manager import connect_client
    from telethon.tl.functions.channels import EditAdminRequest
    from telethon.tl.types import ChatAdminRights
    from telethon.errors import (
        UserAlreadyParticipantError, PeerFloodError, FloodWaitError,
        UserPrivacyRestrictedError, ChatAdminRequiredError,
    )

    _grant = ChatAdminRights(
        post_messages=False, edit_messages=False, delete_messages=False,
        ban_users=False, invite_users=True, pin_messages=False, add_admins=False,
        manage_call=False, other=False, change_info=False, anonymous=False,
        manage_topics=False)
    _revoke = ChatAdminRights(
        post_messages=False, edit_messages=False, delete_messages=False,
        ban_users=False, invite_users=False, pin_messages=False, add_admins=False,
        manage_call=False, other=False, change_info=False, anonymous=False,
        manage_topics=False)

    client = None
    ok, failed = 0, 0
    errors: list[str] = []
    peer_flood = False
    flood_wait = 0
    try:
        client = await connect_client(session_string, _acc, "invite")
        group = await _resolve_group_entity(client, group_ref)
        for ref in user_refs:
            try:
                user = await asyncio.wait_for(client.get_entity(ref), timeout=_ACTION_TIMEOUT)
                # Выдать минимальную админку → человек добавлен в чат…
                await asyncio.wait_for(
                    client(EditAdminRequest(channel=group, user_id=user,
                                            admin_rights=_grant, rank="")),
                    timeout=_ACTION_TIMEOUT)
                await asyncio.sleep(random.uniform(1.0, 2.0))
                # …и сразу снять права → остаётся обычным участником.
                await asyncio.wait_for(
                    client(EditAdminRequest(channel=group, user_id=user,
                                            admin_rights=_revoke, rank="")),
                    timeout=_ACTION_TIMEOUT)
                ok += 1
                await asyncio.sleep(random.uniform(2.5, 5.0))
            except UserAlreadyParticipantError:
                ok += 1
            except ChatAdminRequiredError:
                errors.append("group error: нет прав add_admins для промоут-трюка")
                break
            except PeerFloodError:
                peer_flood = True
                failed += 1
                errors.append(f"{ref}: peer flood")
                break
            except FloodWaitError as e:
                _fw = int(getattr(e, "seconds", 60) or 60)
                failed += 1
                if _fw > _MAX_FLOOD_INLINE:
                    flood_wait = _fw
                    break
                await asyncio.sleep(min(_fw, 60))
            except UserPrivacyRestrictedError:
                # Даже трюк не всегда обходит приватность — честно считаем провалом.
                failed += 1
                errors.append(f"{ref}: privacy (промоут-трюк не помог)")
            except Exception as e:
                log.warning("add_via_promote failed: %s", e)
                failed += 1
                errors.append(f"{ref}: {str(e)[:80]}")
    except Exception as exc:
        log.warning("add_via_promote connect/group error: %s", exc)
        errors.append(f"connect: {str(exc)[:100]}")
    finally:
        try:
            await client.disconnect()
        except Exception:
            log_exc_swallow(log, "add_via_promote: disconnect")

    return {"ok": ok, "failed": failed, "peer_flood": peer_flood,
            "flood_wait": flood_wait, "errors": errors}


# ── Добавление по номерам телефонов ──────────────────────────────────────────

async def invite_by_phones(
    session_string: str,
    _acc: dict | None,
    group_ref: str,
    phones: list[str],
) -> dict[str, Any]:
    """Добавить список номеров телефонов в группу.

    Алгоритм: ImportContactsRequest → получить user_id → InviteToChannel → DeleteContacts.
    """
    from services.account_manager import connect_client
    from telethon.tl.functions.channels import InviteToChannelRequest
    from telethon.tl.functions.contacts import ImportContactsRequest, DeleteContactsRequest
    from telethon.tl.types import InputPhoneContact
    from telethon.errors import (
        UserPrivacyRestrictedError,
        UserAlreadyParticipantError,
        PeerFloodError,
        FloodWaitError,
        ChatAdminRequiredError,
    )

    client = None
    ok, failed = 0, 0
    errors: list[str] = []
    peer_flood = False
    flood_wait = 0
    group_broken = False
    imported_users: list = []
    invited_phones: list[str] = []   # номера, реально добавленные (для дедупа впредь)
    _uid_to_phone: dict = {}          # user_id → исходный номер (по client_id)
    _resolved_phones: set = set()     # номера, которые Telegram сопоставил юзеру

    try:
        client = await connect_client(session_string, _acc, "invite")
        group = await _resolve_group_entity(client, group_ref)

        # Импортируем контакты
        contacts = [
            InputPhoneContact(client_id=i, phone=p, first_name="u", last_name="")
            for i, p in enumerate(phones)
        ]
        result = await asyncio.wait_for(
            client(ImportContactsRequest(contacts=contacts)),
            timeout=_ACTION_TIMEOUT,
        )
        imported_users = list(result.users)
        # client_id → номер: какие именно номера Telegram сопоставил юзеру (по нему
        # различаем «добавлен» и «не найден», а не только считаем разницу длин).
        for _imp in (getattr(result, "imported", None) or []):
            _ci = getattr(_imp, "client_id", None)
            if _ci is not None and 0 <= int(_ci) < len(phones):
                _uid_to_phone[getattr(_imp, "user_id", None)] = phones[int(_ci)]
                _resolved_phones.add(phones[int(_ci)])
        log.info("invite_by_phones: imported %d/%d users", len(imported_users), len(phones))

        for user in imported_users:
            if peer_flood or flood_wait or group_broken:
                break
            _ph = _uid_to_phone.get(getattr(user, "id", None))
            try:
                await asyncio.wait_for(
                    client(InviteToChannelRequest(channel=group, users=[user])),
                    timeout=_ACTION_TIMEOUT,
                )
                ok += 1
                if _ph:
                    invited_phones.append(_ph)
                await asyncio.sleep(random.uniform(2.5, 5.0))
            except UserAlreadyParticipantError:
                ok += 1
                if _ph:
                    invited_phones.append(_ph)
            except ChatAdminRequiredError:
                # Права на добавление в чат отсутствуют — падает каждая попытка,
                # стоп сразу (group error), как и в invite_batch.
                group_broken = True
                failed += 1
                errors.append("group error: у аккаунта нет прав добавлять участников "
                              "— для канала нужен админ с правом «Добавлять подписчиков»")
                break
            except UserPrivacyRestrictedError:
                failed += 1
            except PeerFloodError:
                peer_flood = True
                failed += 1
                errors.append("peer flood — аккаунт ограничен")
            except FloodWaitError as e:
                _fw = int(getattr(e, "seconds", 60) or 60)
                failed += 1
                if _fw > _MAX_FLOOD_INLINE:
                    flood_wait = _fw  # длинный флуд → стоп, не инвайтим во время флуда
                    errors.append(f"flood wait {_fw}s")
                    break
                await asyncio.sleep(min(_fw, 60))
            except Exception as e:
                log.warning('invite failed: %s', e)
                failed += 1
                errors.append(str(e)[:80])

        # Удаляем импортированные контакты
        if imported_users:
            try:
                await asyncio.wait_for(
                    client(DeleteContactsRequest(id=imported_users)),
                    timeout=_ACTION_TIMEOUT,
                )
            except Exception as e:
                log_exc_swallow(log, "invite_by_phones: import")

        # Номера, которые Telegram НЕ сопоставил юзеру — не найдены (не в Telegram).
        # Их НЕ помечаем приглашёнными: человек может зарегистрироваться позже.
        not_found_phones = [p for p in phones if p not in _resolved_phones]
        not_found = len(not_found_phones)
        if not_found:
            failed += not_found
            errors.append(f"{not_found} номеров не зарегистрированы в Telegram")

    except Exception as exc:
        log.warning("invite_by_phones error: %s", exc)
        errors.append(str(exc)[:100])
        not_found_phones = []
    finally:
        try:
            await client.disconnect()
        except Exception as e:
            log_exc_swallow(log, "invite_by_phones: disconnect")

    return {"ok": ok, "failed": failed, "peer_flood": peer_flood,
            "flood_wait": flood_wait, "errors": errors,
            # Для дедупа впредь и честного пер-номер отчёта:
            "invited_phones": list(dict.fromkeys(invited_phones)),
            "not_found_phones": not_found_phones}


# ── Утилиты ──────────────────────────────────────────────────────────────────

def parse_group_ref(text: str) -> str:
    """Normalize a user-typed group reference (@name / t.me/name / numeric ID /
    private invite link) to a canonical form _resolve_group_entity() can consume.

    Delegates public-vs-invite classification to account_manager's
    normalize_telegram_join_ref — the previous hand-rolled regex here matched
    "t.me/joinchat/HASH" as if "joinchat" were a public username, and didn't
    match "t.me/+HASH" at all (the plus sign isn't in its character class),
    silently corrupting every private invite link passed to this feature.
    """
    text = text.strip()
    if re.match(r"^-?\d+$", text):
        return text  # numeric chat ID — not a join ref, pass through as-is

    from services.account_manager import format_telegram_join_ref_display

    formatted = format_telegram_join_ref_display(text)
    return formatted or text


def parse_user_refs(text: str) -> list[str]:
    """Парсинг строки с @username или ID через запятую/пробел/перенос."""
    refs: list[str] = []
    for token in re.split(r"[,;\s\n]+", text.strip()):
        token = token.strip().lstrip("@")
        if not token:
            continue
        if re.match(r"^\d+$", token):
            refs.append(token)
        elif re.match(r"^[A-Za-z0-9_]{3,}$", token):
            refs.append(f"@{token}")
    return list(dict.fromkeys(refs))[:500]


def parse_phones(text: str) -> list[str]:
    """Парсинг номеров телефонов: +79991234567 через любой разделитель."""
    phones: list[str] = []
    for token in re.split(r"[,;\s\n]+", text.strip()):
        token = re.sub(r"[^\d+]", "", token)
        if len(token) >= 10:
            if not token.startswith("+"):
                token = "+" + token
            phones.append(token)
    return list(dict.fromkeys(phones))[:500]


# Telegram user-ID сейчас укладывается в 10 цифр (< 10^10); телефон в формате
# E.164 со страновым кодом — это 11–15 цифр. Поэтому чисто цифровой токен без «+»
# из 11+ цифр — это номер телефона, потерявший ведущий «+», а не ID.
_PHONE_MIN_DIGITS = 11


def _looks_like_bare_phone(token: str) -> bool:
    """True, если токен — телефон без «+»: только цифры и длина ≥ _PHONE_MIN_DIGITS.

    Не трогает @username и короткие числовые ID: у них либо есть буквы/@, либо
    цифр ≤ 10.
    """
    return token.isdigit() and len(token) >= _PHONE_MIN_DIGITS


_PHONE_MAX_DIGITS = 15  # E.164 — максимум 15 цифр вместе со страновым кодом
# Токен-ref, который примет parse_user_refs: числовой ID или @username ≥ 3 симв.
_REF_TOKEN_RE = re.compile(r"^(?:\d+|[A-Za-z0-9_]{3,})$")


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def classify_invite_list(raw: str) -> dict[str, list[str]]:
    """Разобрать вставленный список на телефоны / @username-ID / нераспознанное.

    Возвращает ``{"phones": [...], "user_refs": [...], "unrecognized": [...]}``.
    Устойчив к человеческому вводу:
      * строка-телефон в любом формате (``+7 999 123-45-67``, ``7(911)149-11-99``)
        распознаётся целиком — разделители внутри номера не рвут его на куски;
      * несколько токенов в строке через пробел (``@a @b 12345``) разбираются
        по отдельности;
      * «+» ИЛИ 11–15 цифр → телефон; числовой токен ≤10 цифр → ID; ``@name`` или
        буквенно-цифровое имя ≥3 симв. → @username; остальное — в ``unrecognized``
        (чтобы показать пользователю, что именно не понято, а не молча потерять).
    """
    phones_src: list[str] = []
    refs_src: list[str] = []
    unrec: list[str] = []
    # Делим на «строки» по переносам/запятым/точкам-с-запятой, НЕ по пробелам —
    # иначе форматированный номер разорвётся на части.
    for line in re.split(r"[\n,;]+", (raw or "").strip()):
        line = line.strip()
        if not line:
            continue
        has_alpha = any(c.isalpha() for c in line)
        d = _digits(line)
        # Одиночный номер в строке (с «+» или без), возможно с форматированием.
        # Кладём УЖЕ нормализованные цифры ("+"+d), а не сырую строку: parse_phones
        # режет по пробелам, и "+7 (999) 123-45-67" иначе распалось бы на куски.
        if not has_alpha and "@" not in line:
            if line.startswith("+") and 10 <= len(d) <= _PHONE_MAX_DIGITS:
                phones_src.append("+" + d)
                continue
            if _PHONE_MIN_DIGITS <= len(d) <= _PHONE_MAX_DIGITS:
                phones_src.append("+" + d)
                continue
        # Иначе — несколько токенов через пробел: классифицируем каждый.
        for tok in line.split():
            if not tok:
                continue
            td = _digits(tok)
            if tok.startswith("+"):
                (phones_src if 10 <= len(td) <= _PHONE_MAX_DIGITS else unrec).append(tok)
            elif tok.isdigit() and _PHONE_MIN_DIGITS <= len(tok) <= _PHONE_MAX_DIGITS:
                phones_src.append(tok)
            elif _REF_TOKEN_RE.match(tok.lstrip("@")):
                refs_src.append(tok)
            else:
                unrec.append(tok)
    return {
        "phones": parse_phones(" ".join(phones_src)),
        "user_refs": parse_user_refs(" ".join(refs_src)),
        "unrecognized": unrec[:50],
    }


def split_invite_targets(raw: str) -> tuple[list[str], list[str]]:
    """Разбить вставленный список на (user_refs, phones), взаимоисключающе.

    Тонкая обёртка над :func:`classify_invite_list` (единый источник правды
    классификации) — сохраняет прежнюю сигнатуру для инвайт-эндпоинта и тестов.
    Числовой ID (≤10 цифр) не попадает в телефоны → нет двойного инвайта.
    """
    r = classify_invite_list(raw)
    return r["user_refs"], r["phones"]
