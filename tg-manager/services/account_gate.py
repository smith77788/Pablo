"""Правила входа к Telegram, общие для ВСЕХ путей: ограниченный аккаунт и счёт
действий мимо очереди операций.

ЗАЧЕМ. Очередь операций бережёт флот двумя правилами: аккаунт под спам-блоком
или заморозкой в работу не берётся (словарь `account_status`), а рискованные
действия считаются в суточный бюджет (`account_budget`). Но оба правила стоят в
подборе аккаунтов и в исполнителях. Действие, начатое мимо очереди — кнопкой
бота, из мини-приложения, фоновым циклом, с аккаунтом, выбранным вручную, — их
не проходит: аккаунт, который Telegram уже пометил за спам, вступает в чаты и
шлёт жалобы, а его действия в бюджет не попадают, и очередь считает его
свежим.

Здесь эти два правила в виде, пригодном для клиента аккаунта
(`account_manager._wrap_client_flood_floor`), через который идёт любой запрос:

  • `refusal(...)` — причина по-русски, если запрос нельзя отправлять с
    ограниченного аккаунта; иначе None;
  • `note_direct_action(...)` — действие мимо очереди операций, которое надо
    дописать в суточный бюджет (пишет `pacing_store`, у клиента нет пула).

Модуль без сети и без Telethon: запросы узнаются по имени класса.
"""
from __future__ import annotations

import logging

from services import account_status as _st

log = logging.getLogger(__name__)

# Официальный @SpamBot: через него аккаунт проверяет и снимает ограничение,
# поэтому писать ему можно всегда.
SPAMBOT_USER_ID = 178220800

# id аккаунта → статус из tg_accounts. Перечитывается вместе с картой
# транспорта (account_manager.prime_account_transport) и сразу обновляется
# там, где статус ставит сам процесс (flood_engine.record_peer_flood).
_STATUS: dict[int, str] = {}

# Спам-блок: Telegram сам оставляет такому аккаунту чтение, вступления, свои
# каналы и ответы тем, кто написал первым. Закрыто то, за что аккаунт помечен
# и что продлевает блок: приглашения, импорт контактов и жалобы (жалоба от
# помеченного аккаунта не засчитывается и ухудшает его же счёт).
_REFUSED_SPAMBLOCK = frozenset({
    "InviteToChannelRequest", "AddChatUserRequest", "ImportContactsRequest",
    "ReportRequest", "ReportPeerRequest", "ReportSpamRequest",
})
# Заморозка: аккаунт только читает. Любое действие вернёт отказ Telegram, а
# поток отказов — довод против аккаунта при разборе апелляции.
_REFUSED_FROZEN = _REFUSED_SPAMBLOCK | frozenset({
    "JoinChannelRequest", "ImportChatInviteRequest",
    "CreateChannelRequest", "CreateChatRequest", "SendReactionRequest",
})
# Сообщения замороженному аккаунту закрыты только в личку людям: «Избранное»
# и @SpamBot (через него подают апелляцию) должны работать.
_REFUSED_FROZEN_TO_USERS = frozenset({
    "SendMessageRequest", "SendMediaRequest", "SendMultiMediaRequest",
    "ForwardMessagesRequest",
})

# Вид запроса → действие суточного бюджета (имена из account_budget).
_BUDGET_ACTION = {
    "JoinChannelRequest": "join", "ImportChatInviteRequest": "join",
    "LeaveChannelRequest": "leave",
    "CreateChannelRequest": "create_channel", "CreateChatRequest": "create_group",
    "SendReactionRequest": "reaction",
    "ReportRequest": "report", "ReportPeerRequest": "report",
    "ReportSpamRequest": "report",
}

# (аккаунт, действие) → сколько ещё не записано в бюджет.
_direct_unsaved: dict[tuple[int, str], int] = {}


# Аккаунт → вид запроса, на который Telegram ответил PEER_FLOOD; ждёт записи
# статуса в базу (pacing_store.flush_peer_floods).
_peer_flood_unsaved: dict[int, str] = {}
# Столько же, сколько у исполнителей операций (flood_engine.record_peer_flood).
PEER_FLOOD_COOLDOWN_S = 48 * 3600


