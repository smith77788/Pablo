"""Пакетное добавление одним запросом (opt-in) и его откат на поштучный путь.

ЗАЧЕМ. На каждую цель уходит два обращения к Telegram: get_entity + отдельный
InviteToChannelRequest. Конструктор принимает СПИСОК и в ответе отдаёт
missing_invitees — поимённо сообщает, кого не добавил. Один запрос вместо N это
вдвое меньше вызовов (меньше поводов для PeerFlood) и более точная атрибуция.

ПОЧЕМУ ЗА ФЛАГОМ. Проверить на живом Telegram здесь нечем, а путь — самый
баноопасный в продукте. У пакета есть своя цена: ошибка про ОДНУ цель роняет
весь запрос. Поэтому такая ошибка откатывает батч на поштучный путь, и цели не
теряются. По умолчанию режим выключен: включается INVITE_BULK_API=1 или bulk=True.
"""
from __future__ import annotations

import asyncio

import pytest

from services import mass_inviter_engine as mie


class _Ent:
    def __init__(self, uid):
        self.id = uid


class _Missing:
    def __init__(self, uid):
        self.user_id = uid


class _Res:
    def __init__(self, missing=()):
        self.missing_invitees = [_Missing(u) for u in missing]


class _Client:
    """Клиент-стенд: считает вызовы, чтобы проверить «один запрос вместо N»."""

    def __init__(self, missing=(), raise_on_invite=None, unresolvable=()):
        self.missing = missing
        self.raise_on_invite = raise_on_invite
        self.unresolvable = set(unresolvable)
        self.resolve_calls = 0
        self.invite_calls = 0

    async def get_entity(self, ref):
        self.resolve_calls += 1
        if str(ref) in self.unresolvable:
            raise ValueError(f'No user has "{ref}" as username')
        return _Ent(abs(hash(str(ref))) % 100000)

    async def __call__(self, request):
        self.invite_calls += 1
        if self.raise_on_invite is not None:
            raise self.raise_on_invite
        return _Res(self.missing)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _bulk(client, refs):
    kinds = {}
    res = _run(mie._try_bulk_invite(client, object(), refs,
                                    lambda k: kinds.__setitem__(k, kinds.get(k, 0) + 1)))
    return res, kinds


def _named(name, **attrs):
    return type(name, (Exception,), attrs)()


def test_whole_batch_goes_in_one_request():
    c = _Client()
    refs = [f"@u{i}" for i in range(10)]
    res, _ = _bulk(c, refs)
    assert res["ok"] == 10 and res["failed"] == 0
    assert c.invite_calls == 1, f"добавление ушло {c.invite_calls} запросами вместо одного"


def test_missing_invitees_are_attributed_by_name():
    """Атрибуция точнее поштучной: Telegram сам называет, кого не добавил."""
    c = _Client()
    refs = ["@a", "@b", "@c"]
    ents = {r: abs(hash(r)) % 100000 for r in refs}
    c.missing = [ents["@b"]]
    res, kinds = _bulk(c, refs)
    assert res["ok"] == 2 and res["failed"] == 1
    assert res["privacy_failed"] == ["@b"]
    assert kinds.get(mie.FAIL_PRIVACY) == 1


def test_unresolvable_target_does_not_break_the_batch():
    c = _Client(unresolvable=["@ghost"])
    res, kinds = _bulk(c, ["@a", "@ghost", "@b"])
    assert res["ok"] == 2 and res["failed"] == 1
    assert kinds.get(mie.FAIL_DEAD) == 1
    assert c.invite_calls == 1


@pytest.mark.parametrize("name,check", [
    ("PeerFloodError", lambda r: r["peer_flood"] is True),
    ("ChatAdminRequiredError", lambda r: r["no_rights"] is True),
    ("ChannelPrivateError", lambda r: any("group error" in e for e in r["errors"])),
])
def test_chat_and_account_errors_are_returned_not_retried(name, check):
    """Поштучный обход дал бы ровно то же — только N запросами вместо одного."""
    c = _Client(raise_on_invite=_named(name))
    res, _ = _bulk(c, ["@a", "@b"])
    assert res is not None, f"{name} не должен откатывать на поштучный путь"
    assert check(res)


def test_flood_wait_seconds_survive_the_batch():
    c = _Client(raise_on_invite=_named("FloodWaitError", seconds=42))
    res, _ = _bulk(c, ["@a", "@b"])
    assert res["flood_wait"] == 42


