"""Виртуальный слой: распад догоняет молчание, а каскад умеет остывать.

ЧТО БЫЛО. Слой упрекает тег в том, что тег врёт («интерес, о котором сутки
ничего не слышно, — уже не интерес»), и делал ровно то же сам, в трёх местах:

1. `decay` опускал состояние НА ОДИН рунг за проход и выдавал новый срок от
   момента прохода, а не от прежнего срока. Человек, молчащий месяц, съезжал
   с «готов купить» на «квалифицирован» и получал свежие 72 часа жизни, потом
   на «интерес» — ещё 96 часов, и так дальше: полный остыв занимал две недели
   КАЛЕНДАРЯ после месяца тишины.
2. Дойдя до дна лестницы, строка оставляла просроченный срок. Выборка
   `run_decay` («просрочено и не NULL») возвращала её снова и снова, распадаться
   ей было уже некуда, и такие строки копились, занимая лимит прохода (500 на
   всех владельцев). Распад переставал доходить до тех, кому он нужен, —
   то есть выключался сам, тихо и навсегда.
3. Каскад читал сырое значение, срок игнорировал, и при вердикте «уже не
   горячо» просто выходил, не тронув прежний. Поэтому «кампания горячая»
   держалась на людях, пропавших недели назад, а бот, остывший полгода назад,
   навсегда оставался самым горячим — и подсказка мозга «направьте оффер
   сюда» вместе с ним.

ЧТО ТЕПЕРЬ. Распад опускает на столько рунгов, сколько молчания накопилось,
считая каждый следующий срок от предыдущего; на дне снимает срок; читатели
каскада идут через `effective`; остывший родитель получает COLD.
"""
from __future__ import annotations

import pathlib
from datetime import datetime, timedelta, timezone

from services import virtual_layer as V
from services.organism import spine

ROOT = pathlib.Path(__file__).resolve().parents[1]
UI = (ROOT / "mini_app" / "index.html").read_text(encoding="utf-8")

T0 = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)


def _st(value, *, conf=0.8, exp_h=None, source=None):
    return {"value": value, "confidence": conf, "source": source,
            "expires_at": (T0 + timedelta(hours=exp_h)) if exp_h is not None else None}


def setup_function(_):
    spine._clear()


# ── 1. Распад догоняет накопленное молчание ────────────────────────────────

def test_month_of_silence_cools_all_the_way_down():
    """Месяц тишины — это не «квалифицирован», это дно лестницы."""
    ch = V.decay(_st("ready", exp_h=-24 * 30), now=T0)
    assert ch is not None, "просроченное на месяц состояние не распалось"
    assert ch["value"] == "new", (
        f"за один проход опустились только до «{ch['value']}»: распад считает "
        "рунги, а не время, и месяц молчания читается как интерес")


def test_each_rung_counts_from_the_previous_deadline_not_from_now():
    """Срок следующего рунга — от прежнего срока. Иначе проход дарит жизнь.

    «Готов» (24 ч) просрочен на 73 часа. Честно: 24 ч истекли, минус один
    рунг → «квалифицирован» на 72 ч, они тоже истекли (73 > 72) → минус ещё
    один рунг, «интерес». Если считать от `now`, остановились бы на
    «квалифицирован» и выдали ему 72 свежих часа.
    """
    ch = V.decay(_st("ready", exp_h=-73), now=T0)
    assert ch["value"] == "interested", ch
    assert ch["expires_at"] > T0, "остаток срока у живого рунга должен быть в будущем"
    assert ch["expires_at"] < T0 + V.ttl_for("interested"), (
        "срок выдан от момента прохода: тишина, уже накопленная человеком, "
        "ему прощена")


def test_one_hour_overdue_still_drops_exactly_one_rung():
    """Поведение на свежей просрочке не меняется — догон не значит обвал."""
    ch = V.decay(_st("ready", exp_h=-1), now=T0)
    assert ch["value"] == "qualified" and ch["confidence"] < 0.8


def test_fresh_state_is_left_alone():
    assert V.decay(_st("ready", exp_h=+5), now=T0) is None


# ── 2. Дно лестницы уходит из окна распада ─────────────────────────────────

