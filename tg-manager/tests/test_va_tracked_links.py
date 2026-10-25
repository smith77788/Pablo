"""Витрина считает, сколько людей каждый пост привёл в основной канал.

Администратор учился только на просмотрах, а задача витрины — вступления в
основной канал. Если «куда вести клиентов» — @канал владельца в Infragram,
продающий пост получает свою ссылку-приглашение, Telegram считает вступления
по ней, а отчёт и выбор лучшей рубрики опираются на них.
"""
from __future__ import annotations

import asyncio
import os

import pytest

from services import va_links as vl

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run(coro):
    return asyncio.run(coro)


@pytest.mark.parametrize("contact,expected", [
    ("@main_shop", "main_shop"),
    ("t.me/main_shop", "main_shop"),
    ("https://t.me/main_shop/", "main_shop"),
    ("https://t.me/+AbCdEf123", None),       # приватное приглашение — не посчитать
    ("https://t.me/joinchat/AbCd", None),
    ("shop.ru", None),
    ("+7 999 123-45-67", None),
    ("", None),
])
def test_target_username(contact, expected):
    assert vl.target_username(contact) == expected


def test_apply_link_replaces_every_form_and_only_whole_name():
    text = ("Заходите в @main_shop или https://t.me/main_shop. "
            "А @main_shop_news — другой канал, t.me/main_shopx тоже.")
    out = vl.apply_link(text, "main_shop", "https://t.me/+LINK")
    assert out.count("https://t.me/+LINK") == 2
    assert "@main_shop_news" in out and "t.me/main_shopx" in out
    assert "https://https://" not in out


class _Pool:
    def __init__(self, managed=True):
        self.links: dict[int, dict] = {}
        self.managed = managed

    async def fetchrow(self, q, *a):
        if "FROM managed_channels" in q:
            return {"channel_id": 555} if self.managed and a[1].lower() == "main_shop" else None
        if "INSERT INTO va_tracked_links" in q:
            i = len(self.links) + 1
            self.links[i] = {"id": i, "owner_id": a[0], "channel_id": a[1],
                             "target_channel_id": a[2], "pillar": a[3], "link": None,
                             "op_id": None, "joins": 0, "requests": 0}
            return {"id": i}
        raise AssertionError(q)

    async def execute(self, q, *a):
        if q.startswith("DELETE FROM va_tracked_links"):
            r = self.links.get(a[0])
            if r and ("op_id IS NULL" not in q or r["op_id"] is None):
                self.links.pop(a[0])
        elif "SET link=" in q:
            self.links[a[0]]["link"] = a[1]
        elif "SET op_id=" in q:
            self.links[a[0]]["op_id"] = a[1]
        elif "SET joins=" in q:
            self.links[a[0]].update(joins=a[1], requests=a[2])
        else:
            raise AssertionError(q)

    async def fetch(self, q, *a):
        if "SELECT id, target_channel_id, link" in q:
            return [r for r in self.links.values() if r["link"]]
        if "GROUP BY pillar" in q:
            out: dict = {}
            for r in self.links.values():
                if r["link"] and r["op_id"]:
                    g = out.setdefault(r["pillar"], {"pillar": r["pillar"], "links": 0,
                                                     "joins": 0, "requests": 0})
                    g["links"] += 1
                    g["joins"] += r["joins"]
                    g["requests"] += r["requests"]
            return list(out.values())
        raise AssertionError(q)


@pytest.fixture
def tg(monkeypatch):
    from services import account_manager as am
    from services import channel_admin as ca
    state = {"exported": [], "usage": {}}

    async def get_admin(pool, owner_id, channel_id):
        return {"lead_contact": "@main_shop"}

    async def channel_account(pool, owner_id, channel_id):
        return {"id": 1, "session_str": "s", "access_hash": 9, "username": "main_shop"}

    async def export(session, channel_id, *, title, **kw):
        state["exported"].append((channel_id, title))
        return f"https://t.me/+L{len(state['exported'])}"

    async def usage(session, channel_id, links, **kw):
        return {lk: state["usage"][lk] for lk in links if lk in state["usage"]}

    monkeypatch.setattr(ca, "get_admin", get_admin)
    monkeypatch.setattr(ca, "channel_account", channel_account)
    monkeypatch.setattr(am, "export_tracked_invite", export)
    monkeypatch.setattr(am, "read_invite_usage", usage)
    return state


