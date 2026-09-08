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
import os
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

# Потолок размера списка на приём (защита от гигантских файлов/памяти). Раньше был
# жёсткий 500 — оператор просил принимать больше. Реальный темп/суточные лимиты
# инвайта всё равно ограничивает флот-логика; это лишь верхняя граница на приём.
MAX_INVITE_LIST = 200_000


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

# ── Классификация отказов по цели ────────────────────────────────────────────
# Отказы инвайта делятся на горстку РАЗНЫХ по смыслу случаев, и оператору важно
# различать их: «приватность» — ограничение получателя (обходится только
# промоут-трюком; рассылку ссылок в ЛС незнакомцам здесь НЕ предлагаем — она
# собирает жалобы и убивает флот), «нет в Telegram» — мёртвая цель (убрать из базы), «в
# слишком многих чатах» — лимит на стороне цели (сегодня не добавится никак).
# Раньше всё, кроме приватности и флуда, сваливалось в «прочее»: крупнейшая по
# объёму корзина отказов не имела ни имени, ни смысла, и оператор не понимал,
# чинить ему аудиторию, права или аккаунты.
FAIL_PRIVACY = "privacy"          # настройки приватности получателя
FAIL_NOT_MUTUAL = "not_mutual"    # требует взаимного контакта
FAIL_DEAD = "dead"                # username не существует / аккаунт удалён
FAIL_TOO_MANY_CHATS = "too_many_chats"   # цель уже в предельном числе чатов
FAIL_BANNED = "banned_in_chat"    # цель забанена/кикнута в этом чате
FAIL_BLOCKED = "blocked"          # цель заблокировала инвайтера
FAIL_FLOOD = "flood"
FAIL_PERM = "perm"                # права/доступность самого чата
# Ниже — корзины, которых не было: всё это падало в «прочее» и пряталось от
# диагностики (на живом прогоне «прочее: 58» из 177 отказов).
FAIL_ALREADY_IN = "already_in"    # цель УЖЕ в чате — это не отказ вовсе
FAIL_CHAT_FULL = "chat_full"      # в чате предел участников
FAIL_RESTRICTED = "restricted"    # аккаунт цели ограничен Telegram
FAIL_IS_BOT = "is_bot"            # цель — бот, его так не добавить
FAIL_OTHER = "other"

FAIL_LABELS = {
    FAIL_PRIVACY: "🔒 приватность (нельзя добавить)",
    FAIL_NOT_MUTUAL: "🔒 не в контактах",
    FAIL_DEAD: "👻 нет в Telegram (удалён/username свободен)",
    FAIL_TOO_MANY_CHATS: "📦 у цели предел по числу чатов",
    FAIL_BANNED: "⛔ цель забанена в этом чате",
    FAIL_BLOCKED: "🚷 цель заблокировала инвайтера",
    FAIL_FLOOD: "⏳ флуд-лимит",
    FAIL_PERM: "🚫 нет прав в чате",
    FAIL_ALREADY_IN: "✅ уже в чате",
    FAIL_CHAT_FULL: "📦 в чате предел участников",
    FAIL_RESTRICTED: "🚫 аккаунт цели ограничен Telegram",
    FAIL_IS_BOT: "🤖 цель — бот",
    FAIL_OTHER: "❓ прочее",
}

# Подсказка «что с этим делать» — по крупнейшей корзине отказов.
FAIL_ADVICE = {
    # Рассылку ссылок в ЛС незнакомцам здесь НЕ советуем: она собирает жалобы и
    # уничтожает флот (решение владельца). Приватность обходить нечем — честно
    # говорим об этом и предлагаем то, что не стоит аккаунтов.
    FAIL_PRIVACY: ("Приватность получателей Telegram обойти нельзя — ни одним "
                   "методом. Такие цели лучше исключить из базы: на них впустую "
                   "тратится дневной лимит аккаунтов. Приводить их стоит "
                   "публично — постом со ссылкой на чат, чтобы человек вступил сам."),
    FAIL_NOT_MUTUAL: ("Эти цели требуют взаимного контакта — добавить их нельзя. "
                      "Исключите их из базы, чтобы не жечь на них лимиты."),
    FAIL_DEAD: ("Аудитория устарела: этих аккаунтов больше нет. Соберите её заново "
                "и почистите базу — на мёртвых целях тратится дневной лимит."),
    FAIL_TOO_MANY_CHATS: ("Цели состоят в предельном числе чатов — Telegram не даст "
                          "добавить их никаким методом."),
    FAIL_BANNED: "Эти люди забанены в целевом чате — снимите бан или исключите их.",
    FAIL_BLOCKED: "Цели заблокировали ваши аккаунты — используйте другие инвайтеры.",
    FAIL_ALREADY_IN: ("Эти люди уже состоят в чате — это не отказ. Включите дедуп, "
                      "чтобы не тратить на них лимиты повторно."),
    FAIL_CHAT_FULL: ("В чате достигнут предел участников — Telegram больше никого "
                     "не пустит. Нужен новый чат."),
    FAIL_RESTRICTED: ("Аккаунты целей ограничены самим Telegram — добавить их нельзя, "
                      "исключите из базы."),
    FAIL_IS_BOT: "Ботов так не добавляют — их приглашает админ чата вручную.",
}

