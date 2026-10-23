"""Повторная ЛС тому же человеку — жалоба на спам, то есть бан аккаунта.

Строка «отправлено» в журнале кампании писалась ПОСЛЕ ответа Telegram. Между
доставленным сообщением и этой строкой было окно: цели идут с паузой 2–4с, а
SIGTERM на деплое приходит в это окно штатно (пуш в рабочую ветку уезжает в
прод). Обрыв там означал, что следующий прогон кампании не видел человека в
журнале и писал ему ВТОРОЕ такое же сообщение с незнакомого аккаунта.

Для инвайта этот же выбор уже сделан (`_premark_invite_targets`): метка
ставится ДО необратимого действия, а с тех, кого Telegram не рассматривал,
снимается по ответу. Здесь то же самое для ЛС.

Что проверяется:
  * метка ставится ДО сетевого вызова, а не после;
  * метка не записалась — сообщение НЕ отправляется (дедуп необратимого
    действия это цена действия, а не его украшение);
  * Telegram цель не рассматривал (флуд, мёртвая сессия, сетевой сбой) —
    метка снимается, иначе человек выпал бы из кампании навсегда;
  * цель рассмотрена и отказана окончательно — метка переводится в свой
    статус, иначе экраны кампании показали бы отправку, которой не было.
"""
from __future__ import annotations

import ast
import asyncio
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_LOOP = asyncio.new_event_loop()


def _run(coro):
    return _LOOP.run_until_complete(coro)