def test_selling_post_gets_own_link_and_joins_are_counted(tg):
    pool = _Pool()
    text, lid = _run(vl.prepare(pool, 7, 100, "Заказ у нас: @main_shop", "Продажа"))
    assert text == "Заказ у нас: https://t.me/+L1" and lid == 1
    assert tg["exported"] == [(555, "Витрина 1")]
    _run(vl.attach_op(pool, lid, 42))

    tg["usage"]["https://t.me/+L1"] = {"joins": 13, "requests": 2}
    assert _run(vl.refresh(pool, 7, 100)) == 1
    s = _run(vl.summary(pool, 7, 100))
    assert s == {"links": 1, "joins": 13, "requests": 2, "by_pillar": {"Продажа": 13}}


def test_post_without_target_mention_is_untouched(tg):
    pool = _Pool()
    assert _run(vl.prepare(pool, 7, 100, "Просто полезный пост", "Польза")) == \
        ("Просто полезный пост", None)
    assert not tg["exported"] and not pool.links


def test_target_not_in_infragram_is_untouched(tg):
    pool = _Pool(managed=False)
    assert _run(vl.prepare(pool, 7, 100, "Пишите @main_shop", "Продажа")) == \
        ("Пишите @main_shop", None)
    assert not tg["exported"]


def test_failed_export_keeps_text_and_leaves_no_row(tg, monkeypatch):
    from services import account_manager as am

    async def fail(*a, **kw):
        return None
    monkeypatch.setattr(am, "export_tracked_invite", fail)
    pool = _Pool()
    assert _run(vl.prepare(pool, 7, 100, "Пишите @main_shop", "Продажа")) == \
        ("Пишите @main_shop", None)
    assert not pool.links


def test_unpublished_link_is_dropped_but_published_is_kept(tg):
    pool = _Pool()
    _t, a = _run(vl.prepare(pool, 7, 100, "@main_shop", "Продажа"))
    _t, b = _run(vl.prepare(pool, 7, 100, "@main_shop", "Продажа"))
    _run(vl.attach_op(pool, b, 5))
    _run(vl.drop(pool, a))
    _run(vl.drop(pool, b))
    assert list(pool.links) == [b]


def _src(rel):
    return open(os.path.join(ROOT, rel), encoding="utf-8").read()


def test_publish_wires_link_before_queue_and_never_blocks():
    src = _src("services/channel_admin.py")
    body = src[src.index("async def publish("):src.index("async def _drop_link(")]
    assert body.index("va_links.prepare") < body.index("operation_bus.submit")
    assert body.index("va_links.prepare") < body.index("caption_fits")  # подпись меряется с ссылкой
    assert "va_links.attach_op" in body and "_drop_link(pool, link_id)" in body
    # сбой ссылки не роняет публикацию
    seg = body[body.index("link_id = None"):body.index("if not acc_ids:")]
    assert "except Exception" in seg


def test_runner_refreshes_and_report_shows_joins():
    assert "va_links.refresh" in _src("services/channel_admin_runner.py")
    from services import channel_admin as ca
    msg = ca.format_daily_report([("Витрина", {
        "posts_7d": 5, "links_7d": 3, "joins_7d": 21, "requests_7d": 4,
        "best_pillar": "Продажа"})])
    assert "привёл в основной канал: 21 (ещё заявок на вступление: 4)" in msg
    assert "r.joins_7d" in _src("mini_app/screens/va_admin.js")