_DEAD_MARKERS = (
    "no user has", "cannot find any entity", "usernamenotoccupied",
    "usernameinvalid", "inputuserdeactivated", "userdeactivated",
    "peeridinvalid", "the username is not occupied",
)


def classify_invite_error(exc: BaseException) -> str:
    """К какой корзине отнести отказ по одной цели.

    Сначала по ИМЕНИ класса ошибки Telethon (устойчиво к формулировкам), затем по
    тексту — часть случаев Telethon отдаёт обычным ValueError без своего типа.
    """
    name = type(exc).__name__
    if name in ("UserPrivacyRestrictedError",):
        return FAIL_PRIVACY
    if name in ("UserNotMutualContactError",):
        return FAIL_NOT_MUTUAL
    if name in ("UserChannelsTooMuchError", "ChannelsTooMuchError"):
        return FAIL_TOO_MANY_CHATS
    if name in ("UserBannedInChannelError", "UserKickedError"):
        return FAIL_BANNED
    if name in ("UserBlockedError", "YouBlockedUserError"):
        return FAIL_BLOCKED
    # Ниже — то, что раньше целиком падало в «прочее» и пряталось от диагностики.
    if name in ("UserAlreadyParticipantError",):
        return FAIL_ALREADY_IN
    if name in ("UsersTooMuchError", "ChatTooMuchError"):
        return FAIL_CHAT_FULL
    if name in ("UserRestrictedError", "UserDeactivatedBanError"):
        return FAIL_RESTRICTED
    if name in ("BotGroupsBlockedError", "BotsTooMuchError"):
        return FAIL_IS_BOT
    if name in ("ChatAdminRequiredError", "ChatWriteForbiddenError",
                "ChannelPrivateError", "ChatIdInvalidError"):
        return FAIL_PERM
    if name in ("InputUserDeactivatedError", "UsernameNotOccupiedError",
                "UsernameInvalidError", "PeerIdInvalidError"):
        return FAIL_DEAD
    if "flood" in name.lower():
        return FAIL_FLOOD
    text = str(exc).lower()
    if any(m in text for m in _DEAD_MARKERS):
        return FAIL_DEAD
    if "too much" in text and "channel" in text:
        return FAIL_TOO_MANY_CHATS
    if "privacy" in text:
        return FAIL_PRIVACY
    # Текстовые варианты тех же случаев: Telethon часть из них отдаёт обычным
    # ValueError без своего типа, и они уходили в «прочее».
    if "already" in text and "participant" in text:
        return FAIL_ALREADY_IN
    if "too many members" in text or "users_too_much" in text:
        return FAIL_CHAT_FULL
    if "admin" in text and ("required" in text or "privileges" in text):
        return FAIL_PERM
    if "restricted" in text and "privacy" not in text:
        return FAIL_RESTRICTED
    if "bot" in text and ("can't" in text or "cannot" in text or "blocked" in text):
        return FAIL_IS_BOT
    return FAIL_OTHER


# Пакетное добавление одним запросом: opt-in, по умолчанию ВЫКЛЮЧЕНО.
#
# Сейчас на каждую цель уходит два обращения к Telegram: get_entity + отдельный
# InviteToChannelRequest. Между тем конструктор принимает СПИСОК пользователей, а
# в ответе отдаёт missing_invitees — то есть поимённо сообщает, кого не добавил.
# Один запрос вместо N это и вдвое меньше вызовов (значит меньше поводов для
# PeerFlood), и более точная атрибуция, чем поштучный обход.
#
# Почему всё-таки за флагом. Проверить это можно только на живом Telegram, а
# путь — самый баноопасный в продукте. Плюс у пакета есть своя цена: ошибка,
# относящаяся к ОДНОЙ цели (удалённый аккаунт, приватность в старом слое),
# роняет весь запрос. Поэтому при такой ошибке мы автоматически откатываемся на
# поштучный путь для этого же батча — пакет не имеет права стоить целей.
#
# Включение: INVITE_BULK_API=1 в окружении либо bulk=True у вызывающего.
_BULK_DEFAULT = (os.getenv("INVITE_BULK_API") or "").strip().lower() in ("1", "true", "yes", "on")

# Ошибки, которые относятся к чату или аккаунту целиком: их пакет не «чинит»,
# и откатываться на поштучный путь бессмысленно — результат будет тот же.
_BULK_FATAL_NAMES = frozenset({
    "PeerFloodError", "FloodWaitError", "ChatAdminRequiredError",
    "ChatWriteForbiddenError", "ChannelPrivateError", "UsersTooMuchError",
})