def test_bottom_rung_gives_up_its_deadline():
    """Иначе строка навсегда остаётся в выборке run_decay и занимает лимит."""
    ch = V.decay(_st("new", exp_h=-5), now=T0)
    assert ch is not None, (
        "строка на дне с просроченным сроком не правится никогда — выборка "
        "распада возвращает её каждый проход и лимит достаётся ей, а не тем, "
        "кому распад нужен")
    assert ch["expires_at"] is None, "срок на дне обязан сниматься"
    assert ch["value"] == "new" and ch["value_changed"] is False


def test_bottom_rung_without_deadline_is_quiet():
    assert V.decay(_st("new"), now=T0) is None


def test_terminal_never_decays():
    assert V.decay(_st("purchased", exp_h=-999), now=T0) is None
    assert V.decay(_st(V.LOST, exp_h=-999), now=T0) is None


class _DecayPool:
    """Отдаёт строки распада и записывает SQL с аргументами."""

    def __init__(self, rows):
        self.rows = rows
        self.queries: list[str] = []
        self.states: list[tuple] = []
        self.history: list[tuple] = []

    async def fetch(self, sql, *args):
        self.queries.append(sql)
        return self.rows

    async def execute(self, sql, *args):
        self.queries.append(sql)
        if "INSERT INTO virtual_states" in sql:
            self.states.append(args)
        elif "virtual_state_history" in sql:
            self.history.append(args)


async def test_run_decay_clears_the_deadline_without_writing_history():
    """Снятие срока — не переход: история переходов не должна его видеть.

    `temporal_pattern` («заходил и уходил») считается именно по истории: строка
    «из new в new» исказила бы рисунок, по которому слой принимает решение.
    """
    pool = _DecayPool([{"owner_id": 7, "entity_type": V.USER, "entity_id": "c1",
                        "state_key": "funnel", "value": "new", "confidence": 0.3,
                        "expires_at": T0 - timedelta(hours=9), "source": "bot_5"}])
    n = await V.run_decay(pool, 7, now=T0)
    assert n == 0, "снятие срока — не остывание, в счёт остывших не идёт"
    assert len(pool.states) == 1, "срок так и не сняли — строка вернётся в окно"
    assert pool.states[0][7] is None, f"записан срок {pool.states[0][7]!r}, ждали NULL"
    assert pool.history == [], "переход из new в new попал в историю"


async def test_run_decay_takes_the_longest_overdue_first():
    """Лимит прохода — 500 на всех владельцев: порядок решает, кому он достанется."""
    pool = _DecayPool([])
    await V.run_decay(pool, 7, now=T0)
    assert any("ORDER BY expires_at" in q for q in pool.queries), (
        "выборка распада без порядка: лимит достаётся случайным строкам, и "
        "давно просроченные могут не попасть в него никогда")


# ── 3. effective: читатель видит состояние, каким оно ЕСТЬ ─────────────────

def test_effective_applies_decay_without_writing():
    assert V.effective(_st("ready", exp_h=-24 * 30), now=T0)["value"] == "new"


def test_effective_leaves_a_fresh_state_as_is():
    st = _st("ready", exp_h=+5)
    assert V.effective(st, now=T0)["value"] == "ready"


def test_effective_of_nothing_is_nothing():
    assert V.effective(None) is None


# ── 4. Каскад считает по живым состояниям ──────────────────────────────────

class _CascadePool:
    """Дети-контакты (со сроками) + одно состояние родителя."""

    def __init__(self, children, parent=None):
        self.children = children
        self.parent = parent
        self.writes: list[str] = []

    async def fetch(self, sql, *args):
        if "FROM virtual_states" in sql and "COUNT" not in sql:
            return self.children
        return []

    async def fetchrow(self, sql, *args):
        if "FROM virtual_states" in sql and self.parent:
            return {"value": self.parent, "confidence": 0.8,
                    "source": "cascade", "expires_at": None}
        return None

    async def execute(self, sql, *args):
        if "INSERT INTO virtual_states" in sql:
            self.parent = args[4]
            self.writes.append(args[4])


