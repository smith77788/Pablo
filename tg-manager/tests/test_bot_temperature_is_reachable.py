"""Температура бота обязана быть достижимой сигналами, которые бот наблюдает.

ЧТО БЫЛО. Каскад «люди → бот» складывал состояния людей, пришедших через
конкретного бота (строки с источником `bot_<id>`), и объявлял бота горячим,
когда «горячих» людей хватает по числу и по доле. «Горячим» считался человек с
рунга `ready` и выше — общий порог каскада аудитории.

Рунга `ready` человек, пришедший через бота, достичь не мог. Бот наблюдает тап,
ответ, `/start`, «стоп» и блокировку, то есть сигналы до `clicked_offer`
включительно, а он целит в `qualified` — на один рунг ниже порога. Рунги выше
ставятся только из Хранилища и CRM, и строку они пишут со своим источником, не
`bot_<id>`; сигнал слабее текущего рунга состояние вообще не трогает, так что и
подтверждением источник не подменяется.

Следствие: каскад бота не возвращал ни «горячо», ни «тепло» НИКОГДА. На каждом
проходе владелец получал пустую температуру ботов, «самый горячий бот» в сводке
не появлялся, а единственным действием каскада оставалось остужение. Проверялся
он при этом тестами на состояниях `ready` с источником `bot_<id>` — то есть на
данных, которых в проде не бывает.

ЧТО ТЕПЕРЬ. Порог каскада бота — верхний достижимый рунг канала (`qualified`,
`virtual_layer.BOT_HOT_AT`), и этот тест держит связь «порог ≤ то, что бот
умеет подать» по фактическим вызовам в `auto_responder`.
"""
from __future__ import annotations

import ast
import inspect

from services import auto_responder, virtual_layer as vl


class _Pool:
    """Отдаёт агрегат каскада и глотает записи (как в соседних тестах слоя)."""

    def __init__(self, rows):
        self.rows = rows
        self.writes: list[tuple] = []

    async def fetch(self, sql, *args):
        return self.rows

    async def fetchrow(self, sql, *args):
        return None

    async def execute(self, sql, *args):
        self.writes.append(args)


def _bot_signals() -> set[str]:
    """Сигналы, которые бот РЕАЛЬНО подаёт, — по вызовам в auto_responder.

    Разбор по AST, а не по списку в тесте: список пришлось бы обновлять руками,
    и он молча разошёлся бы с кодом — ровно та болезнь, из-за которой порог
    оказался недостижимым.
    """
    tree = ast.parse(inspect.getsource(auto_responder))
    names = {"_vl_signal", "vl_signal"}
    out: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        called = getattr(fn, "id", None) or getattr(fn, "attr", None)
        if called not in names:
            continue
        for arg in node.args[1:2]:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                out.add(arg.value)
            elif isinstance(arg, ast.IfExp):
                # `"opened" if is_start else "replied"` — обе ветви реальны.
                for branch in (arg.body, arg.orelse):
                    if isinstance(branch, ast.Constant):
                        out.add(branch.value)
    return out


def test_the_probe_finds_the_signals_bot_sends():
    """Измеритель проверяем прежде, чем судить по нему о пороге."""
    found = _bot_signals()
    assert "clicked_offer" in found, found
    assert "replied" in found and "opened" in found, found
    assert "blocked" in found, found


def test_bot_hot_threshold_is_a_rung_the_bot_can_produce():
    """Порог выше достижимого — замер, который всегда отвечает «нет»."""
    positive = {s for s in _bot_signals() if s in vl.SIGNAL_TARGET}
    assert positive, "бот не подаёт ни одного позитивного сигнала — тест устарел"
    best = max(vl.rank(vl.SIGNAL_TARGET[s]) for s in positive)
    assert vl.rank(vl.BOT_HOT_AT) <= best, (
        f"порог каскада бота ({vl.BOT_HOT_AT}) выше всего, что бот умеет подать "
        f"({vl.LADDER[best]}): температура ботов не включится никогда")