async def _try_bulk_invite(client, group, user_refs: list, note) -> dict | None:
    """Одним запросом добавить весь батч. None — пакет не подошёл, нужен поштучный.

    Возвращает None ТОЛЬКО когда ошибка относится к одной цели: тогда вызывающий
    повторяет батч поштучно и не теряет остальных. Ошибки про чат или аккаунт
    (флуд, нет прав, чат закрыт) возвращаются как результат — поштучный обход дал
    бы ровно то же, только N запросами вместо одного.
    """
    from telethon.tl.functions.channels import InviteToChannelRequest

    resolved: list = []
    errors: list[str] = []
    privacy_failed: list = []
    failed = 0
    for ref in user_refs:
        try:
            ent = await asyncio.wait_for(client.get_entity(ref), timeout=_ACTION_TIMEOUT)
        except Exception as e:
            # Нерезолвящаяся цель — это её собственная беда, а не повод ломать
            # пакет: просто не кладём её в запрос.
            kind = classify_invite_error(e)
            note(kind)
            failed += 1
            errors.append(f"{ref}: {FAIL_LABELS.get(kind, str(e)[:80])}")
            continue
        resolved.append((ref, ent))

    if not resolved:
        return {"ok": 0, "failed": failed, "errors": errors,
                "privacy_failed": privacy_failed, "peer_flood": False,
                "flood_wait": 0, "no_rights": False}

    async def _send():
        return await asyncio.wait_for(
            client(InviteToChannelRequest(channel=group,
                                          users=[e for _, e in resolved])),
            timeout=_ACTION_TIMEOUT * 2)

    try:
        try:
            res = await _send()
        except Exception as first:
            # Короткий флуд поштучный путь просто пережидает и продолжает.
            # Пакет обязан вести себя так же: иначе 20-секундная пауза вернулась
            # бы как flood_wait, исполнитель отправил бы аккаунт в cooldown и
            # вывел из круга — режим «быстрее» превращался бы в «теряем аккаунты».
            if (type(first).__name__ == "FloodWaitError"
                    and int(getattr(first, "seconds", 0) or 0) <= _MAX_FLOOD_INLINE):
                await asyncio.sleep(min(int(getattr(first, "seconds", 0) or 0), 60))
                res = await _send()
            else:
                raise
    except Exception as e:
        name = type(e).__name__
        if name not in _BULK_FATAL_NAMES:
            # Ошибка относится к одной из целей — откатываемся на поштучный путь.
            log.info("bulk invite: %s — откат на поштучный путь", name)
            return None
        if name == "PeerFloodError":
            note(FAIL_FLOOD)
            return {"ok": 0, "failed": failed + len(resolved), "errors":
                    errors + ["batch: peer flood — аккаунт ограничен"],
                    "privacy_failed": privacy_failed, "peer_flood": True,
                    "flood_wait": 0, "no_rights": False}
        if name == "FloodWaitError":
            note(FAIL_FLOOD)
            _fw = int(getattr(e, "seconds", 60) or 60)
            return {"ok": 0, "failed": failed + len(resolved),
                    "errors": errors + [f"batch: flood wait {_fw}s"],
                    "privacy_failed": privacy_failed, "peer_flood": False,
                    "flood_wait": _fw, "no_rights": False}
        if name == "ChatAdminRequiredError":
            return {"ok": 0, "failed": failed, "errors": errors + [
                "account error: у аккаунта нет прав добавлять участников"],
                "privacy_failed": privacy_failed, "peer_flood": False,
                "flood_wait": 0, "no_rights": True}
        note(FAIL_PERM)
        return {"ok": 0, "failed": failed + len(resolved),
                "errors": errors + [f"group error: {name}"],
                "privacy_failed": privacy_failed, "peer_flood": False,
                "flood_wait": 0, "no_rights": False}

    # Кого Telegram не добавил — говорит поимённо. Это ТОЧНЕЕ поштучного обхода:
    # там «не добавлен» приходилось выводить из отсутствия исключения.
    _raw_missing = list(getattr(res, "missing_invitees", None) or [])
    missing = set()
    for m in _raw_missing:
        # user_id — форма Telethon; на всякий случай принимаем и вложенного user.
        uid = getattr(m, "user_id", None)
        if uid is None:
            uid = getattr(getattr(m, "user", None), "id", None)
        if uid is not None:
            missing.add(int(uid))
    if _raw_missing and not missing:
        # Telegram сказал, что добавил не всех, но сопоставить их с целями не
        # вышло (незнакомая форма ответа). Засчитать успех всем — худшее, что
        # можно сделать: отчёт соврёт, дневной бюджет уйдёт на фантомов, а
        # промоут-трюк не получит тех, кого мог бы добрать. Откатываемся на
        # поштучный путь: он определяет исход по каждой цели сам.
        log.warning("bulk invite: %d missing_invitees не сопоставлены — "
                    "откат на поштучный путь", len(_raw_missing))
        return None
    ok = 0
    for ref, ent in resolved:
        if int(getattr(ent, "id", 0) or 0) in missing:
            failed += 1
            note(FAIL_PRIVACY)
            privacy_failed.append(ref)
            errors.append(f"{ref}: not added (privacy/limit — missing_invitee)")
        else:
            ok += 1
    return {"ok": ok, "failed": failed, "errors": errors,
            "privacy_failed": privacy_failed, "peer_flood": False,
            "flood_wait": 0, "no_rights": False}


