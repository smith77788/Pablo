"""Жалоба (report_peer) по приватной инвайт-ссылке t.me/+HASH реально работает.

Жалоба владельца (операция #916): «Жалоба и Strike падают сразу же». В логе у
каждого аккаунта — `Cannot find any entity corresponding to
"http://T.me/+FnUVE718eCE5MDIy"`. Причина: цель — приватная инвайт-ссылка
(joinchat/+hash), а reporter_engine резолвил её голым get_entity, который такую
ссылку НЕ находит (Telegram не резолвит приватный инвайт по username-пути).

Эти тесты сторожат, что:
  * инвайт-ссылка разбирается через ImportChatInvite/CheckChatInvite, а не
    get_entity (который для неё заведомо падает), и жалоба уходит;
  * аккаунт, уже состоящий в группе, берёт сущность через CheckChatInvite;
  * когда жалоба по приватной группе этим аккаунтом объективно невозможна,
    владелец получает ПОНЯТНУЮ русскую причину, а не сырой английский стектрейс.

На СТАРОМ коде (единственный get_entity по ссылке) первый тест падает: entity не
резолвится, report_peer возвращает ok=False с «Cannot find any entity …».

Гоняется под заглушкой telethon из conftest; реальная сеть не нужна.
"""
from __future__ import annotations

import pytest
from services import reporter_engine as rep
from telethon.errors import FloodWaitError

_INVITE = "https://t.me/+FnUVE718eCE5MDIy"


class _Chat:
    """Минимальная сущность чата/канала, годная как peer для ReportPeerRequest."""

    def __init__(self, cid: int = 123) -> None:
        self.id = cid
        self.access_hash = 7
        self.restricted = False


class _ImportResult:
    """Ответ ImportChatInviteRequest — несёт вступивший чат в .chats."""

    def __init__(self, chat: _Chat) -> None:
        self.chats = [chat]


class _CheckAlready:
    """ChatInviteAlready-подобный ответ CheckChatInvite — аккаунт уже в группе."""

    def __init__(self, chat: _Chat) -> None:
        self.chat = chat


class _CheckPreview:
    """ChatInvite (превью) — у не-участника нет .chat, отрепортить нельзя."""

    def __init__(self) -> None:
        self.title = "Приватная группа"
        self.participants_count = 42


class _FakeInviteClient:
    """Фейковый клиент: get_entity по ссылке ЗАВЕДОМО падает (как в проде);
    client(request) отдаёт заранее заготовленные ответы по порядку вызовов."""

    def __init__(self, responses: list, *, get_entity_ok: bool = False) -> None:
        self._responses = list(responses)
        self._get_entity_ok = get_entity_ok
        self.get_entity_calls = 0
        self.get_input_entity_calls = 0
        self.call_count = 0
        self.disconnected = False

    async def connect(self):
        return True

    async def get_input_entity(self, ref):
        self.get_input_entity_calls += 1
        raise ValueError("no cached entity")

    async def get_entity(self, ref):
        self.get_entity_calls += 1
        if self._get_entity_ok:
            return f"entity:{ref}"
        raise ValueError(f'Cannot find any entity corresponding to "{ref}"')

    async def __call__(self, request):
        self.call_count += 1
        item = self._responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    async def disconnect(self):
        self.disconnected = True


def _install(monkeypatch, client: _FakeInviteClient) -> None:
    import services.account_manager as am
    monkeypatch.setattr(am, "_make_client", lambda *a, **k: client, raising=False)

    async def _fast_sleep(_s):
        return None
    monkeypatch.setattr(rep.asyncio, "sleep", _fast_sleep, raising=False)


