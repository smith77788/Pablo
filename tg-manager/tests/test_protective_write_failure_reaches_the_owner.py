"""Сбой защитной записи доходит до человека, а не остаётся в логе.

РАЗРЫВ. У записей, которые ЗАЩИЩАЮТ от повторного действия (дедуп инвайта,
пауза аккаунта, строка успеха в журнале, доставка отчёта, постановка следующего
запуска повторяющейся операции, аренда исполнителя), есть повторная попытка,
`log.error` и счётчик в `services/metrics`. Этого мало:

* логи владелец не читает;
* счётчики живут в ПАМЯТИ процесса и отдаются только на `/metrics` — сборщика
  метрик в проде нет, а рестарт их обнуляет.

То есть сбой защиты происходил молча, и следующая попытка операции делала
реальное действие второй раз: повторные приглашения тем же людям (риск бана),
второй пост в канал, второе сообщение человеку. Узнать об этом было негде.

ЧТО ПРОВЕРЯЕМ. `_watchdog_protective_failures` сравнивает счётчики с базой
отсчёта, и при росте шлёт админу русское сообщение, которое называет, что
именно перестало защищать. База отсчёта двигается ТОЛЬКО после успешной
отправки: иначе пустой список админов или сбой доставки теряли бы сообщение
навсегда (эта ошибка уже была на алертах о застрявших операциях).
"""
from __future__ import annotations

import ast
import asyncio
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "services", "op_worker.py")


class _Bot:
    def __init__(self, fail: bool = False):
        self.sent: list[tuple[int, str]] = []
        self._fail = fail

    async def send_message(self, chat_id, text, **kw):
        if self._fail:
            raise RuntimeError("Telegram недоступен")
        self.sent.append((int(chat_id), str(text)))


class _Pool:
    """Окно анти-спама всегда открыто: его собственное поведение проверено отдельно."""

    async def execute(self, *a, **k):
        return "UPDATE 1"

    async def fetchrow(self, *a, **k):
        return None

    async def fetch(self, *a, **k):
        return []


@pytest.fixture
def ow(monkeypatch):
    from services import metrics, op_worker

    metrics.reset()
    monkeypatch.setattr(op_worker.db, "notify_dedup_ok",
                        lambda *a, **k: _true(), raising=False)
    import bot.utils.subscription as subs
    monkeypatch.setattr(subs, "_admin_ids", lambda: {777}, raising=False)
    return op_worker


async def _true():
    return True


def test_a_failed_protective_write_is_reported_to_the_admin(ow):
    from services import metrics

    metrics.inc("infragram_invite_dedup_write_failures_total")
    metrics.inc("infragram_invite_dedup_write_failures_total")
    bot, pool, seen = _Bot(), _Pool(), {}

    asyncio.run(ow._watchdog_protective_failures(pool, bot, seen))

    assert bot.sent, "сбой защиты не дошёл ни до кого"
    aid, text = bot.sent[0]
    assert aid == 777
    assert "дедуп инвайта" in text, text
    assert "2 раз" in text, "не сказано, сколько раз защита не легла"
    assert seen.get("infragram_invite_dedup_write_failures_total") == 2


def test_nothing_is_sent_when_nothing_failed(ow):
    bot, pool, seen = _Bot(), _Pool(), {}
    asyncio.run(ow._watchdog_protective_failures(pool, bot, seen))
    assert not bot.sent, "сообщение без повода — владелец перестанет их читать"


def test_the_same_failure_is_not_repeated_every_five_minutes(ow):
    from services import metrics

    metrics.inc("infragram_journal_write_failures_total")
    bot, pool, seen = _Bot(), _Pool(), {}
    asyncio.run(ow._watchdog_protective_failures(pool, bot, seen))
    assert len(bot.sent) == 1
    asyncio.run(ow._watchdog_protective_failures(pool, bot, seen))
    assert len(bot.sent) == 1, "тот же сбой ушёл второй раз без нового роста"