async def invite_batch(
    session_string: str,
    _acc: dict | None,
    group_ref: str,
    user_refs: list[str | int],
    pace_mult: float = 1.0,
    bulk: bool | None = None,
) -> dict[str, Any]:
    """Добавить список пользователей (ID или @username) в группу.

    pace_mult — множитель пауз ВНУТРИ батча. Раньше здесь стояла жёсткая пауза
    2–4с на цель, не зависящая ни от режима темпа, ни от состояния аккаунта.
    Она же и была основной задержкой прогона: исполнитель аккуратно считал
    адаптивную паузу МЕЖДУ батчами, а внутри батча всё равно спалось 10–20с на
    пятёрку целей. Пользователь выбирал «быстро» и не видел разницы — настройка
    существовала, но ни на что не влияла.

    Возвращает:
      {"ok", "failed", "peer_flood", "flood_wait", "errors", "privacy_failed",
       "no_rights", "fail_kinds"} — fail_kinds это {корзина: счётчик}, чтобы
      вызывающему не приходилось угадывать причину разбором английского текста.
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

    # Множитель темпа: ограничиваем снизу, чтобы «быстро» не превращалось в
    # безпаузный долбёж — самый верный способ поймать PeerFlood на первом батче.
    _pm = max(0.35, float(pace_mult or 1.0))
    client = None
    ok, failed = 0, 0
    errors: list[str] = []
    peer_flood = False
    flood_wait = 0
    no_rights = False  # у ЭТОГО аккаунта нет прав админа (проблема аккаунта, не группы)
    fail_kinds: dict[str, int] = {}

    def _note(kind: str) -> None:
        fail_kinds[kind] = fail_kinds.get(kind, 0) + 1
    # Цели, которые отклонены приватностью/не-взаимностью — их прямой инвайт не
    # берёт, но может взять «добавление через выдачу админки» (промоут-трюк).
    privacy_failed: list = []

    try:
        client = await connect_client(session_string, _acc, "invite")
        group = await _resolve_group_entity(client, group_ref)

        _use_bulk = _BULK_DEFAULT if bulk is None else bool(bulk)
        if _use_bulk and len(user_refs) > 1:
            _bulk = await _try_bulk_invite(client, group, user_refs, _note)
            if _bulk is not None:
                ok += _bulk["ok"]
                failed += _bulk["failed"]
                errors.extend(_bulk["errors"])
                privacy_failed.extend(_bulk["privacy_failed"])
                await asyncio.sleep(random.uniform(2.0, 4.0) * _pm)
                return {"ok": ok, "failed": failed, "peer_flood": _bulk["peer_flood"],
                        "flood_wait": _bulk["flood_wait"], "errors": errors,
                        "privacy_failed": privacy_failed,
                        "no_rights": _bulk["no_rights"], "fail_kinds": fail_kinds,
                        "bulk": True}
            # None — пакет не подошёл (ошибка про одну цель): идём поштучно ниже,
            # НЕ теряя целей. Счётчики пакета при этом не применялись.

        for ref in user_refs:
            try:
                user = await asyncio.wait_for(client.get_entity(ref), timeout=_ACTION_TIMEOUT)
                _res = await asyncio.wait_for(
                    client(InviteToChannelRequest(channel=group, users=[user])),
                    timeout=_ACTION_TIMEOUT,
                )
                # ЧЕСТНЫЙ УСПЕХ: в современных слоях Telegram НЕ бросает
                # UserPrivacyRestricted, а тихо возвращает пользователя в
                # missing_invitees (не добавлен: приватность/премиум/лимит). Считать
                # такой ответ успехом = врать в отчёте И тратить дневной бюджет
                # аккаунта на фантомы. Не добавлен → в privacy_failed (как и явную
                # privacy-ошибку — уйдёт в промоут/контакт-фолбэк).
                if getattr(_res, "missing_invitees", None):
                    failed += 1
                    _note(FAIL_PRIVACY)
                    privacy_failed.append(ref)
                    errors.append(f"{ref}: not added (privacy/limit — missing_invitee)")
                else:
                    ok += 1
                await asyncio.sleep(random.uniform(2.0, 4.0) * _pm)
            except UserAlreadyParticipantError:
                ok += 1  # уже в группе = успех
            except UserPrivacyRestrictedError:
                failed += 1
                _note(FAIL_PRIVACY)
                privacy_failed.append(ref)
                errors.append(f"{ref}: privacy restricted")
            except UserNotMutualContactError:
                failed += 1
                _note(FAIL_NOT_MUTUAL)
                privacy_failed.append(ref)
                errors.append(f"{ref}: not mutual contact")
            except PeerFloodError:
                peer_flood = True
                failed += 1
                _note(FAIL_FLOOD)
                errors.append(f"{ref}: peer flood — аккаунт ограничен")
                break  # аккаунт перегрет, дальше не пробуем
            except FloodWaitError as e:
                _fw = int(getattr(e, "seconds", 60) or 60)
                failed += 1
                _note(FAIL_FLOOD)
                errors.append(f"{ref}: flood wait {_fw}s")
                if _fw > _MAX_FLOOD_INLINE:
                    # Длинный флуд: НЕ инвайтим во время активного флуда (эскалация).
                    flood_wait = _fw
                    break
                await asyncio.sleep(min(_fw, 60))
            except ChatAdminRequiredError:
                # Не про пользователя и НЕ про группу — про ПРАВА ЭТОГО аккаунта.
                # Для канала добавлять участников может только админ с правом
                # «Добавлять подписчиков». Раньше это метилось «group error» и
                # ВАЛИЛО всю операцию, хотя у ДРУГИХ аккаунтов (создатель) права
                # есть. Теперь помечаем no_rights → вызывающий выводит ЭТОТ аккаунт
                # из круга и продолжает аккаунтами с правами, а не рушит прогон.
                no_rights = True
                errors.append("account error: у аккаунта нет прав добавлять участников "
                              "(не админ / права ещё не применились)")
                break
            except UsersTooMuchError:
                failed += 1
                _note(FAIL_PERM)
                errors.append("group error: в чате достигнут лимит участников Telegram")
                break
            except (ChatWriteForbiddenError, ChannelPrivateError) as e:
                failed += 1
                _note(FAIL_PERM)
                errors.append(f"group error: нет доступа к чату ({type(e).__name__})")
                break  # нет прав/группа закрыта
            except Exception as e:
                # Сюда попадает большинство реальных отказов: мёртвый username,
                # удалённый аккаунт, «слишком много чатов», бан в чате. Раньше
                # все они уходили в отчёт сырым английским текстом Telethon и
                # считались одной корзиной «прочее».
                _kind = classify_invite_error(e)
                _note(_kind)
                failed += 1
                if _kind in (FAIL_PRIVACY, FAIL_NOT_MUTUAL):
                    privacy_failed.append(ref)
                errors.append(f"{ref}: {FAIL_LABELS.get(_kind, str(e)[:80])}")
                if _kind == FAIL_OTHER:
                    log.warning("invite failed: %s", e)

    except Exception as exc:
        log.warning("invite_batch connect/group error: %s", exc)
        errors.append(f"connect: {str(exc)[:100]}")
    finally:
        # client остаётся None, если упал сам connect_client — тогда disconnect
        # звать не на чем, и попытка давала лишний AttributeError в логе поверх
        # настоящей причины сбоя.
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                log_exc_swallow(log, "invite_batch: disconnect")

    return {"ok": ok, "failed": failed, "peer_flood": peer_flood,
            "flood_wait": flood_wait, "errors": errors,
            "privacy_failed": privacy_failed, "no_rights": no_rights,
            "fail_kinds": fail_kinds}


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
        # client остаётся None, если упал сам connect_client —
        # тогда disconnect звать не на чем, и попытка добавляла лишний
        # AttributeError в лог поверх настоящей причины сбоя.
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                log_exc_swallow(log, "channel_admin_status: disconnect")


async def check_membership_and_admin(session_string: str, _acc: dict | None,
                                     group_ref: str) -> dict[str, Any]:
    """Готовность ОДНОГО аккаунта к инвайту в группу (пре-флайт).

    Различает состояния, которые раньше сливались в «не ответил»:
      no_connect — сессия/сеть (аккаунт не подключился);
      group_bad  — группа недоступна/не резолвится;
      not_member — подключился, но НЕ участник группы (инвайтить не сможет);
      member     — участник, но не админ (прямой инвайт зависит от прав группы);
      admin      — админ; can_promote=есть право «Назначать админов» (промоутер).
    Только чтение, аккаунт не меняется.
    """
    from services.account_manager import connect_client
    from telethon.tl.functions.channels import GetParticipantRequest
    from telethon.tl.types import ChannelParticipantCreator, ChannelParticipantAdmin
    from telethon.errors import UserNotParticipantError

    client = None
    try:
        # Пре-проверка: подключаемся быстро, без 29с-ретрая AUTH_KEY_DUPLICATED
        # (иначе readiness-эндпоинт висит до таймаута шлюза).
        client = await connect_client(session_string, _acc, "invite", retry_auth_dup=False)
    except Exception as exc:
        return {"state": "no_connect", "error": str(exc)[:120]}
    try:
        try:
            group = await _resolve_group_entity(client, group_ref)
        except Exception as exc:
            return {"state": "group_bad", "error": str(exc)[:120]}
        try:
            part = await asyncio.wait_for(
                client(GetParticipantRequest(channel=group, participant="me")),
                timeout=_ACTION_TIMEOUT)
        except UserNotParticipantError:
            return {"state": "not_member", "can_promote": False, "can_invite": False}
        p = part.participant
        if isinstance(p, ChannelParticipantCreator):
            return {"state": "admin", "creator": True, "can_promote": True, "can_invite": True}
        if isinstance(p, ChannelParticipantAdmin):
            r = getattr(p, "admin_rights", None)
            _add_admins = bool(getattr(r, "add_admins", False))
            return {"state": "admin" if _add_admins else "member",
                    "can_promote": _add_admins,
                    "can_invite": bool(getattr(r, "invite_users", False))}
        return {"state": "member", "can_promote": False, "can_invite": False}
    except Exception as exc:
        return {"state": "error", "error": str(exc)[:120]}
    finally:
        # client остаётся None, если упал сам connect_client —
        # тогда disconnect звать не на чем, и попытка добавляла лишний
        # AttributeError в лог поверх настоящей причины сбоя.
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                log_exc_swallow(log, "check_membership_and_admin: disconnect")


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
    no_rights = False  # у вызывающего нет add_admins — проблема аккаунта, не группы
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
                # У ЭТОГО аккаунта нет add_admins — проблема аккаунта, не группы.
                # no_rights → вызывающий выдаёт ему add_admins и продолжает, а не
                # рушит операцию (как в invite_batch).
                no_rights = True
                errors.append("account error: нет прав add_admins для промоут-трюка")
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
        # client остаётся None, если упал сам connect_client —
        # тогда disconnect звать не на чем, и попытка добавляла лишний
        # AttributeError в лог поверх настоящей причины сбоя.
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                log_exc_swallow(log, "add_via_promote: disconnect")

    return {"ok": ok, "failed": failed, "peer_flood": peer_flood,
            "flood_wait": flood_wait, "errors": errors, "no_rights": no_rights}


async def export_group_invite_link(session_string: str, _acc: dict | None,
                                   group_ref: str) -> str:
    """Экспортировать ссылку-приглашение группы/канала. Возвращает '' при сбое.

    Использует общий резолвер группы (публичный @/приват-ссылка), поэтому работает
    и для @username, и для t.me/+HASH. Нужны права «Пригласительные ссылки» у
    аккаунта (обычно есть у создателя/админа).
    """
    from services.account_manager import connect_client
    from telethon.tl.functions.messages import ExportChatInviteRequest

    client = None
    try:
        client = await connect_client(session_string, _acc, "invite")
        group = await _resolve_group_entity(client, group_ref)
        result = await asyncio.wait_for(
            client(ExportChatInviteRequest(peer=group)), timeout=_ACTION_TIMEOUT)
        return getattr(result, "link", "") or ""
    except Exception as e:
        log.warning("export_group_invite_link error: %s", e)
        return ""
    finally:
        # client остаётся None, если упал сам connect_client —
        # тогда disconnect звать не на чем, и попытка добавляла лишний
        # AttributeError в лог поверх настоящей причины сбоя.
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                log_exc_swallow(log, "export_group_invite_link: disconnect")


# Шаблон сообщения со ссылкой-приглашением (по умолчанию). {link} обязателен.
DEFAULT_LINK_INVITE_TEXT = "Привет! Приглашаю в наш чат: {link}"


async def invite_via_link_batch(session_string: str, _acc: dict | None,
                                invite_link: str, user_refs: list[str | int],
                                message_text: str | None = None,
                                pace_mult: float = 1.0) -> dict[str, Any]:
    """«Мягкий» инвайт: разослать пользователям ссылку-приглашение в ЛС.

    Не добавляет насильно — отправляет каждому цель ссылку на вступление, человек
    заходит сам. Полностью обходит приватность «кто может добавлять» (пользователь
    вступает добровольно) и наименее опасен для аккаунтов из всех методов, но
    зависит от готовности человека кликнуть. `ok` здесь = ссылка ДОСТАВЛЕНА (не
    гарантирует вступление). Ограничитель — PeerFlood (лимит на рассылку в ЛС).

    Текст разворачивается spintax'ом ДЛЯ КАЖДОЙ ЦЕЛИ отдельно. Раньше он
    вычислялся один раз до цикла, и весь флот слал побайтово одинаковое
    сообщение сотням людей — самая узнаваемая подпись спама, какая бывает, и
    именно в методе, который продукт называет «безопаснее всего». Движок spintax
    в проекте есть и используется рассылкой; здесь он просто не вызывался.
    """
    from services.account_manager import send_dm
    from services.dm_engine import expand_spintax

    tpl = (message_text or DEFAULT_LINK_INVITE_TEXT)
    if "{link}" not in tpl:
        tpl = tpl.rstrip() + "\n{link}"

    # Тот же смысл, что у invite_batch: режим темпа обязан влиять на паузы, но не
    # обнулять их — безпаузная рассылка в ЛС ловит PeerFlood мгновенно.
    _pm = max(0.35, float(pace_mult or 1.0))

    ok, failed = 0, 0
    errors: list[str] = []
    privacy_failed: list = []
    peer_flood = False
    flood_wait = 0
    fail_kinds: dict[str, int] = {}

    def _note(kind: str) -> None:
        fail_kinds[kind] = fail_kinds.get(kind, 0) + 1

    # Плейсхолдер прячем от spintax'а меткой без фигурных скобок: для лексера
    # `{link}` — это группа из одного варианта, и он честно раскрывает её в слово
    # «link», съедая место под ссылку. Ссылку тоже нельзя подставлять до разбора:
    # в хэше приват-ссылки встречаются символы, ломающие лексер.
    _LINK_SLOT = "\x00LINKSLOT\x00"
    _tpl_masked = tpl.replace("{link}", _LINK_SLOT)

    for ref in user_refs:
        text = expand_spintax(_tpl_masked).replace(_LINK_SLOT, invite_link)
        try:
            res = await asyncio.wait_for(
                send_dm(session_string, str(ref), text, _acc=_acc), timeout=45)
        except Exception as e:
            failed += 1
            _note(classify_invite_error(e))
            errors.append(f"{ref}: {str(e)[:80]}")
            # Пауза нужна и после отказа. Раньше sleep стоял только на успешной
            # ветке, поэтому серия отказов (а приватность отвечает быстро)
            # прогоняла цикл без единой задержки — ровно тот всплеск частоты,
            # из-за которого прилетает PeerFlood.
            await asyncio.sleep(random.uniform(2.5, 5.0) * _pm)
            continue
        if res.get("ok"):
            ok += 1
            await asyncio.sleep(random.uniform(2.5, 5.0) * _pm)
            continue
        # Ошибки send_dm: privacy / peer_flood / flood_wait / прочее.
        failed += 1
        if res.get("peer_flood"):
            peer_flood = True
            _note(FAIL_FLOOD)
            errors.append(f"{ref}: peer flood")
            break
        _fw = int(res.get("flood_wait") or 0)
        if _fw:
            _note(FAIL_FLOOD)
            if _fw > _MAX_FLOOD_INLINE:
                flood_wait = _fw
                errors.append(f"{ref}: flood wait {_fw}s")
                break
            await asyncio.sleep(min(_fw, 60))
            errors.append(f"{ref}: flood wait {_fw}s (переждали)")
            continue
        # Причина отказа — той же классификацией, что у прямого инвайта: иначе
        # отчёт по «ссылке в ЛС» остаётся в корзине «прочее», хотя в нём ровно
        # те же случаи (приватность ЛС, мёртвая цель, блокировка).
        _err = str(res.get("error") or "")
        _kind = classify_invite_error(ValueError(_err)) if _err else FAIL_OTHER
        if "приватн" in _err.lower() or "privacy" in _err.lower():
            _kind = FAIL_PRIVACY
        _note(_kind)
        if _kind in (FAIL_PRIVACY, FAIL_NOT_MUTUAL):
            privacy_failed.append(ref)
        errors.append(f"{ref}: {FAIL_LABELS.get(_kind) if _kind != FAIL_OTHER else _err[:80]}")
        await asyncio.sleep(random.uniform(2.5, 5.0) * _pm)

    return {"ok": ok, "failed": failed, "peer_flood": peer_flood,
            "flood_wait": flood_wait, "errors": errors,
            "privacy_failed": privacy_failed, "no_rights": False,
            "fail_kinds": fail_kinds}


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
    no_rights = False  # у ЭТОГО аккаунта нет прав админа (проблема аккаунта, не группы)
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
            if peer_flood or flood_wait or group_broken or no_rights:
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
                # Права — проблема ЭТОГО аккаунта, не группы. no_rights → вызывающий
                # выводит аккаунт из круга и продолжает аккаунтами с правами (как в
                # invite_batch), а не рушит всю операцию.
                no_rights = True
                errors.append("account error: у аккаунта нет прав добавлять участников "
                              "(не админ / права ещё не применились)")
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
        # client остаётся None, если упал сам connect_client —
        # тогда disconnect звать не на чем, и попытка добавляла лишний
        # AttributeError в лог поверх настоящей причины сбоя.
        if client is not None:
            try:
                await client.disconnect()
            except Exception as e:
                log_exc_swallow(log, "invite_by_phones: disconnect")

    return {"ok": ok, "failed": failed, "peer_flood": peer_flood,
            "flood_wait": flood_wait, "errors": errors, "no_rights": no_rights,
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


def validate_group_ref(text: str, strict: bool = True) -> tuple[bool, str]:
    """Похоже ли это на ссылку/имя чата. (ok, человеческая причина отказа).

    Раньше проверки не было вовсе: `normalize_telegram_join_ref` для ЛЮБОГО
    текста возвращает ("public", текст), поэтому опечатка вроде «мой чат»
    доходила до исполнителя как @мой чат. Дальше операция вставала в очередь,
    клеймила аккаунты, подключалась ими и падала на резолве — прогон и суточные
    лимиты сгорали на том, что видно на входе за миллисекунду.

    Проверяем ФОРМУ, а не существование чата: существование выясняет пре-флайт
    живым аккаунтом, здесь же ловим заведомо негодный ввод.

    strict=True — для точек ВВОДА (бот, мини-апп): человек прямо сейчас смотрит
    на поле и может исправить опечатку.
    strict=False — для ИСПОЛНИТЕЛЯ: там операция уже стоит в очереди, могла быть
    создана прежней версией или внешним API, и отказ по спорной форме превратил
    бы рабочий прогон в проваленный. В мягком режиме отсекается только заведомо
    невозможное — пустое, слишком длинное, с пробелами или нелатиницей, то есть
    введённое НАЗВАНИЕ чата вместо ссылки.
    """
    raw = (text or "").strip()
    if not raw:
        return False, "Укажите группу или канал"
    if len(raw) > 512:
        return False, "Слишком длинная ссылка на чат"
    # Числовой id чата (в т.ч. супергруппа -100…)
    if re.fullmatch(r"-?\d{5,20}", raw):
        return True, ""
    from services.account_manager import normalize_telegram_join_ref

    kind, value = normalize_telegram_join_ref(raw)
    if not value:
        return False, "Укажите группу или канал"
    if kind == "invite":
        # Хэш приват-ссылки: base64url, у Telegram он заметно длиннее 8 символов.
        if re.fullmatch(r"[A-Za-z0-9_-]{8,64}", value):
            return True, ""
        return False, ("Ссылка-приглашение выглядит обрезанной. Скопируйте её целиком: "
                       "https://t.me/+XXXXXXXX")
    # Публичное имя: буквы/цифры/подчёркивание, начинается с буквы.
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", value):
        return True, ""
    if not strict:
        # Мягкий режим: не наше дело придираться к длине или дефису — Telegram
        # ответит сам. Отсекаем только то, чем ссылка быть не может.
        if re.search(r"\s", value) or re.search(r"[^\x00-\x7F]", value):
            return False, ("Похоже, это название чата, а не ссылка. Нужен @username, "
                           "ссылка t.me/… или числовой ID чата")
        return True, ""
    if re.search(r"[А-Яа-яЁё]", value):
        return False, ("Похоже, вы ввели название чата, а не ссылку. Нужен @username, "
                       "ссылка t.me/… или числовой ID чата")
    return False, ("Не похоже на чат: нужен @username (латиница, 4–32 символа), "
                   "ссылка t.me/… , приват-ссылка t.me/+… или числовой ID")


def parse_user_refs(text: str, limit: int = MAX_INVITE_LIST) -> list[str]:
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
    return list(dict.fromkeys(refs))[:limit]


def parse_phones(text: str, limit: int = MAX_INVITE_LIST) -> list[str]:
    """Парсинг номеров телефонов: один номер на строку (или через запятую/;).

    Разделяем ТОЛЬКО по переносам/запятым/точке-с-запятой, а НЕ по пробелам —
    иначе «+7 999 123 45 67» разбивается на куски и теряется. Пробелы/скобки/
    дефисы внутри номера вычищаются."""
    phones: list[str] = []
    for token in re.split(r"[,;\n\r]+", text.strip()):
        token = re.sub(r"[^\d+]", "", token)
        if len(token) >= 10:
            if not token.startswith("+"):
                token = "+" + token
            phones.append(token)
    return list(dict.fromkeys(phones))[:limit]


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
        # phones_src уже нормализованы ("+"+цифры). Склеиваем через ПЕРЕНОС, а не
        # пробел: parse_phones теперь делит по переносам/запятым (не по пробелам),
        # иначе все номера слиплись бы в одно гигантское число.
        "phones": parse_phones("\n".join(phones_src)),
        "user_refs": parse_user_refs("\n".join(refs_src)),
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
