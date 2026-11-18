"""«Убрать бота» больше не стирает его аудиторию.

На managed_bots(bot_id) висит десяток таблиц с ON DELETE CASCADE: bot_users,
funnels, auto_replies, broadcasts, статистика. Пока delete_bot делал DELETE,
нажатие «Удалить» стирало всю базу подписчиков и всю историю — безвозвратно,
потому что удалённую строку неоткуда взять. Продукт эту цену уже признал
слишком высокой: ради неё появилась replace_bot_token («молчание бота лечилось
ценой базы подписчиков»), а Mini App удаляет бота снятием is_active.

То есть две двери вели в разные места, и хуже того — обе выглядели сломанными:

* в списке бота условие было записано как «свои ИЛИ экосистемные ИЛИ (по
  workspace И активные)». AND связывает крепче OR, поэтому на СВОИ боты
  фильтр is_active не действовал: бот, удалённый в Mini App, продолжал висеть
  в списке бота, и «пауза» там тоже ничего не меняла;
* add_bot на свою выключенную строку отвечал «уже добавлен», так что бота,
  отключённого после мёртвого токена, нельзя было вернуть вообще.

Теперь: убрали — строка осталась, бот пропал из списков; добавили тем же
токеном — вернулся вместе с данными. Стирание осталось ровно в одном месте:
бота забирает ДРУГОЙ владелец, и чужая аудитория ему не достаётся.
"""
from __future__ import annotations

import asyncio
import inspect
import re

from database import db


def _run(coro):
    return asyncio.run(coro)


class _Pool:
    def __init__(self, existing=None, execute_result="UPDATE 1"):
        self.existing = existing
        self.execute_result = execute_result
        self.statements: list[tuple[str, tuple]] = []

    async def execute(self, q, *a):
        self.statements.append((" ".join(q.split()), a))
        return self.execute_result

    async def fetchrow(self, q, *a):
        self.statements.append((" ".join(q.split()), a))
        return self.existing

    async def fetchval(self, q, *a):
        self.statements.append((" ".join(q.split()), a))
        return 0

    def sql(self) -> str:
        return " | ".join(q for q, _ in self.statements)


# ── Убрать бота ───────────────────────────────────────────────────────────────

def test_remove_does_not_delete_the_row():
    pool = _Pool()
    assert _run(db.delete_bot(pool, bot_id=7, added_by=1)) is True
    sql = pool.sql()
    assert "DELETE FROM managed_bots" not in sql, (
        "строка удаляется — каскадом уйдут подписчики, воронки и история"
    )
    assert "UPDATE managed_bots SET is_active=FALSE" in sql


def test_remove_is_owner_scoped():
    pool = _Pool()
    _run(db.delete_bot(pool, bot_id=7, added_by=1))
    q, args = pool.statements[0]
    assert "added_by=$2" in q and args == (7, 1)


def test_remove_reports_failure_when_nothing_changed():
    pool = _Pool(execute_result="UPDATE 0")
    assert _run(db.delete_bot(pool, bot_id=7, added_by=1)) is False


# ── Вернуть бота ──────────────────────────────────────────────────────────────

def test_readding_own_disabled_bot_revives_the_same_row():
    pool = _Pool(existing={"added_by": 1, "is_active": False})
    assert _run(db.add_bot(pool, "1:tok", 7, "u", "n", 1)) is True
    sql = pool.sql()
    assert "UPDATE managed_bots" in sql and "is_active=TRUE" in sql
    assert "INSERT INTO managed_bots" not in sql, "заводится новая строка вместо своей"
    assert "DELETE FROM managed_bots" not in sql


def test_revived_bot_gets_a_fresh_token_and_clean_health():
    """Возвращают бота обычно именно из-за мёртвого токена."""
    pool = _Pool(existing={"added_by": 1, "is_active": False})
    _run(db.add_bot(pool, "1:newtok", 7, "u", "n", 1))
    upd = [q for q, _ in pool.statements if "UPDATE managed_bots" in q][0]
    assert "token=$3" in upd
    assert "fail_streak=0" in upd and "last_error=NULL" in upd


def test_token_of_revived_bot_is_encrypted():
    pool = _Pool(existing={"added_by": 1, "is_active": False})
    _run(db.add_bot(pool, "1:newtok", 7, "u", "n", 1))
    args = [a for q, a in pool.statements if "UPDATE managed_bots" in q][0]
    assert any(isinstance(x, str) and x.startswith("ENC:") for x in args), (
        "токен уходит в базу открытым"
    )


def test_active_own_bot_is_still_reported_as_already_added():
    pool = _Pool(existing={"added_by": 1, "is_active": True})
    assert _run(db.add_bot(pool, "1:tok", 7, "u", "n", 1)) is False
    assert "UPDATE managed_bots" not in pool.sql()


def test_active_foreign_bot_is_still_taken():
    pool = _Pool(existing={"added_by": 2, "is_active": True})
    assert _run(db.add_bot(pool, "1:tok", 7, "u", "n", 1)) == "taken"
    assert "DELETE FROM managed_bots" not in pool.sql()


def test_foreign_disabled_bot_is_handed_over_clean():
    """Прежний владелец бота у себя убрал — новый получает его без чужих данных."""
    pool = _Pool(existing={"added_by": 2, "is_active": False}, execute_result="INSERT 0 1")
    assert _run(db.add_bot(pool, "1:tok", 7, "u", "n", 1)) is True
    sql = pool.sql()
    assert "DELETE FROM managed_bots" in sql, "чужая аудитория досталась новому владельцу"
    assert sql.index("DELETE FROM managed_bots") < sql.index("INSERT INTO managed_bots")


# ── Список ботов ──────────────────────────────────────────────────────────────

def test_bot_list_filter_applies_to_every_branch():
    """is_active обязан относиться ко всем веткам OR, а не только к последней.

    Проверяем именно скобки: от начала внешнего WHERE до `AND m.is_active`
    они обязаны сойтись в ноль. Иначе AND, который связывает крепче OR,
    прилипнет к последней ветке, и на свои боты фильтр не подействует.
    """
    src = inspect.getsource(db.get_bots)
    start = src.index("WHERE (m.added_by=$1") if "WHERE (m.added_by=$1" in src else -1
    assert start >= 0, "внешний WHERE не начинается со скобки — группа OR не сгруппирована"
    end = src.index("AND m.is_active=TRUE", start)
    group = src[start + len("WHERE "):end]
    assert group.count("(") == group.count(")"), (
        f"скобки не сходятся до AND m.is_active — {' '.join(group.split())[:140]}"
    )


def test_bot_list_still_decrypts_tokens():
    src = inspect.getsource(db.get_bots)
    assert "_dec_bot_rows" in src