class AccountRestrictedError(Exception):
    """Запрос не отправлен: аккаунт под ограничением Telegram."""

    def __init__(self, account_id: int, status: str, request_name: str):
        self.account_id = account_id
        self.status = status
        self.request_name = request_name
        super().__init__(
            f"Аккаунт под ограничением Telegram ({_st.ru_label(status)}) — "
            "действие не отправлено, чтобы не усугубить ограничение. "
            "Сначала снимите ограничение через @SpamBot.")


def in_operation() -> bool:
    """Запрос идёт изнутри операции очереди (её действия в бюджет пишет
    исполнитель — второй раз считать нельзя)."""
    try:
        from services import logger as _lg
        return bool(_lg._op_id.get())
    except Exception:
        return False


def set_status(account_id, status) -> None:
    try:
        aid = int(account_id)
    except (TypeError, ValueError):
        return
    s = str(status or "active").strip().lower()
    if s in _st.RESTRICTED_STATUSES:
        _STATUS[aid] = s
    else:
        _STATUS.pop(aid, None)


def forget(account_id) -> None:
    try:
        _STATUS.pop(int(account_id), None)
    except (TypeError, ValueError):
        pass


def _to_a_person(request) -> bool:
    """Запрос адресован человеку в личку (не каналу, не группе, не себе, не
    @SpamBot). Чего не понимаем — считаем НЕ личкой: запрет по догадке ломал
    бы работающее."""
    peer = getattr(request, "peer", None)
    if peer is None:
        peer = getattr(request, "to_peer", None)
    if type(peer).__name__ not in ("InputPeerUser", "InputUser", "PeerUser"):
        return False
    return getattr(peer, "user_id", None) != SPAMBOT_USER_ID


def refusal(account_id, request) -> AccountRestrictedError | None:
    """Ошибка, если этот запрос нельзя отправлять с этого аккаунта; иначе None."""
    try:
        status = _STATUS.get(int(account_id))
    except (TypeError, ValueError):
        return None
    if not status:
        return None
    items = request if isinstance(request, (list, tuple)) else [request]
    for r in items:
        name = type(r).__name__
        if status == "frozen":
            hit = name in _REFUSED_FROZEN or (
                name in _REFUSED_FROZEN_TO_USERS and _to_a_person(r))
        else:
            hit = name in _REFUSED_SPAMBLOCK
        if hit:
            try:
                from services import metrics as _m
                _m.inc("infragram_restricted_account_refusals_total", {"status": status})
            except Exception:
                pass
            return AccountRestrictedError(int(account_id), status, name)
    return None


def note_direct_action(account_id, request) -> None:
    """Запрос прошёл МИМО очереди операций — запомнить для суточного бюджета."""
    try:
        aid = int(account_id)
    except (TypeError, ValueError):
        return
    items = request if isinstance(request, (list, tuple)) else [request]
    for r in items:
        action = _BUDGET_ACTION.get(type(r).__name__)
        if action:
            _direct_unsaved[(aid, action)] = _direct_unsaved.get((aid, action), 0) + 1


def drain_direct_actions() -> list[tuple[int, str, int]]:
    out = [(aid, action, n) for (aid, action), n in _direct_unsaved.items() if n > 0]
    _direct_unsaved.clear()
    return out


def requeue_direct_actions(items) -> None:
    for aid, action, n in items:
        key = (int(aid), str(action))
        _direct_unsaved[key] = _direct_unsaved.get(key, 0) + int(n)


