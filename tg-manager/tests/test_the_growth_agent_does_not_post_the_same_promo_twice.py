"""Growth Agent: промо не уходит в одну группу дважды.

ЧТО ЗАЩИЩАЕМ. Операция «рост в нише» вступает в группы и постит в них
рекламный текст, то есть делает НЕОБРАТИМОЕ действие, видимое людям. Её защита
от повтора — таблица `niche_growth_targets`: группа, в которую уже постили,
исключается из поиска следующим запуском. Повтор же здесь — событие штатное:
операция идёт часами (паузы 10–20 минут между группами), а деплой, таймаут
прогона и сторож зависших поднимают её заново с `done_items=0`.

Дыра была в том, КАК эта защита себя вела при сбое базы.

1. Чтение списка отработанных групп фейлилось ОТКРЫТО: `except → set()`. Пустой
   набор означает «владелец не был нигде», поиск отдаёт те же группы,
   вступление проходит как already_member — и промо уходит в группу второй раз
   с того же аккаунта. Это ровно та сигнатура спама, против которой написан
   весь безопасный режим операции.
2. Запись пометки шла ПОСЛЕ вступления, но её неудача ничего не меняла: пост
   уходил, а группа оставалась неотработанной, и следующий запуск постил в неё
   снова. Группа без id не отмечалась вовсе — и тоже получала пост.

Инвариант теперь один: ПОСТУ ПРЕДШЕСТВУЕТ НАДЁЖНАЯ ПОМЕТКА. Нет пометки —
нет поста; нет списка отработанных — нет прогона, операция уходит в повтор.
"""
from __future__ import annotations

import asyncio
import sys
import types

import pytest


# ── стенды вокруг исполнителя ────────────────────────────────────────────────

class _Pool:
    """Пул, который умеет ломаться ровно там, где нужно тесту."""

    def __init__(self, *, prior=(), fail_read: bool = False, fail_mark: bool = False):
        self.prior = list(prior)
        self.fail_read = fail_read
        self.fail_mark = fail_mark
        self.marked: list[int] = []

    async def fetch(self, query: str, *args):
        if "niche_growth_targets" in query:
            if self.fail_read:
                raise RuntimeError("база недоступна")
            return [{"group_id": g} for g in self.prior]
        return []

    async def execute(self, query: str, *args):
        if "INSERT INTO niche_growth_targets" in query:
            if self.fail_mark:
                raise RuntimeError("база недоступна")
            self.marked.append(int(args[1]))
        return "INSERT 0 1"

    async def fetchrow(self, query: str, *args):
        return None

    async def fetchval(self, query: str, *args):
        return 0


class _Telegram:
    """Что операция сделала во внешнем мире."""

    def __init__(self):
        self.joined: list = []
        self.posted: list = []

    async def join_channel(self, session_str, ref, **kw):
        self.joined.append(ref)
        return {"ok": True}

    async def post_to_channel(self, session_str, grp_id, text, **kw):
        self.posted.append(grp_id)
        return {"msg_id": 100 + len(self.posted)}


def _module(name: str, **attrs):
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    return mod


