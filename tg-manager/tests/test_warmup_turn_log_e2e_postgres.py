"""Журнал ходов разогрева — на НАСТОЯЩЕМ Postgres.

ЗАЧЕМ ОТДЕЛЬНО. Заглушка пула принимает любые параметры, поэтому ошибки
СВЯЗЫВАНИЯ (строка вместо timestamptz, int вместо text) на юнит-тестах невидимы
в принципе — так пережил релиз сломанный `operation_bus.submit(scheduled_for=…)`,
убивавший все отложенные операции продукта.

Здесь цена такой ошибки та же, что и у самой находки: `_log` намеренно не
бросает, чтобы журнал не сорвал ход. Значит сломанный INSERT не упал бы и не
заметился — разогрев снова стал бы немым, причём с зелёными тестами. А сводка по
аккаунтам (GROUP BY с SUM(CASE…) и склейкой подписи) на заглушке не выполняется
вовсе.

КАК ЗАПУСТИТЬ — см. докстринг tests/test_invite_e2e_postgres.py.
"""
from __future__ import annotations

import asyncio
import glob
import os
import re

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(
    not DSN, reason="нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")

OWNER = 990301
_LOOP: "asyncio.AbstractEventLoop | None" = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


@pytest.fixture(scope="module")
def pool():
    import asyncpg

    async def _boot():
        conn = await asyncpg.connect(DSN)
        files = ["schema.sql"] + sorted(
            glob.glob("schema_v*.sql"),
            key=lambda p: int(re.search(r"schema_v(\d+)", p).group(1)))
        for f in files:
            try:
                await conn.execute(open(f, encoding="utf-8").read())
            except Exception:
                pass  # схемы идемпотентны
        await conn.close()
        return await asyncpg.create_pool(DSN, min_size=1, max_size=4)

    try:
        p = _run(_boot())
    except Exception as exc:
        pytest.skip(f"Postgres по INFRAGRAM_TEST_DSN недоступен: {str(exc)[:120]}")
    yield p
    _run(p.close())


async def _fresh(p) -> dict:
    """Чистая сессия разогрева с двумя аккаунтами флота."""
    from services import chat_warmup as cw

    await p.execute("DELETE FROM chat_warmup_log WHERE owner_id=$1", OWNER)
    await p.execute("DELETE FROM chat_warmup_sessions WHERE owner_id=$1", OWNER)
    await p.execute("DELETE FROM tg_accounts WHERE owner_id=$1", OWNER)
    ids = []
    for i, name in enumerate(("lora_m", "alik_v")):
        ids.append(await p.fetchval(
            "INSERT INTO tg_accounts(owner_id,phone,session_str,is_active,acc_status,"
            "username) VALUES($1,$2,$3,TRUE,'active',$4) RETURNING id",
            OWNER, f"+7991{i:07d}", f"s{i}", name))
    s = await cw.create_session(p, OWNER, "t.me/savemod_chat", ids,
                                mode="mixed", topics="", intensity="normal")
    s["account_ids"] = ids
    return s


def test_a_turn_really_lands_in_the_table(pool):
    from services import chat_warmup as cw

    async def _t():
        s = await _fresh(pool)
        await cw._log(pool, s, "ok", kind="reply", text="привет, как сам?",
                      account_id=s["account_ids"][0], reply_to="Лора: всем привет")
        row = await pool.fetchrow(
            "SELECT * FROM chat_warmup_log WHERE session_id=$1", int(s["id"]))
        assert row is not None, (
            "удачный ход не попал в журнал: _log не бросает, поэтому сломанный "
            "INSERT был бы невидим, а разогрев снова немым"
        )
        assert row["text"] == "привет, как сам?"
        assert row["reply_to"] == "Лора: всем привет"
        assert row["status"] == "ok" and row["kind"] == "reply"
        assert row["repeats"] == 1
        assert row["created_at"] is not None

    _run(_t())


def test_repeated_silence_collapses_on_a_live_table(pool):
    from services import chat_warmup as cw

    async def _t():
        s = await _fresh(pool)
        for _ in range(5):
            await cw._log(pool, s, "error", "Не удалось войти в чат",
                          account_id=s["account_ids"][0])
        rows = await pool.fetch(
            "SELECT repeats FROM chat_warmup_log WHERE session_id=$1", int(s["id"]))
        assert len(rows) == 1, (
            f"пять одинаковых пропусков дали {len(rows)} строк — за сутки такая "
            f"сессия забьёт таблицу"
        )
        assert rows[0]["repeats"] == 5

    _run(_t())