def note_peer_flood(account_id, request_name: str = "") -> None:
    """Telegram ответил PEER_FLOOD: аккаунт помечен за спам.

    Исполнители операций записывают это сами (flood_engine.record_peer_flood),
    но ошибку ловят 17 файлов, а записывают четыре. На остальных путях экран
    показывал ошибку, аккаунт оставался «активным» и уходил в следующую
    рассылку или приглашения прямо под блок — это и удлиняет ограничение.

    Здесь то же самое делает дверь: блок действует сразу в этом процессе
    (отказ в `refusal`, пауза в подборе аккаунтов), а в базу статус дописывает
    pacing_store. Повторная запись поверх исполнителя безвредна: статус тот
    же, пауза только продлевается."""
    try:
        aid = int(account_id)
    except (TypeError, ValueError):
        return
    if aid <= 0:
        return
    _STATUS[aid] = "spamblock"
    _peer_flood_unsaved[aid] = str(request_name or "")[:80]
    try:
        import time as _time
        from services import flood_engine as _fe
        state = _fe.get_account_state(aid)
        state.cooldown_until = max(state.cooldown_until,
                                   _time.monotonic() + PEER_FLOOD_COOLDOWN_S)
    except Exception:
        log.debug("note_peer_flood: пауза в памяти не поставлена", exc_info=True)
    try:
        from services import metrics as _m
        _m.inc("infragram_flood_events_total",
               {"kind": "peer_flood_at_door", "action": "direct"})
    except Exception:
        pass


def drain_peer_floods() -> list[tuple[int, str]]:
    out = list(_peer_flood_unsaved.items())
    _peer_flood_unsaved.clear()
    return out


def requeue_peer_floods(items) -> None:
    for aid, name in items:
        _peer_flood_unsaved.setdefault(int(aid), str(name))


# Аккаунт → имя ошибки Telegram «аккаунт заблокирован»; ждёт записи в базу
# (pacing_store.flush_bans).
_ban_unsaved: dict[int, str] = {}
# Только однозначные ответы «аккаунта больше нет». Отзыв ключа сессии
# (AUTH_KEY_UNREGISTERED) сюда НЕ входит намеренно: его же даёт наш
# собственный сбой транспорта (сессия с адресом не того дата-центра), и на
# общем входе, где контекста нет, живой аккаунт выключался бы навсегда.
# Мёртвую сессию по-прежнему определяют исполнители и монитор аккаунтов.
_BAN_ERRORS = frozenset({
    "UserDeactivatedBanError", "UserDeactivatedError", "PhoneNumberBannedError",
})


def note_error(account_id, request_name: str, exc: BaseException) -> None:
    """Дверь к Telegram получила ошибку. Отметить то, что касается АККАУНТА,
    а не запроса: спам-блок и блокировку. Никогда не бросает."""
    name = type(exc).__name__
    if name == "PeerFloodError":
        note_peer_flood(account_id, request_name)
    elif name in _BAN_ERRORS:
        note_ban(account_id, name)


def note_ban(account_id, error_name: str = "") -> None:
    """Telegram ответил, что аккаунт заблокирован или удалён.

    Исполнители операций выключают такой аккаунт сразу, но ошибку ловят и
    экраны, и фоновые циклы, которые ничего не записывают: до следующего
    обхода монитора аккаунт оставался «активным», подбор отдавал его в новые
    операции, и каждая падала с ошибкой у пользователя."""
    try:
        aid = int(account_id)
    except (TypeError, ValueError):
        return
    if aid <= 0:
        return
    _ban_unsaved[aid] = str(error_name or "")[:60]
    try:  # подбор аккаунтов в этом процессе перестаёт брать его сразу
        import time as _time
        from services import flood_engine as _fe
        state = _fe.get_account_state(aid)
        state.cooldown_until = max(state.cooldown_until,
                                   _time.monotonic() + PEER_FLOOD_COOLDOWN_S)
    except Exception:
        log.debug("note_ban: пауза в памяти не поставлена", exc_info=True)
    try:
        from services import metrics as _m
        _m.inc("infragram_session_deaths_total", {"kind": "ban_at_door"})
    except Exception:
        pass


def drain_bans() -> list[tuple[int, str]]:
    out = list(_ban_unsaved.items())
    _ban_unsaved.clear()
    return out


def requeue_bans(items) -> None:
    for aid, name in items:
        _ban_unsaved.setdefault(int(aid), str(name))
