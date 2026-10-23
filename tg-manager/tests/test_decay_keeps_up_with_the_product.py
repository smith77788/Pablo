"""Распад обязан успевать за продуктом, иначе его просто нет.

ЧТО БЫЛО. `run_decay` брал РОВНО ОДНУ пачку на 500 строк, и звал его организм
шестичасовым проходом — тем же, что подчищает журнал событий. Проход один на
всех владельцев, значит распад обрабатывал 2000 состояний в сутки на всю
платформу. У одного владельца с пятью тысячами живых контактов просрочивается
больше; очередь росла безвозвратно.

Что из этого следовало, кроме неверных цифр на экране: «остыл» переставало
случаться, а вместе с ним не рождалось виртуальное событие user_lost_interest —
то самое, на которое подписаны автоматизации. Механика, вокруг которой слой и
построен («тишина остужает»), на масштабе продукта не работала.

ЧТО ТЕПЕРЬ. Пачками, пока окно не опустеет (с потолком, чтобы проход не висел
на разовом всплеске), и на КАЖДОМ сердцебиении организма, а не раз в шесть
часов: верхний рунг живёт сутки, и шести часов задержки хватало, чтобы экран и
каскад считали по состояниям, которые уже неверны.
"""
from __future__ import annotations

import inspect
import pathlib
from datetime import datetime, timedelta, timezone

from services import virtual_layer as V

ROOT = pathlib.Path(__file__).resolve().parents[1]
RUNNER = (ROOT / "services" / "organism" / "runner.py").read_text(encoding="utf-8")

T0 = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)


class _Pool:
    """Отдаёт заданное число просроченных строк, считая прочитанные пачки."""

    def __init__(self, rows: list[dict]):
        self.pending = list(rows)
        self.fetches = 0
        self.written: list[str] = []

    async def fetch(self, sql, *args):
        self.fetches += 1
        limit = int(sql.rsplit("LIMIT", 1)[-1].strip())
        batch, self.pending = self.pending[:limit], self.pending[limit:]
        return batch

    async def execute(self, sql, *args):
        if "INSERT INTO virtual_states" in sql:
            self.written.append(args[2])
        return "INSERT 0 1"


def _state(eid: str, value: str, *, hours_overdue: int = 2) -> dict:
    return {"owner_id": 7, "entity_type": V.USER, "entity_id": eid,
            "state_key": "funnel", "value": value, "confidence": 0.7,
            "expires_at": T0 - timedelta(hours=hours_overdue), "source": None}


async def test_decay_drains_the_whole_window_not_one_batch():
    rows = [_state(f"c{i}", "ready") for i in range(1250)]
    pool = _Pool(rows)
    cooled = await V.run_decay(pool, 7, limit=500, now=T0)
    assert cooled == 1250, (
        f"остыло {cooled} из 1250: распад берёт одну пачку за проход, и "
        "очередь просроченных растёт безвозвратно")
    assert pool.fetches == 3, pool.fetches


async def test_an_empty_window_costs_one_query():
    pool = _Pool([])
    assert await V.run_decay(pool, 7, now=T0) == 0
    assert pool.fetches == 1, "спокойное состояние не должно стоить лишних запросов"


async def test_a_batch_of_bottom_rungs_does_not_stop_the_pass():
    """Пачка со дна лестницы остывших не даёт, но работу делает.

    Если считать прогресс по остывшим, проход остановится на первой такой
    пачке — а это ровно те строки, из-за которых окно и засорялось.
    """
    rows = ([_state(f"n{i}", "new") for i in range(100)]
            + [_state(f"c{i}", "ready") for i in range(10)])
    pool = _Pool(rows)
    cooled = await V.run_decay(pool, 7, limit=100, now=T0)
    assert cooled == 10, (
        f"остыло {cooled}: проход остановился на пачке, где остывать было "
        "нечему, и живые состояния до него не дошли")


async def test_the_batch_cap_is_respected():
    """Проход организма не должен висеть на всплеске неограниченно."""
    pool = _Pool([_state(f"c{i}", "ready") for i in range(5000)])
    cooled = await V.run_decay(pool, 7, limit=100, batches=3, now=T0)
    assert cooled == 300 and pool.fetches == 3, (cooled, pool.fetches)


# ── Проводка ───────────────────────────────────────────────────────────────

def _calls_run_decay(node) -> bool:
    import ast
    return any(isinstance(n, ast.Attribute) and n.attr == "run_decay"
               for n in ast.walk(node))


def test_decay_runs_on_every_heartbeat_not_the_six_hour_pass():
    """Шестичасовой блок — ретеншен журнала; распаду там не место.

    Разбираем дерево, а не отступы: вложенность — это именно то, что здесь
    проверяется, и по тексту её видно плохо.
    """
    import ast
    tick = next(n for n in ast.walk(ast.parse(RUNNER))
                if isinstance(n, ast.AsyncFunctionDef) and n.name == "_tick")
    assert _calls_run_decay(tick), "распад вообще не зовётся из прохода организма"
    six_hour = [n for n in tick.body
                if isinstance(n, ast.If) and "_last_prune" in ast.dump(n.test)]
    assert six_hour, "шестичасовой блок пропал — проверку надо пересмотреть"
    for block in six_hour:
        assert not _calls_run_decay(block), (
            "распад внутри шестичасового блока: верхний рунг живёт сутки, и за "
            "шесть часов экран и каскад успевают соврать")


def test_run_decay_still_emits_the_cooling_event():
    """На этом событии висят автоматизации — их распад и кормит."""
    src = inspect.getsource(V._decay_batch)
    assert "spine.emit" in src and "virtual_event_for" in src