def _source(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _func_src(rel: str, name: str) -> str:
    src = _source(rel)
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    raise AssertionError(f"{name} пропал из {rel}")


def _total(name: str) -> float:
    from services import metrics

    counters = (metrics.snapshot() or {}).get("counters") or {}
    return float(sum(v for k, v in counters.items()
                     if k == name or k.startswith(name + "{")))


class _Pool:
    """Стенд базы: запоминает запросы, по требованию падает на нужном глаголе."""

    def __init__(self, fail_on: str | None = None, update_rows: int = 1):
        self.calls: list[tuple[str, tuple]] = []
        self.fail_on = fail_on
        self.update_rows = update_rows

    async def execute(self, sql, *args):
        self.calls.append((" ".join(sql.split()), args))
        verb = sql.strip().split()[0].upper()
        if self.fail_on and verb == self.fail_on:
            raise RuntimeError("стенд: запись отклонена")
        if verb == "UPDATE":
            return f"UPDATE {self.update_rows}"
        return f"{verb} 1"

    def sql_of(self, verb: str) -> list[str]:
        return [s for s, _a in self.calls if s.upper().startswith(verb)]


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """Повтор записи не должен растягивать прогон тестов на секунды."""
    async def _instant(_s):
        return None

    monkeypatch.setattr(asyncio, "sleep", _instant)


# ── Поведение дверей ─────────────────────────────────────────────────────────

def test_the_mark_is_written_before_the_message_goes_out():
    from services import dm_engine

    pool = _Pool()
    assert _run(dm_engine._premark_dm_target(pool, 7, 3, 555)) is True
    inserts = pool.sql_of("INSERT")
    assert len(inserts) == 1, pool.calls
    assert "dm_campaign_log" in inserts[0] and "'sent'" in inserts[0]
    assert "ON CONFLICT DO NOTHING" in inserts[0], (
        "повторная метка обязана быть безвредной: прогон может повториться")


def test_a_lost_mark_cancels_the_message_and_is_counted():
    """Отправить, не сумев запомнить — значит написать человеку второй раз."""
    from services import dm_engine

    before = _total("infragram_dm_dedup_write_failures_total")
    pool = _Pool(fail_on="INSERT")
    assert _run(dm_engine._premark_dm_target(pool, 7, 3, 555)) is False
    assert len(pool.sql_of("INSERT")) == 2, (
        "одна попытка — сетевой блик стоил бы цели; попыток должно быть две")
    assert _total("infragram_dm_dedup_write_failures_total") > before, (
        "потерянная метка обязана быть видна на графике, а не только в логе")


def test_a_target_telegram_never_looked_at_gets_its_mark_back():
    from services import dm_engine

    pool = _Pool()
    _run(dm_engine._unmark_dm_target(pool, 7, 555))
    deletes = pool.sql_of("DELETE")
    assert len(deletes) == 1, pool.calls
    assert "status='sent'" in deletes[0], (
        "снимать можно только предзапись, иначе удалялись бы честные исходы")


def test_a_failed_unmark_is_counted_not_swallowed():
    """Человек, выпавший из кампании, не должен исчезать молча."""
    from services import dm_engine

    before = _total("infragram_dm_unmark_failures_total")
    pool = _Pool(fail_on="DELETE")
    _run(dm_engine._unmark_dm_target(pool, 7, 555))
    assert _total("infragram_dm_unmark_failures_total") > before


def test_a_final_refusal_keeps_the_mark_under_its_own_status():
    from services import dm_engine

    pool = _Pool(update_rows=1)
    _run(dm_engine._settle_dm_target(pool, 7, 3, 555, "blocked", "закрыл личку"))
    updates = pool.sql_of("UPDATE")
    assert len(updates) == 1 and "status=$3" in updates[0], pool.calls
    assert not pool.sql_of("INSERT"), (
        "предзапись уже стоит — вторая строка раздваивала бы счёт по статусам")


def test_without_a_premark_the_outcome_is_still_recorded():
    """Предзапись могли потерять; исход цели всё равно обязан попасть в журнал."""
    from services import dm_engine

    pool = _Pool(update_rows=0)
    _run(dm_engine._settle_dm_target(pool, 7, 3, 555, "skip", "удалён"))
    assert len(pool.sql_of("UPDATE")) == 1
    inserts = pool.sql_of("INSERT")
    assert len(inserts) == 1 and "'skip'" not in inserts[0], pool.calls


# ── Проводка в кампании ──────────────────────────────────────────────────────

def test_the_campaign_marks_before_it_sends():
    """Порядок двух строк здесь и есть всё содержание фикса."""
    body = _func_src("services/dm_engine.py", "run_campaign")
    i_mark = body.index("_premark_dm_target(")
    i_send = body.index("await send_dm(")
    assert i_mark < i_send, (
        "метка обязана стоять ДО сетевого вызова: обрыв между ними и есть "
        "окно, в котором человек получает второе сообщение")


def test_the_campaign_no_longer_writes_the_mark_after_the_fact():
    body = _func_src("services/dm_engine.py", "run_campaign")
    assert "VALUES ($1,$2,$3,'sent')" not in " ".join(body.split()), (
        "запись «отправлено» после ответа Telegram — это и был потерянный "
        "случай; теперь её ставит предзапись")


def test_every_undelivered_outcome_returns_the_target_to_the_campaign():
    """Флуд, мёртвая сессия, сетевой сбой — человек сообщения не получил."""
    body = _func_src("services/dm_engine.py", "run_campaign")
    assert body.count("_unmark_dm_target(") >= 3, (
        "ветки «не доставлено» обязаны снимать метку, иначе цель вычтена из "
        "кампании навсегда: " + str(body.count("_unmark_dm_target(")))


def test_a_message_that_was_not_sent_is_not_counted_as_sent():
    """Пропуск по несработавшей метке обязан попасть в счёт неудач."""
    body = _func_src("services/dm_engine.py", "run_campaign")
    m = re.search(r"if not await _premark_dm_target\([^)]*\):\s*\n(.+?)\n\s*t0_dm",
                  body, re.S)
    assert m, "вызов предзаписи в кампании не найден"
    branch = m.group(1)
    assert "failed += 1" in branch, (
        "иначе кампания закончится «успешно», не написав никому")
    assert "continue" in branch, "после пропуска цели отправлять нельзя"


def test_the_detector_reads_the_real_function():
    """Пустой разбор сделал бы проверки выше вечно зелёными."""
    body = _func_src("services/dm_engine.py", "run_campaign")
    assert "await send_dm(" in body and len(body) > 2000