def _rows(pairs, expired=False):
    groups: dict[tuple, int] = {}
    for src, val in pairs:
        groups[(src, val)] = groups.get((src, val), 0) + 1
    return [{"source": src, "value": val, "expired": expired, "c": c}
            for (src, val), c in groups.items()]


async def test_bot_gets_hot_on_data_the_bot_can_actually_produce(monkeypatch):
    """Падало: на достижимых данных каскад возвращал пустоту."""
    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(vl, "_write", _noop)
    # Шесть тапов по офферу из пятидесяти подписчиков одного бота — это ровно
    # то, что бот способен увидеть, и это уже разговор.
    rows = _rows([("bot_7", "qualified")] * 6 + [("bot_7", "curious")] * 44)
    out = await vl.recompute_bot_cascade(_Pool(rows), 1)
    assert out.get("7") == "hot", out


async def test_cold_bot_stays_cold(monkeypatch):
    """Обратная сторона: порог не должен красить горячим кого попало."""
    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(vl, "_write", _noop)
    rows = _rows([("bot_8", "qualified")] + [("bot_8", "curious")] * 49)
    out = await vl.recompute_bot_cascade(_Pool(rows), 1)
    assert out.get("8") != "hot", out


async def test_expired_qualified_does_not_heat_the_bot(monkeypatch):
    """Просроченный рунг остывает на чтении — греть им бота нельзя."""
    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(vl, "_write", _noop)
    rows = _rows([("bot_9", "qualified")] * 6 + [("bot_9", "curious")] * 44,
                 expired=True)
    out = await vl.recompute_bot_cascade(_Pool(rows), 1)
    assert out.get("9") != "hot", out


def test_audience_cascade_keeps_the_stricter_bar():
    """У аудитории рунги выше достижимы (Хранилище, CRM) — её порог не трогаем."""
    sig = inspect.signature(vl.recompute_cascade)
    assert "hot_at" not in sig.parameters, (
        "каскад аудитории берёт порог по умолчанию cascade_from_counts")
    assert inspect.signature(vl.cascade_from_counts).parameters[
        "hot_at"].default == "ready"


# ── Просроченное состояние не греет ────────────────────────────────────────
#
# Распад на чтении опускает просроченный рунг ровно на одну ступень: в
# агрегате известно только «просрочено», а не насколько. С порогом бота на
# `qualified` этого стало недостаточно: просроченный `ready` опускается ровно
# НА порог и снова начинает греть. Поэтому правило точное — просроченная
# строка входит в знаменатель (человек остыл, а не исчез) и не входит в
# числитель вовсе.

async def test_expired_ready_does_not_heat_the_bot(monkeypatch):
    """Падало бы от одного понижения рунга: `ready` минус ступень = порог."""
    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(vl, "_write", _noop)
    rows = _rows([("bot_11", "ready")] * 6, expired=True)
    rows += _rows([("bot_11", "curious")] * 44)
    out = await vl.recompute_bot_cascade(_Pool(rows), 1)
    assert out.get("11") != "hot", out


def test_expired_rows_stay_in_the_denominator():
    """Остывший человек из аудитории не исчез — доля горячих это чувствует."""
    counts = {"qualified": 6, "curious": 44}
    live = {"qualified": 6}
    assert vl.cascade_from_counts(counts, hot_at=vl.BOT_HOT_AT, min_count=5,
                                  min_share=0.1, live=live) == "hot"
    # Те же шесть горячих, но среди тысячи остывших — это не горячий бот.
    assert vl.cascade_from_counts({"qualified": 6, "curious": 994},
                                  hot_at=vl.BOT_HOT_AT, min_count=5,
                                  min_share=0.1, live=live) != "hot"


def test_pure_cascade_without_expiry_knows_nothing_about_it():
    """Чистой `cascade` срок не передают — она считает все значения живыми."""
    assert vl.cascade([vl.BOT_HOT_AT] * 6 + ["curious"] * 4,
                      hot_at=vl.BOT_HOT_AT, min_count=5, min_share=0.1) == "hot"
