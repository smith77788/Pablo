"""У оповещения о сбое защиты должен быть адресат — иначе защиты нет.

ЧТО ЗАЩИЩАЕМ. Все защитные счётчики воркера (дедуп инвайта, журнал сделанного,
пауза аккаунта, предзаписи инвайта и Growth Agent) доходят до человека ровно
одним путём: сообщением админам из ADMIN_IDS. Пустая переменная означала, что
сторож защитных сбоев КАЖДЫЙ круг молча возвращается: счётчики растут, текст
отчёта составляется, адресата нет — и о том, что защита от повторного
реального действия перестала писаться, не узнаёт никто. Ровно тот класс, из-за
которого в этом проекте и завелось правило «механизм может быть исправен и при
этом не выполняться НИ РАЗУ».

Чинится не доставкой (получателя взять негде), а видимостью: факт «сообщить
некому» теперь громкий на старте воркера и считается счётчиком, то есть виден
в срезе метрик, а не только в логе, которого никто не читает.
"""
from __future__ import annotations

import ast
import asyncio
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_NO_RECIPIENT = "infragram_protective_alerts_no_recipient_total"


def _src(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _total(name: str) -> float:
    from services import metrics

    counters = (metrics.snapshot() or {}).get("counters") or {}
    return float(sum(v for k, v in counters.items()
                     if k == name or k.startswith(name + "{")))


class _Bot:
    def __init__(self):
        self.sent: list = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))


@pytest.fixture
def grown(monkeypatch):
    """Счётчик защитной записи вырос — сторожу есть о чём докладывать."""
    from services import metrics

    metrics.inc("infragram_invite_dedup_write_failures_total", {}, 3)
    return {}


def test_without_a_recipient_the_failure_is_counted_not_swallowed(grown, monkeypatch):
    from services import op_worker

    monkeypatch.setenv("ADMIN_IDS", "")
    before = _total(_NO_RECIPIENT)
    seen: dict = {}
    bot = _Bot()
    asyncio.run(op_worker._watchdog_protective_failures(object(), bot, seen))

    assert bot.sent == [], "сообщение ушло в никуда"
    assert _total(_NO_RECIPIENT) > before, (
        "полный отказ оповещений о защите не виден в метриках"
    )
    # База отсчёта не двинулась: сообщение должно вернуться, когда адресат появится.
    assert seen == {}, seen


def test_with_a_recipient_the_alert_is_delivered_and_the_baseline_moves(grown, monkeypatch):
    from services import op_worker

    monkeypatch.setenv("ADMIN_IDS", "4242")

    class _Db:
        async def notify_dedup_ok(self, pool, uid, key, window):
            return True

        async def notify_dedup_forget(self, pool, uid, key):
            return None

    monkeypatch.setattr(op_worker, "db", _Db())
    before = _total(_NO_RECIPIENT)
    seen: dict = {}
    bot = _Bot()
    asyncio.run(op_worker._watchdog_protective_failures(object(), bot, seen))

    assert bot.sent and bot.sent[0][0] == 4242, bot.sent
    assert "Защита не записалась" in bot.sent[0][1]
    assert _total(_NO_RECIPIENT) == before, (
        "адресат есть, а счётчик «сообщить некому» всё равно вырос"
    )
    assert "infragram_invite_dedup_write_failures_total" in seen, seen


def test_the_worker_says_it_at_startup_too(monkeypatch):
    """Сторож срабатывает только когда защита УЖЕ сломалась — поздно узнавать.

    Проверка по исходнику: `run` — бесконечный цикл, вызвать его в тесте
    нельзя, а молча выпавшая проверка снова оставила бы оповещения без
    адресата.
    """
    src = _src("services/op_worker.py")
    tree = ast.parse(src)
    run = next(n for n in ast.walk(tree)
               if isinstance(n, ast.AsyncFunctionDef) and n.name == "run")
    body = ast.get_source_segment(src, run) or ""
    assert "ADMIN_IDS пуст" in body, (
        "воркер больше не предупреждает на старте, что защитные оповещения "
        "отправлять некому"
    )
    assert _NO_RECIPIENT in body, "старт не считает этот отказ счётчиком"


def test_the_counter_is_described_for_the_owner():
    from services.metrics import _HELP

    assert _NO_RECIPIENT in _HELP
    assert "ADMIN_IDS" in _HELP[_NO_RECIPIENT]