def test_per_target_error_falls_back_to_one_by_one():
    """Ошибка про ОДНУ цель роняет весь запрос — пакет не имеет права стоить
    целей, поэтому батч повторяется поштучно."""
    c = _Client(raise_on_invite=_named("InputUserDeactivatedError"))
    res, _ = _bulk(c, ["@a", "@b"])
    assert res is None, "должен быть откат на поштучный путь, а не потеря батча"


def test_all_targets_unresolvable_returns_without_calling_invite():
    c = _Client(unresolvable=["@x", "@y"])
    res, _ = _bulk(c, ["@x", "@y"])
    assert res["ok"] == 0 and res["failed"] == 2
    assert c.invite_calls == 0, "пустой запрос слать незачем"


def test_bulk_is_off_by_default():
    """На проде поведение не меняется, пока флаг не включён явно."""
    assert mie._BULK_DEFAULT is False or __import__("os").getenv("INVITE_BULK_API")


def test_single_target_never_uses_bulk():
    """На одной цели пакет не даёт выигрыша, но добавляет путь для ошибок."""
    import inspect
    src = inspect.getsource(mie.invite_batch)
    assert "len(user_refs) > 1" in src


# ── Сквозная проверка: откат не теряет целей ─────────────────────────────────

def _patch_engine(monkeypatch, client):
    async def _connect(session, acc, purpose):
        return client
    async def _resolve(cl, ref):
        return object()
    import services.account_manager as am
    monkeypatch.setattr(am, "connect_client", _connect)
    monkeypatch.setattr(mie, "_resolve_group_entity", _resolve)
    async def _sleep(_s):
        return None
    monkeypatch.setattr(mie.asyncio, "sleep", _sleep)


class _FallbackClient(_Client):
    """Пакетный запрос падает ошибкой про одну цель; поштучные — проходят."""

    def __init__(self):
        super().__init__()
        self.per_target_calls = 0

    async def __call__(self, request):
        self.invite_calls += 1
        if self.invite_calls == 1:
            raise _named("InputUserDeactivatedError")
        self.per_target_calls += 1
        return _Res()


def test_fallback_invites_everyone_no_target_is_lost(monkeypatch):
    c = _FallbackClient()
    _patch_engine(monkeypatch, c)
    refs = ["@a", "@b", "@c"]
    res = _run(mie.invite_batch("s", {"id": 1}, "@grp", refs, 1.0, bulk=True))
    assert res["ok"] == len(refs), (
        f"после отката приглашены не все: {res}"
    )
    assert c.per_target_calls == len(refs), "поштучный путь не отработал батч целиком"
    assert not res.get("bulk"), "результат отката не должен помечаться пакетным"


def test_bulk_result_is_marked_so_it_is_visible_in_logs(monkeypatch):
    c = _Client()
    _patch_engine(monkeypatch, c)
    res = _run(mie.invite_batch("s", {"id": 1}, "@grp", ["@a", "@b"], 1.0, bulk=True))
    assert res["ok"] == 2 and res.get("bulk") is True
    assert c.invite_calls == 1


def test_disabled_flag_keeps_one_by_one_path(monkeypatch):
    c = _Client()
    _patch_engine(monkeypatch, c)
    res = _run(mie.invite_batch("s", {"id": 1}, "@grp", ["@a", "@b"], 1.0, bulk=False))
    assert res["ok"] == 2
    assert c.invite_calls == 2, "с выключенным флагом путь обязан остаться поштучным"
    assert not res.get("bulk")


# ── Обкатка на части флота, а не «всем сразу» ────────────────────────────────

def _read(rel):
    from pathlib import Path
    return (Path(__file__).resolve().parents[1] / rel).read_text(encoding="utf-8")


def test_operation_can_override_the_global_flag():
    """Обкатывать такое нужно на ЧАСТИ флота: глобального env недостаточно."""
    worker = _read("services/op_worker.py")
    i = worker.index("async def _exec_mass_invite")
    seg = worker[i:worker.index("\nasync def _exec_", i + 1)]
    assert 'params.get("bulk_api")' in seg
    assert "bulk=_bulk_api" in seg, "флаг операции не доходит до движка"


def test_absent_flag_means_environment_decides():
    worker = _read("services/op_worker.py")
    assert "None if _bulk_api is None else bool(_bulk_api)" in worker, (
        "отсутствие ключа должно означать «как решит окружение», а не «выключено»"
    )


def test_endpoint_passes_the_flag_through():
    api = _read("services/mini_app_api.py")
    i = api.index("async def mass_inviter_submit")
    seg = api[i:api.index("    async def ", i + 30)]
    assert 'body.get("bulk_api") is not None' in seg
    assert 'params["bulk_api"]' in seg