async def test_expired_children_do_not_heat_the_parent():
    """«Горячая кампания» на людях, пропавших месяц назад, — это неправда."""
    kids = [_st("ready", exp_h=-24 * 30) for _ in range(30)] + \
           [_st("new") for _ in range(70)]
    pool = _CascadePool(kids)
    v = await V.recompute_cascade(pool, 7, V.NETWORK, "audience", V.USER,
                                  min_count=20, min_share=0.15, now=T0)
    assert v != "hot", (
        "каскад взял сырое значение и срок проигнорировал: аудитория «горячая» "
        "по тем, кто давно остыл")


async def test_fresh_children_still_heat_the_parent():
    kids = [_st("ready", exp_h=+5) for _ in range(30)] + \
           [_st("new") for _ in range(70)]
    pool = _CascadePool(kids)
    v = await V.recompute_cascade(pool, 7, V.NETWORK, "audience", V.USER,
                                  min_count=20, min_share=0.15, now=T0)
    assert v == "hot" and pool.parent == "hot"


# ── 5. Каскад снимает прежнее «горячо» ─────────────────────────────────────

async def test_cooled_audience_loses_its_hot_label():
    seen = []
    spine.on("*", lambda o, k, p, pool: seen.append(k) or None)
    pool = _CascadePool([_st("new") for _ in range(100)], parent="hot")
    v = await V.recompute_cascade(pool, 7, V.NETWORK, "audience", V.USER,
                                  min_count=20, min_share=0.15, now=T0)
    assert v == V.COLD, (
        "готовых больше нет, а в базе осталось «горячая»: экран, подсказка "
        "мозга и «самый горячий бот» читают именно её")
    assert pool.parent == V.COLD
    assert "network_became_cold" in seen


async def test_cascade_does_not_invent_a_parent_it_never_touched():
    """Нет вердикта и нет прежнего значения — писать нечего."""
    pool = _CascadePool([_st("new") for _ in range(100)])
    v = await V.recompute_cascade(pool, 7, V.NETWORK, "audience", V.USER,
                                  min_count=20, min_share=0.15, now=T0)
    assert v is None and pool.writes == []


async def test_cascade_does_not_overwrite_a_funnel_value():
    """COLD ставим только поверх своих же температур, не поверх рунга воронки."""
    pool = _CascadePool([_st("new") for _ in range(100)], parent="interested")
    v = await V.recompute_cascade(pool, 7, V.USER, "c1", V.USER,
                                  min_count=20, min_share=0.15, now=T0)
    assert v is None and pool.writes == []


# ── 6. То же для каскада «бот ← люди» ──────────────────────────────────────

class _BotPool(_CascadePool):
    async def fetchrow(self, sql, *args):
        if "FROM virtual_states" in sql and self.parent:
            return {"value": self.parent, "confidence": 0.8,
                    "source": "cascade", "expires_at": None}
        return None


async def test_bot_cools_down_with_its_audience():
    pool = _BotPool([_st("new", source="bot_10") for _ in range(50)], parent="hot")
    out = await V.recompute_bot_cascade(pool, 7, now=T0)
    assert out.get("10") == V.COLD, (
        f"бот остался {out.get('10')!r}: его горячие контакты остыли, а "
        "«самый горячий бот» и подсказка «направьте оффер сюда» живут дальше")


async def test_bot_is_not_heated_by_expired_contacts():
    pool = _BotPool([_st("ready", exp_h=-24 * 30, source="bot_10") for _ in range(6)]
                    + [_st("curious", source="bot_10") for _ in range(4)])
    out = await V.recompute_bot_cascade(pool, 7, now=T0)
    assert out.get("10") != "hot", "бот греется просроченными состояниями"


# ── 7. Поверхности владельца понимают третью температуру ───────────────────

def test_cold_has_a_russian_label():
    assert V.TEMPERATURE_LABEL[V.COLD] == "🧊 Остыл"


def test_overview_hot_list_skips_expired():
    import inspect
    src = inspect.getsource(V.overview)
    assert "expires_at IS NULL OR expires_at > now()" in src, (
        "список «кого дожимать первыми» берёт просроченные рунги: человек, "
        "пропавший три недели назад, стоит в нём первым")


def test_miniapp_knows_the_cold_audience():
    assert "Аудитория остыла" in UI, (
        "мини-апп не знает третьей температуры: остывшая аудитория покажется "
        "«тёплой», то есть ровно наоборот"
    )
    assert "cold ? '🧊'" in UI or "'cold'" in UI