@pytest.mark.asyncio
async def test_invite_link_joins_then_reports(monkeypatch):
    """t.me/+HASH: вступаем (ImportChatInvite даёт чат) и жалоба уходит.

    get_entity по ссылке НЕ зовётся — именно его промах ломал операцию."""
    chat = _Chat()
    # [ImportChatInvite -> чат, ReportPeer -> ok]
    client = _FakeInviteClient([_ImportResult(chat), True])
    _install(monkeypatch, client)

    res = await rep.report_peer("sess", {"id": 1}, _INVITE, "drugs")
    assert res["ok"] is True, res
    assert client.get_entity_calls == 0  # ссылку НЕ гнали через get_entity
    assert client.call_count == 2        # import + report
    assert client.disconnected is True


@pytest.mark.asyncio
async def test_invite_link_already_member_uses_check(monkeypatch):
    """Аккаунт уже в группе: ImportChatInvite падает → CheckChatInvite даёт чат."""
    chat = _Chat()
    # [ImportChatInvite -> падает(уже участник), CheckChatInvite -> .chat, ReportPeer -> ok]
    client = _FakeInviteClient([
        RuntimeError("USER_ALREADY_PARTICIPANT"),
        _CheckAlready(chat),
        True,
    ])
    _install(monkeypatch, client)

    res = await rep.report_peer("sess", {"id": 1}, _INVITE, "spam")
    assert res["ok"] is True, res
    assert client.get_entity_calls == 0


@pytest.mark.asyncio
async def test_invite_preview_only_gives_clear_russian_error(monkeypatch):
    """Не состоим и вступить нельзя (одно превью) → понятная русская причина,
    а НЕ сырой «Cannot find any entity …»."""
    # [ImportChatInvite -> падает, CheckChatInvite -> превью без .chat]
    client = _FakeInviteClient([
        RuntimeError("join failed"),
        _CheckPreview(),
    ])
    _install(monkeypatch, client)

    res = await rep.report_peer("sess", {"id": 1}, _INVITE, "spam")
    assert res["ok"] is False
    assert "Cannot find any entity" not in (res["error"] or "")
    assert "невозможна" in res["error"].lower()
    # Кириллица в тексте — значит владельцу ушла русская причина.
    assert any("а" <= ch.lower() <= "я" for ch in res["error"])


@pytest.mark.asyncio
async def test_invite_hash_expired_is_honest_not_retryable(monkeypatch):
    """Просроченная/битая ссылка → понятно и БЕЗ повтора (retryable=False)."""
    client = _FakeInviteClient([RuntimeError("INVITE_HASH_EXPIRED")])
    _install(monkeypatch, client)

    res = await rep.report_peer("sess", {"id": 1}, _INVITE, "spam")
    assert res["ok"] is False
    assert res["retryable"] is False
    assert "просроч" in res["error"].lower() or "недействительн" in res["error"].lower()


@pytest.mark.asyncio
async def test_floodwait_on_invite_resolve_propagates(monkeypatch):
    """FloodWait на вступлении не тонет в «unresolvable» — исполнитель должен
    увидеть флуд и поставить cooldown, а не ретайрить аккаунт как неудачника."""
    fw = FloodWaitError.__new__(FloodWaitError)
    BaseException.__init__(fw, "flood 200s")
    fw.seconds = 200
    client = _FakeInviteClient([fw])
    _install(monkeypatch, client)

    res = await rep.report_peer("sess", {"id": 1}, _INVITE, "spam")
    assert res["ok"] is False
    assert res.get("flood_wait") == 200
    assert res.get("retryable") is True


@pytest.mark.asyncio
async def test_public_username_still_uses_get_entity(monkeypatch):
    """Регресс: публичный @username не трогает инвайт-путь, идёт прежней дорогой."""
    client = _FakeInviteClient([True], get_entity_ok=True)  # только ReportPeer
    _install(monkeypatch, client)

    res = await rep.report_peer("sess", {"id": 1}, "@somechannel", "spam")
    assert res["ok"] is True, res
    # публичную цель резолвим через get_input_entity/get_entity (не инвайт-путь)
    assert client.get_input_entity_calls == 1
    assert client.get_entity_calls == 1
    assert client.call_count == 1  # только report
