"""Кнопки бота не меняют чужое по id из callback_data.

callback_data пишет клиент Telegram: модифицированный клиент шлёт боту любую
строку для любой кнопки, которую бот ему показал. «Пауза», «Возобновить»,
«Удалить» и «Оценить» в Stars Hub брали experiment_id оттуда и меняли запись
БЕЗ owner_id: подобрав id, можно было удалить или остановить чужой
эксперимент. Тест падает без фикса.
"""
from __future__ import annotations

import ast
import asyncio
import glob
import os
import re
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _Pool:
    def __init__(self):
        self.sql = []

    async def execute(self, q, *a):
        self.sql.append((q, a))

    async def fetchrow(self, q, *a):
        self.sql.append((q, a))
        return None

    async def fetchval(self, q, *a):
        self.sql.append((q, a))
        return None

    async def fetch(self, q, *a):
        self.sql.append((q, a))
        return []


class _Msg:
    async def edit_text(self, *a, **k):
        pass


class _Cb:
    def __init__(self, uid):
        self.from_user = SimpleNamespace(id=uid)
        self.message = _Msg()
        self.answers = []

    async def answer(self, *a, **k):
        self.answers.append(a)


def test_stars_mutations_are_owner_scoped(monkeypatch):
    from bot.handlers import stars_hub as h

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(h, "cb_detail", _noop)
    monkeypatch.setattr(h, "cb_list_experiments", _noop)
    called = []

    async def _eval(pool, exp_id):
        called.append(exp_id)
        return {}

    monkeypatch.setattr(h.stars_optimizer, "evaluate_experiment", _eval)
    data = SimpleNamespace(experiment_id=777, bot_id=0)
    for fn in (h.cb_pause, h.cb_resume, h.cb_delete_do, h.cb_delete_confirm, h.cb_evaluate):
        pool = _Pool()
        asyncio.run(fn(_Cb(42), data, pool))
        writes = [(q, a) for q, a in pool.sql if re.search(r"\b(UPDATE|DELETE)\b", q)]
        for q, a in writes:
            assert "owner_id" in q and 42 in a, (fn.__name__, q)
    assert called == [], "чужой эксперимент оценён"


# Обработчики, у которых права проверены выше по стеку (вся админка — за
# _is_admin в диспетчере), и фоновые помощники над уже отобранными строками.
_ALLOWED = {
    "bot/handlers/admin.py",
    "bot/handlers/competitors.py:_refresh_bg",
}
_OWN = re.compile(r"owner_id|added_by|user_id|created_by|telegram_id|_owns|_own_|_mine"
                  r"|can_use|_check_owner|_get_owned|belongs|_is_owner|owner="
                  # выборка «своего» по id пользователя: _get_funnel(pool, id, from_user.id)
                  r"|\(pool,[^)\n]*from_user\.id", re.I)


def test_no_new_callback_handlers_write_without_owner():
    bad = []
    for f in sorted(glob.glob(os.path.join(ROOT, "bot", "handlers", "*.py"))):
        rel = os.path.relpath(f, ROOT).replace(os.sep, "/")
        if rel in _ALLOWED:
            continue
        src = open(f, encoding="utf-8").read()
        for n in ast.walk(ast.parse(src)):
            if not isinstance(n, ast.AsyncFunctionDef):
                continue
            if f"{rel}:{n.name}" in _ALLOWED:
                continue
            body = ast.get_source_segment(src, n) or ""
            if not re.search(r"\b(callback|cb|call|query)\.data\b|callback_data", body):
                continue
            if "_is_admin(" in body:
                continue
            writes = [c.value for c in ast.walk(n) if isinstance(c, ast.Constant)
                      and isinstance(c.value, str)
                      and re.search(r"\b(UPDATE|DELETE FROM)\b", c.value)]
            if writes and not _OWN.search(body):
                bad.append(f"{rel}:{n.lineno} {n.name}")
    assert not bad, "запись по id из кнопки без проверки владельца: " + ", ".join(bad)


def test_node_thread_and_cluster_picks_are_owner_scoped():
    """Топик узла и кластер фабрики каналов по id из кнопки — только свои."""
    src_nodes = open(os.path.join(ROOT, "bot", "handlers", "nodes_hub.py"), encoding="utf-8").read()
    i = src_nodes.index("async def cb_thread_view(")
    body = src_nodes[i:src_nodes.index("\nasync def ", i + 10)]
    assert "n.owner_id=$3" in body
    src_cf = open(os.path.join(ROOT, "bot", "handlers", "channel_factory.py"), encoding="utf-8").read()
    i = src_cf.index("async def cb_chanf_pick_cluster(")
    body = src_cf[i:src_cf.index("\nasync def ", i + 10)]
    assert "FROM clusters WHERE id=$1 AND owner_id=$2" in body
    assert "if cl else None" in body