def test_the_detail_query_runs_and_counts(pool):
    """Тот же SQL, что отдаёт экран: сводка по аккаунтам и журнал."""
    from services import chat_warmup as cw

    async def _t():
        s = await _fresh(pool)
        a1, a2 = s["account_ids"]
        await cw._log(pool, s, "ok", kind="reply", text="раз", account_id=a1)
        await cw._log(pool, s, "ok", kind="seed", text="два", account_id=a1)
        await cw._log(pool, s, "ok", kind="reply", text="три", account_id=a2)
        for _ in range(3):
            await cw._log(pool, s, "skipped", "Аккаунт в карантине по здоровью",
                          account_id=a2)

        by_acc = await pool.fetch(
            """SELECT l.account_id,
                      COALESCE(a.username, a.first_name, a.phone, '#' || l.account_id::text) AS label,
                      COUNT(*) FILTER (WHERE l.status='ok') AS sent,
                      SUM(CASE WHEN l.status<>'ok' THEN l.repeats ELSE 0 END) AS missed,
                      MAX(l.created_at) AS last_at
                 FROM chat_warmup_log l
                 LEFT JOIN tg_accounts a ON a.id = l.account_id
                WHERE l.session_id=$1 AND l.owner_id=$2 AND l.account_id IS NOT NULL
                GROUP BY l.account_id, a.username, a.first_name, a.phone
                ORDER BY sent DESC, missed DESC""", int(s["id"]), OWNER)

        got = {r["label"]: (int(r["sent"]), int(r["missed"] or 0)) for r in by_acc}
        assert got == {"lora_m": (2, 0), "alik_v": (1, 3)}, got
        assert all(r["last_at"] is not None for r in by_acc)

    _run(_t())


def test_the_journal_is_scoped_to_its_owner(pool):
    from services import chat_warmup as cw

    async def _t():
        s = await _fresh(pool)
        await cw._log(pool, s, "ok", text="чужое", account_id=s["account_ids"][0])
        rows = await pool.fetch(
            "SELECT id FROM chat_warmup_log WHERE session_id=$1 AND owner_id=$2",
            int(s["id"]), OWNER + 1)
        assert not rows, "журнал читается под чужим владельцем"

    _run(_t())


def test_a_turn_without_an_account_is_allowed(pool):
    """Ход может не дойти до выбора аккаунта — строка всё равно обязана лечь."""
    from services import chat_warmup as cw

    async def _t():
        s = await _fresh(pool)
        await cw._log(pool, s, "skipped",
                      "В сессии не выбрано ни одного аккаунта флота")
        row = await pool.fetchrow(
            "SELECT account_id, reason FROM chat_warmup_log WHERE session_id=$1",
            int(s["id"]))
        assert row is not None and row["account_id"] is None
        assert "не выбрано" in row["reason"]

    _run(_t())


def test_the_live_diagnosis_names_each_blocked_account(pool):
    """Почему молчит СЕЙЧАС — по живым данным, теми же гейтами, что и ход."""
    from services import chat_warmup as cw

    async def _t():
        s = await _fresh(pool)
        a1, a2 = s["account_ids"]
        # Один аккаунт на паузе Telegram, второй занят операцией.
        await pool.execute(
            "UPDATE tg_accounts SET cooldown_until = now() + interval '40 minutes' "
            "WHERE id=$1", a1)
        await pool.execute("UPDATE tg_accounts SET in_operation=TRUE WHERE id=$1", a2)

        d = await cw.diagnose(pool, OWNER, s)
        reasons = {a["label"]: a["reason"] for a in d["accounts"]}
        assert d["ready"] == 0
        assert "пауза Telegram" in reasons["lora_m"], reasons
        assert "занят операцией" in reasons["alik_v"], reasons
        assert any("Ни один аккаунт" in g for g in d["global"])

        # Снимаем паузу у одного — он снова готов, и это видно.
        await pool.execute("UPDATE tg_accounts SET cooldown_until=NULL WHERE id=$1", a1)
        d2 = await cw.diagnose(pool, OWNER, s)
        assert d2["ready"] == 1
        assert not any("Ни один аккаунт" in g for g in d2["global"])

    _run(_t())


def test_a_dead_account_is_named_by_its_status(pool):
    from services import chat_warmup as cw

    async def _t():
        s = await _fresh(pool)
        await pool.execute("UPDATE tg_accounts SET acc_status='spamblock' WHERE id=$1",
                           s["account_ids"][0])
        d = await cw.diagnose(pool, OWNER, s)
        reasons = {a["label"]: a["reason"] for a in d["accounts"]}
        assert "spamblock" in reasons["lora_m"], reasons


def test_the_diagnosis_is_scoped_to_its_owner(pool):
    from services import chat_warmup as cw

    async def _t():
        s = await _fresh(pool)
        d = await cw.diagnose(pool, OWNER + 1, s)
        # Чужому владельцу аккаунты сессии не принадлежат — их просто нет.
        assert all(not a["ok"] for a in d["accounts"])
        assert all("удалён или сменил владельца" in a["reason"] for a in d["accounts"])

    _run(_t())


def test_the_diagnosis_never_breaks_the_screen():
    """Сломанная диагностика не имеет права уронить экран хода."""
    from services import chat_warmup as cw

    class _Boom:
        async def fetch(self, *a, **k):
            raise RuntimeError("БД недоступна")

    out = _run(cw.diagnose(_Boom(), 1, {"account_ids": [1, 2]}))
    assert isinstance(out, dict) and "accounts" in out
