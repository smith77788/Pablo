"""Повтор операции ставится шиной, а не сбросом строки в 'pending'.

ЧТО БЫЛО. Кнопка «повторить» существовала в продукте в ЧЕТЫРЁХ копиях, и ни
одна не ходила через `operation_bus`:

  * `bot/handlers/mass_ops.py` — одна операция и «все упавшие»;
  * `bot/handlers/botmother_menu.py` — то же из панели операций.

Каждая делала сырой `UPDATE operation_queue SET status='pending',
retry_count=0, done_items=0 …`. Такой сброс — это постановка операции в работу:
поллер забирает её в ту же секунду. Но мимо гейта тарифа, мимо предохранителя
Ban Weather и мимо дедупа двойного тапа. Храповик сырых INSERT'ов этого не
ловил, потому что INSERT'а там нет.

Хуже всего две детали:
  * «все упавшие» сбрасывала ВСЕ упавшие операции владельца разом, без потолка —
    включая `mass_invite`, самую баноопасную операцию продукта;
  * `done_items=0` стирал память о сделанном: журнал целей оставался, и операция
    навсегда показывала «обработано меньше, чем было сделано». А в
    `botmother_menu` второй UPDATE писал `done_items=0` ВООБЩЕ без фильтра
    статуса — то есть мог обнулить счётчик операции, которую исполнитель уже
    подхватил.

ФИКС: две функции шины — `resubmit_unfinished` и `resubmit_one` — единственная
дверь повтора на всех поверхностях; все четыре кнопки зовут их.
"""
from __future__ import annotations

import asyncio
import pathlib
import re

import pytest

from services import op_status, operation_bus


class _Pool:
    def __init__(self, rows):
        self.rows = [dict(r) for r in rows]
        self.writes: list[str] = []

    def _filtered(self, query):
        m = re.search(r"status\s+IN\s*\(([^)]*)\)", query)
        if m:
            allowed = set(re.findall(r"'(\w+)'", m.group(1)))
            return [r for r in self.rows if r["status"] in allowed]
        return list(self.rows)

    async def fetch(self, query, *args):
        if "operation_queue" not in query:
            return []
        rows = self._filtered(query)
        m = re.search(r"LIMIT (\d+)", query)
        return rows[: int(m.group(1))] if m else rows

    async def fetchrow(self, query, *args):
        if "operation_queue" not in query:
            return None
        if "WHERE id=$1" in query:
            want = args[0]
            for r in self.rows:
                if r["id"] == want:
                    return dict(r)
            return None
        rows = self._filtered(query)
        return dict(rows[0]) if rows else None

    async def execute(self, query, *args):
        self.writes.append(query)
        return "UPDATE 1"


def _row(op_id, status=op_status.PARTIAL, op_type="mass_invite"):
    return {"id": op_id, "owner_id": 555, "op_type": op_type, "status": status,
            "params": {"targets": ["a", "b"]}, "label": "Приглашение",
            "total_items": 380, "done_items": 203}


@pytest.fixture
def submitted(monkeypatch):
    seen: list[dict] = []

    async def _fake_submit(pool, owner_id, op_type, params, **kw):
        seen.append({"owner_id": owner_id, "op_type": op_type,
                     "params": params, "total_items": kw.get("total_items")})
        return 9000 + len(seen)

    monkeypatch.setattr(operation_bus, "submit", _fake_submit)
    return seen


def test_resubmit_unfinished_takes_partial_and_failed(submitted):
    pool = _Pool([_row(1, op_status.PARTIAL), _row(2, op_status.FAILED),
                  _row(3, op_status.DONE), _row(4, op_status.CANCELLED)])
    res = asyncio.run(operation_bus.resubmit_unfinished(pool, 555))

    assert res["retried"] == 2, f"повторено не то, что недоведено: {res}"
    assert not pool.writes, (
        "повтор всё ещё пишет в operation_queue напрямую: "
        f"{pool.writes}")


def test_resubmit_unfinished_skips_mass_publish(submitted):
    """Публикация — исключение: успешные каналы получили бы дубль поста."""
    pool = _Pool([_row(1, op_type="mass_publish")])
    res = asyncio.run(operation_bus.resubmit_unfinished(pool, 555))
    assert (res["retried"], res["skipped"]) == (0, 1)
    assert submitted == []


def test_resubmit_unfinished_has_a_ceiling(submitted):
    """Потолок есть: сырой сброс брал ВСЕ операции владельца разом."""
    pool = _Pool([_row(i) for i in range(1, 30)])
    asyncio.run(operation_bus.resubmit_unfinished(pool, 555, limit=5))
    assert len(submitted) == 5