@pytest.fixture
def harness(monkeypatch):
    """Исполнитель с заглушенным окружением: наружу ходит только _Telegram."""
    import services
    from services import op_worker

    tg = _Telegram()
    groups = {"value": [
        {"id": 111, "title": "Группа 1", "join_ref": "@g1", "username": "g1",
         "access_hash": 1},
    ]}
    seen = {"exclude_ids": None}

    async def _search(session_str, keywords, **kw):
        seen["exclude_ids"] = set(kw.get("exclude_ids") or ())
        return list(groups["value"])

    async def _keywords(desc):
        return [desc]

    async def _enforce(pool, owner_id, *texts, **kw):
        return types.SimpleNamespace(blocked=False)

    for name, mod in (
        ("niche_searcher", _module("services.niche_searcher",
                                   generate_keywords=_keywords,
                                   search_niche_groups=_search)),
        ("account_manager", _module("services.account_manager",
                                    join_channel=tg.join_channel,
                                    post_to_channel=tg.post_to_channel)),
        ("content_safety", _module("services.content_safety", enforce=_enforce)),
        ("dm_engine", _module("services.dm_engine", expand_spintax=lambda t: t)),
    ):
        monkeypatch.setitem(sys.modules, f"services.{name}", mod)
        monkeypatch.setattr(services, name, mod, raising=False)

    async def _select(pool, owner_id, n, action_type=None, **kw):
        return [{"id": 7, "session_str": "sess", "phone": "+700"}]

    async def _no_quarantine(pool, op_id, accounts):
        return accounts, []

    async def _claim(ids):
        return list(ids)

    async def _release(ids):
        return None

    async def _not_cancelled(pool, op_id):
        return False

    async def _no_pause(pool, op_id, seconds, where=""):
        return None

    async def _no_delay(pool, owner_id, seconds):
        return 0.0

    monkeypatch.setattr(op_worker.resource_selector, "select_accounts", _select)
    monkeypatch.setattr(op_worker, "_filter_quarantined_accounts", _no_quarantine)
    monkeypatch.setattr(op_worker, "try_claim_accounts", _claim)
    monkeypatch.setattr(op_worker, "release_accounts", _release)
    monkeypatch.setattr(op_worker, "_is_cancelled", _not_cancelled)
    monkeypatch.setattr(op_worker, "_pause_watching_for_cancel", _no_pause)
    monkeypatch.setattr(op_worker, "_governed_delay", _no_delay)

    def _run(pool):
        return asyncio.run(op_worker._exec_niche_growth_post(
            pool, None, 4242, 500,
            {"niche": "фитнес", "promo_text": "Заходите к нам",
             "max_groups": 5, "acc_count": 1},
        ))

    return types.SimpleNamespace(tg=tg, groups=groups, seen=seen, run=_run,
                                 worker=op_worker)


# ── сами проверки ────────────────────────────────────────────────────────────

def test_the_healthy_run_posts_once_and_remembers_the_group(harness):
    pool = _Pool(prior=[999])
    res = harness.run(pool)
    assert res["status"] == "done", res
    assert harness.tg.posted == [111], harness.tg.posted
    assert pool.marked == [111], pool.marked
    # Список отработанных доехал до поиска: иначе он отдал бы те же группы снова.
    assert harness.seen["exclude_ids"] == {999}, harness.seen


def test_a_lost_list_of_done_groups_defers_the_run_instead_of_posting(harness):
    """Нет списка отработанных — нет прогона: иначе промо уйдёт по второму разу."""
    pool = _Pool(prior=[111], fail_read=True)
    res = harness.run(pool)
    assert res["status"] == "requeue", res
    assert harness.tg.posted == [], (
        "промо ушло, хотя операция не знала, в какие группы уже постила"
    )


def test_a_group_that_could_not_be_marked_does_not_get_the_promo(harness):
    """Пометка — цена поста. Не записалась — повтор отправил бы промо второй раз."""
    pool = _Pool(fail_mark=True)
    res = harness.run(pool)
    assert res["status"] == "done", res
    assert res.get("fail") == 1, res
    assert harness.tg.posted == [], harness.tg.posted
    # Вступление при этом состоялось — оно обратимо, и возвращать его незачем.
    assert harness.tg.joined == ["@g1"]


def test_a_group_without_an_id_is_not_posted_to(harness):
    """Группу без id отметить нечем, значит повтор постил бы в неё снова."""
    harness.groups["value"] = [
        {"id": 0, "title": "Без id", "join_ref": "@g0", "username": "g0",
         "access_hash": 0},
    ]
    pool = _Pool()
    res = harness.run(pool)
    assert res["status"] == "done", res
    assert harness.tg.posted == [], harness.tg.posted


def test_both_failures_reach_the_owner_as_protective_counters(harness):
    """Счётчик защиты без описания в реестре — строка, которую некому прочитать."""
    reg = harness.worker._PROTECTIVE_FAILURE_COUNTERS
    for name in ("infragram_growth_dedup_read_failures_total",
                 "infragram_growth_dedup_write_failures_total"):
        assert name in reg, f"{name} не попадёт в оповещение владельцу"