def test_a_lost_message_comes_back_on_the_next_round(ow):
    """Доставка не удалась — база отсчёта не двигается, сообщение вернётся."""
    from services import metrics

    metrics.inc("infragram_account_cooldown_write_failures_total")
    broken, pool, seen = _Bot(fail=True), _Pool(), {}
    asyncio.run(ow._watchdog_protective_failures(pool, broken, seen))
    assert not broken.sent and not seen, "сбой доставки потерял алерт навсегда"

    ok = _Bot()
    asyncio.run(ow._watchdog_protective_failures(pool, ok, seen))
    assert ok.sent and "пауза аккаунта" in ok.sent[0][1]


def test_without_admins_the_alert_is_not_marked_as_delivered(ow, monkeypatch):
    from services import metrics
    import bot.utils.subscription as subs

    monkeypatch.setattr(subs, "_admin_ids", lambda: set(), raising=False)
    metrics.inc("infragram_journal_write_failures_total")
    bot_, pool, seen = _Bot(), _Pool(), {}
    asyncio.run(ow._watchdog_protective_failures(pool, bot_, seen))
    assert not seen, "алерт помечен доставленным, хотя админов нет"


def test_counters_with_labels_are_counted(ow):
    """Счётчик с метками в срезе выглядит как name{...} — его тоже надо увидеть."""
    from services import metrics

    metrics.inc("infragram_journal_write_failures_total", {"op_type": "mass_invite"})
    bot, pool, seen = _Bot(), _Pool(), {}
    asyncio.run(ow._watchdog_protective_failures(pool, bot, seen))
    assert bot.sent, "счётчик с метками остался незамеченным"


# ── Храповик: новая защитная запись обязана попадать в сообщение ─────────────

def _declared_counters() -> set[str]:
    src = open(SRC, encoding="utf-8").read()
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.Assign):
            continue
        names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if "_PROTECTIVE_FAILURE_COUNTERS" not in names:
            continue
        return {k.value for k in node.value.keys if isinstance(k, ast.Constant)}
    raise AssertionError("_PROTECTIVE_FAILURE_COUNTERS пропал из op_worker")


def _counters_in_code() -> set[str]:
    """Счётчики сбоя защитной записи, которые код действительно пишет."""
    import re

    found = set()
    for base in ("services", "database"):
        for dirpath, _d, files in os.walk(os.path.join(ROOT, base)):
            for f in files:
                if not f.endswith(".py"):
                    continue
                with open(os.path.join(dirpath, f), encoding="utf-8") as fh:
                    text = fh.read()
                found |= set(re.findall(
                    r'"(infragram_[a-z_]*(?:_failures|_undelivered|_errors)_total)"',
                    text))
    return found


def test_every_protective_failure_counter_is_surfaced():
    """Иначе новая защита снова сломается молча."""
    declared = _declared_counters()
    assert declared, "список видов сбоя пуст — сообщать больше не о чем"
    missing = sorted(_counters_in_code() - declared)
    assert not missing, (
        "эти счётчики сбоя защитной записи никому не показываются — добавьте их "
        "в _PROTECTIVE_FAILURE_COUNTERS словами владельца:\n  " + "\n  ".join(missing))


def test_the_counter_detector_bites():
    """Самопроверка: детектор обязан находить счётчик в заведомо больном тексте."""
    import re

    sick = 'await _metric("infragram_something_write_failures_total")'
    assert re.findall(r'"(infragram_[a-z_]*(?:_failures|_undelivered|_errors)_total)"',
                      sick) == ["infragram_something_write_failures_total"]


def test_the_check_runs_in_the_watchdog_cycle():
    """Проверка, которая никогда не вызывается, ничего не защищает."""
    src = open(SRC, encoding="utf-8").read()
    tree = ast.parse(src)
    run = next((n for n in tree.body
                if isinstance(n, ast.AsyncFunctionDef) and n.name == "run"), None)
    assert run is not None
    called = {getattr(c.func, "id", None) or getattr(c.func, "attr", None)
              for c in ast.walk(run) if isinstance(c, ast.Call)}
    assert "_watchdog_protective_failures" in called, (
        "сбой защитной записи снова никому не сообщается")
