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
