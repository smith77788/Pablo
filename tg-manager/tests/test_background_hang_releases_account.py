"""Повисший запрос в фоновом цикле не должен держать аккаунт занятым вечно.

Механика дефекта, ради которой написан файл. Фоновые циклы (прогрев, призрак,
активность, сетка контента, прогрев чатов) берут аккаунт у ТОГО ЖЕ арбитра, что
и операции владельца: `op_worker.try_claim_account`, а отпускают его в `finally`
через `release_accounts`. Пока аккаунт захвачен, он лежит в
`op_worker._accounts_in_use`.

Дальше две детали, которые по отдельности правильны, а вместе дают вечную
блокировку:

* `renew_leases()` каждые ~30 секунд продлевает аренду ВСЕМУ, что лежит в
  `_accounts_in_use`, — именно чтобы живой держатель не потерял сессию;
* `_reconcile_in_operation()` намеренно НЕ снимает `in_operation` с того, что
  лежит в `_accounts_in_use`, — для него живая память и есть доказательство
  занятости.

Оба считают, что запись в памяти означает живого держателя. Повисший запрос
ломает ровно это: `await` не возвращается никогда (мёртвый прокси отдаёт
half-open сокет — TCP установлен, ответа нет и не будет), поэтому `finally` не
исполняется, запись остаётся, аренда продлевается вечно. Аккаунт навсегда
«занят операцией»: он выпадает и из операций владельца, и из прогрева, и из
призрака, и вернуть его может только рестарт процесса.

`invite_behavior` в этом списке — не фоновый цикл: `humanize()` зовут прямо из
`_exec_mass_invite`, между приглашениями. Там цена выше — встаёт сама операция
вместе со всем арендованным под неё флотом.

Поэтому проверяем не «есть ли обёртка» (это делает храповик
tests/test_no_unbounded_telegram_request.py), а поведение: на клиенте, который
не отвечает, действие ОБРЫВАЕТСЯ по потолку и управление доходит до `finally`.
"""
from __future__ import annotations

import asyncio

import pytest


class HangingClient:
    """Клиент, у которого любой запрос не возвращается никогда."""

    def __init__(self) -> None:
        self.disconnected = False

    async def _hang(self, *a, **kw):
        await asyncio.Event().wait()

    # Вызов клиента как функции — это raw-запрос (client(SomeRequest(...))).
    def __call__(self, *a, **kw):
        return self._hang()

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return self._hang

    async def connect(self):
        await asyncio.Event().wait()

    async def disconnect(self):
        self.disconnected = True

    def is_connected(self):
        return True


# ── invite_behavior: путь операции инвайта ───────────────────────────────────

@pytest.mark.asyncio
async def test_invite_humanize_gives_up_and_disconnects(monkeypatch):
    """Повисший коннект внутри инвайта обязан дойти до finally с disconnect.

    Без потолка `_run` не возвращается вовсе: тест не «падает», он ВИСНЕТ,
    поэтому у самого теста есть свой внешний бюджет.
    """
    from services import invite_behavior

    monkeypatch.setattr(invite_behavior, "_CONNECT_TIMEOUT", 0.05, raising=True)
    client = HangingClient()

    import services.account_manager as am
    monkeypatch.setattr(am, "_make_client", lambda *a, **kw: client, raising=True)

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(
            invite_behavior._run("sess", {"id": 1}, "presence"), timeout=5)

    assert client.disconnected, (
        "управление не дошло до finally — клиент остался открытым, а аккаунт "
        "остался бы в _accounts_in_use с вечно продлеваемой арендой")


@pytest.mark.asyncio
async def test_invite_humanize_reports_failure_not_hang(monkeypatch):
    """humanize() глотает ошибку и возвращает None — но именно ВОЗВРАЩАЕТ."""
    from services import invite_behavior

    monkeypatch.setattr(invite_behavior, "_CONNECT_TIMEOUT", 0.05, raising=True)
    monkeypatch.setattr(invite_behavior, "_TG_TIMEOUT", 0.05, raising=True)
    import services.account_manager as am
    monkeypatch.setattr(am, "_make_client", lambda *a, **kw: HangingClient(),
                        raising=True)

    res = await asyncio.wait_for(
        invite_behavior.humanize({"id": 1, "session_str": "sess"}), timeout=5)
    assert res is None


# ── Фоновые циклы: действие обрывается по потолку ────────────────────────────

@pytest.mark.asyncio
async def test_ghost_action_gives_up_on_hang(monkeypatch):
    from services import ghost_engine

    monkeypatch.setattr(ghost_engine, "_TG_TIMEOUT", 0.05, raising=True)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(
            ghost_engine._act_update_status(HangingClient(), None, 1, 1),
            timeout=5)


@pytest.mark.asyncio
async def test_warmup_action_returns_failure_on_hang(monkeypatch):
    """Прогрев считает повисшее действие несостоявшимся, а не «выполненным»."""
    from services import account_warmer

    monkeypatch.setattr(account_warmer, "_TG_TIMEOUT", 0.05, raising=True)
    ok = await asyncio.wait_for(
        account_warmer._perform_read_channel(HangingClient(), "@ch"), timeout=5)
    assert ok is False


@pytest.mark.asyncio
async def test_activity_action_gives_up_on_hang(monkeypatch):
    from services import activity_engine

    monkeypatch.setattr(activity_engine, "_TG_TIMEOUT", 0.05, raising=True)
    ok = await asyncio.wait_for(
        activity_engine._act_read(HangingClient(), "@ch"), timeout=5)
    assert ok is False


# ── Оговорка обязана оставаться правдой ──────────────────────────────────────

@pytest.mark.parametrize("mod", [
    "account_warmer", "activity_engine", "ghost_engine",
    "content_mesh", "chat_warmup", "account_console",
])
def test_these_modules_really_hold_the_shared_claim(mod):
    """Если модуль перестал брать аккаунт у арбитра — рассуждение выше устарело.

    Тест не про таймауты: он держит в актуальном состоянии ПРИЧИНУ, по которой
    потолки здесь обязательны. Перестал брать — перечитайте файл, а не правьте
    список.
    """
    import os

    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "services", f"{mod}.py")
    with open(path, encoding="utf-8") as f:
        src = f.read()
    assert "try_claim_account" in src and "release_accounts" in src, (
        f"services/{mod}.py больше не берёт аккаунт у op_worker")


def test_invite_behavior_is_called_from_the_invite_executor():
    """`humanize` в пути операции — это и есть причина потолков в модуле."""
    import os

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "services", "op_worker.py"), encoding="utf-8") as f:
        src = f.read()
    assert "invite_behavior.humanize" in src, (
        "invite_behavior больше не зовут из исполнителя операций — "
        "перечитайте обоснование потолков в модуле")