def test_resubmit_unfinished_survives_plan_refusal(monkeypatch):
    """Отказ по тарифу — не падение: остальные операции повторяются."""
    calls = {"n": 0}

    async def _fake_submit(pool, owner_id, op_type, params, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise operation_bus.PlanRequiredError("mass_invite", "paid")
        return 1

    monkeypatch.setattr(operation_bus, "submit", _fake_submit)
    pool = _Pool([_row(1), _row(2)])
    res = asyncio.run(operation_bus.resubmit_unfinished(pool, 555))
    assert res["retried"] == 1 and res["skipped"] == 1


def test_resubmit_one_prefers_pointwise_retry(monkeypatch, submitted):
    """Тип с точечным повтором повторяется только по упавшим целям."""
    pool = _Pool([_row(11)])
    called: list[int] = []

    async def _fake_retry_failed(p, owner_id, src_op_id):
        called.append(src_op_id)
        return {"ok": True, "op_id": 777, "count": 17, "reason": ""}

    monkeypatch.setattr(operation_bus, "submit_retry_failed", _fake_retry_failed)
    monkeypatch.setattr(operation_bus, "retry_targets_meta",
                        lambda op_type: {"param": "targets"})
    res = asyncio.run(operation_bus.resubmit_one(pool, 555, 11))

    assert called == [11] and res["op_id"] == 777
    assert submitted == [], "точечный повтор подменён повтором целиком"


def test_resubmit_one_falls_back_to_whole_operation(monkeypatch, submitted):
    """Тип без точечного повтора не теряет кнопку: ставим операцию целиком."""
    pool = _Pool([_row(12, op_type="mass_join")])
    monkeypatch.setattr(operation_bus, "retry_targets_meta", lambda op_type: None)
    res = asyncio.run(operation_bus.resubmit_one(pool, 555, 12))

    assert res["ok"] and len(submitted) == 1
    assert submitted[0]["op_type"] == "mass_join"
    assert not pool.writes


def test_resubmit_one_refuses_finished_and_foreign(submitted):
    pool = _Pool([_row(13, op_status.DONE), _row(14, op_status.PARTIAL)])
    pool.rows[1]["owner_id"] = 999

    done = asyncio.run(operation_bus.resubmit_one(pool, 555, 13))
    alien = asyncio.run(operation_bus.resubmit_one(pool, 555, 14))

    assert done["ok"] is False and "неудачей" in done["reason"]
    assert alien["ok"] is False and "не найдена" in alien["reason"]
    assert submitted == []


# ── Повтор не клонирует расписание и не теряет журнал предка ─────────────────

def test_retry_links_the_ancestor_journal_and_drops_the_schedule(submitted):
    """Главное в params повтора: ссылка на журнал предка и НЕТ расписания.

    Без ссылки повтор идёт со свежим пустым журналом и честно проходит весь
    список целей заново — рассылка, вставшая на 203 адресатах из 380, присылает
    этим 203 второе такое же сообщение. А унаследованное `repeat_interval_min`
    заводит ВТОРУЮ цепочку автопостинга с тем же интервалом: каналы получают
    посты вдвое чаще, следующий тап — вчетверо, и для флота это прямой путь в
    бан.
    """
    row = _row(77, op_type="mass_publish_dm")
    row["params"] = {"text": "привет", "repeat_interval_min": 60,
                     "fail_streak": 2, "targets": ["a", "b"]}
    pool = _Pool([row])
    res = asyncio.run(operation_bus.resubmit_unfinished(pool, 555))

    assert res["retried"] == 1
    p = submitted[0]["params"]
    assert p.get("retry_of_op") == 77, (
        "повтор не сослался на журнал предка — уже обработанные цели получат "
        f"работу второй раз: {p}")
    assert "repeat_interval_min" not in p, (
        f"повтор унаследовал расписание — будет вторая цепочка: {p}")
    assert p["text"] == "привет" and p["targets"] == ["a", "b"], (
        "повтор потерял содержательные параметры операции")
    assert res["recurrence_dropped"] == 1


def test_single_retry_reports_the_dropped_schedule(monkeypatch, submitted):
    """Владельцу говорим правду: расписание повтор не наследует."""
    row = _row(78, op_type="mass_join")
    row["params"] = {"repeat_interval_min": 30}
    monkeypatch.setattr(operation_bus, "retry_targets_meta", lambda op_type: None)
    res = asyncio.run(operation_bus.resubmit_one(_Pool([row]), 555, 78))

    assert res["ok"] and res.get("dropped_recurrence") is True
    assert submitted[0]["params"].get("retry_of_op") == 78
    assert "repeat_interval_min" not in submitted[0]["params"]


def test_retry_without_a_schedule_says_nothing_about_it(monkeypatch, submitted):
    """Обратная сторона: у операции без расписания сообщать не о чем."""
    monkeypatch.setattr(operation_bus, "retry_targets_meta", lambda op_type: None)
    res = asyncio.run(operation_bus.resubmit_one(_Pool([_row(79)]), 555, 79))
    assert res.get("dropped_recurrence") is False


# ── Размер повтора — остаток, а не потолок предка ────────────────────────────

class _JournalPool(_Pool):
    """Очередь плюс журнал целей: повтор обязан считать по нему остаток."""

    def __init__(self, rows, closed=0):
        super().__init__(rows)
        self.closed = closed

    async def fetchrow(self, query, *args):
        if "operation_log" in query:
            return {"n": self.closed}
        return await super().fetchrow(query, *args)


def test_retry_is_sized_by_what_is_left(monkeypatch, submitted):
    """Повтор на 380 целей, где 203 закрыты, обязан встать на 177.

    Унаследованный потолок 380 означал, что повтор, доделавший всё, по недобору
    прогресса (177 из 380) объявлялся «частично выполненным» — и его снова
    предлагали повторить. Круг ложных «недоведено».
    """
    monkeypatch.setattr(operation_bus, "retry_targets_meta", lambda op_type: None)
    pool = _JournalPool([_row(90)], closed=203)
    res = asyncio.run(operation_bus.resubmit_one(pool, 555, 90))

    assert res["ok"] and submitted[0]["total_items"] == 177, (
        "повтор унаследовал потолок предка вместо остатка: "
        f"{submitted[0]['total_items']}")
    assert res["count"] == 177


def test_retry_refuses_when_everything_is_closed(monkeypatch, submitted):
    """Все цели закрыты — повторять нечего, и это не «ок, поставлено»."""
    monkeypatch.setattr(operation_bus, "retry_targets_meta", lambda op_type: None)
    pool = _JournalPool([_row(91)], closed=380)
    res = asyncio.run(operation_bus.resubmit_one(pool, 555, 91))

    assert res["ok"] is False and "закрыты" in res["reason"]
    assert submitted == []


def test_retry_keeps_the_parent_size_without_a_journal(monkeypatch, submitted):
    """Типы без журнала целей ведут себя как раньше: размер предка."""
    monkeypatch.setattr(operation_bus, "retry_targets_meta", lambda op_type: None)
    pool = _JournalPool([_row(92)], closed=0)
    res = asyncio.run(operation_bus.resubmit_one(pool, 555, 92))

    assert res["ok"] and submitted[0]["total_items"] == 380


# ── Храповик на класс: повтор не возвращается к сбросу строки ────────────────

_RESET_RE = re.compile(
    r"UPDATE\s+operation_queue[^;]{0,400}?(retry_count\s*=\s*0|done_items\s*=\s*0)",
    re.IGNORECASE | re.DOTALL)

# Исполнитель и шина возвращают операцию в очередь по своему праву: у воркера
# это откладывание по флуду и подъём осиротевшей строки, у шины — сама дверь.
_ALLOWED = {"services/op_worker.py", "services/operation_bus.py"}


def _sources():
    root = pathlib.Path(__file__).resolve().parent.parent
    for rel in ("bot", "services", "mini_app"):
        for f in (root / rel).rglob("*.py"):
            yield f.relative_to(root).as_posix(), f.read_text(encoding="utf-8")


def test_detector_bites_on_a_known_sample():
    """Самопроверка измерителя — до того, как верить его пустому списку."""
    sample = ("await pool.execute(\"UPDATE operation_queue SET status='pending', "
              "retry_count=0, done_items=0 WHERE id=$1\", op_id)")
    assert _RESET_RE.search(sample)
    assert not _RESET_RE.search("UPDATE accounts SET retry_count=0 WHERE id=$1")


def test_no_retry_by_resetting_the_queue_row():
    offenders = []
    for rel, src in _sources():
        if rel in _ALLOWED:
            continue
        # строки-комментарии и docstring'и описывают то, ЧТО БЫЛО, — не код
        code = "\n".join(l for l in src.splitlines()
                         if not l.lstrip().startswith("#"))
        for m in _RESET_RE.finditer(code):
            offenders.append(f"{rel}: …{m.group(0)[:90]}…")
    assert not offenders, (
        "повтор операции снова сбрасывает строку очереди вместо шины — это "
        "постановка в работу мимо гейта тарифа, предохранителя и дедупа:\n"
        + "\n".join(offenders))
