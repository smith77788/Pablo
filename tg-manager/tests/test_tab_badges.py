"""Живые значки на вкладках: числа, скоуп, отказ по одному счётчику.

Нижние вкладки были статичны: узнать, не ограничили ли аккаунт и не идёт ли
рассылка, можно было только открыв экраны по очереди. Теперь на вкладке висит
число, и берут его все вкладки ОДНОЙ ручкой `/api/miniapp/badges`.

Здесь две проверки, которых не сделать на живой базе и наоборот: что упавший
счётчик даёт ноль, а не ломает ответ (стенд с падающим пулом), и что экран
красит значок и прячет его на нуле (стенд node). Числа и скоуп проверяет
`tests/test_tab_badges_postgres.py`.
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import re
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
JS = (ROOT / "mini_app" / "screens" / "tab_badges.js").read_text(encoding="utf-8")
HTML = (ROOT / "mini_app" / "index.html").read_text(encoding="utf-8")


def test_a_broken_counter_is_a_zero_and_not_a_broken_answer():
    """Значок — украшение: из-за одного счётчика не пропадают остальные."""
    from services import mini_app_badges

    class _Pool:
        async def fetchval(self, sql, *args):
            if "broadcasts" in sql:
                raise RuntimeError("relation \"broadcasts\" does not exist")
            return 7

        async def fetchrow(self, sql, *args):
            raise RuntimeError("operation_queue недоступна")

    out = asyncio.new_event_loop().run_until_complete(
        mini_app_badges.collect(_Pool(), 991001))
    assert out == {"notifications": 7, "accounts": 7, "broadcasts": 0,
                   "operations": 7, "op": None}, (
        "упавшая текущая операция — пустая плашка, а не упавший ответ")


def test_every_counter_takes_the_owner_in_the_query():
    """Скоуп держит запрос, а не обработчик: иначе значок показывал бы чужую беду."""
    from services import mini_app_badges

    for key, sql in mini_app_badges._COUNTS.items():
        assert "$1" in sql, key
        assert re.search(r"(owner_id|created_by)\s*=\s*\$1", sql), key


@pytest.mark.skipif(not shutil.which("node"), reason="нужен node")
def test_the_screen_paints_a_badge_and_hides_a_zero():
    stand = """
const tabs = {};
function tab(id, lbl) {
  const kids = [];
  const t = {id, kids, attrs: {},
    appendChild(c) { kids.push(c); },
    querySelector(sel) {
      if (sel === '.tab-lbl') return {textContent: lbl};
      return kids.find(k => k.className === sel.slice(1)) || null;
    },
    setAttribute(k, v) { this.attrs[k] = v; }};
  return tabs[id] = t;
}
const document = { getElementById: id => tabs[id] || null,
  createElement: () => ({className: '', textContent: '', style: {}}) };
"""
    names = ("TAB_BADGE_OF", "setTabBadge")
    src = stand + "const TAB_BADGE_OF = " + re.search(
        r"const TAB_BADGE_OF = (\{[\s\S]*?\});", JS).group(1) + ";\n" + \
        re.search(r"\nfunction setTabBadge\([\s\S]*?\n}\n", JS).group(0) + r"""
tab('t-accounts', 'Аккаунты');
setTabBadge('accounts', 3);
const on = tabs['t-accounts'].kids[0];
const shown = {text: on.textContent, display: on.style.display,
               aria: tabs['t-accounts'].attrs['aria-label']};
setTabBadge('accounts', 0);
const hidden = on.style.display;
setTabBadge('accounts', 1240);
console.log(JSON.stringify({shown, hidden, capped: on.textContent}));
"""
    assert all(n in JS for n in names)
    out = json.loads(subprocess.check_output(["node", "-e", src], text=True))
    assert out["shown"]["text"] == "3" and out["shown"]["display"] == ""
    assert "3" in out["shown"]["aria"] and "Аккаунты" in out["shown"]["aria"]
    assert out["hidden"] == "none", "нуль обязан прятаться: «0» читается как сообщение"
    assert out["capped"] == "99+"


def test_the_badges_are_wired_once_and_for_all_tabs():
    assert '<script src="screens/tab_badges.js"></script>' in HTML
    assert ".tab-badge{" in HTML, "у значка нет стиля"
    # Одна ручка на весь таббар: отдельная ручка значка колокольчика удалена.
    nf = (ROOT / "mini_app" / "screens" / "notifications_feed.js").read_text(encoding="utf-8")
    assert "notifications/unread" not in nf + HTML, (
        "значок снова тянет своё число отдельным запросом")
    assert "refreshTabBadges" in HTML, "значки никто не обновляет с экранов"
    for key in ("notifications", "accounts", "broadcasts", "operations"):
        assert f"{key}:" in JS, key


@pytest.mark.skipif(not shutil.which("node"), reason="нужен node")
def test_the_live_bar_shows_what_is_running_and_goes_away():
    """Плашка «идёт операция»: прогресс поверх всех экранов, а не только в очереди.

    Массовая операция — это минуты и часы работы флота, и всё это время о ней
    не было видно ничего, кроме экрана очереди: опрос в приложении сдаётся
    через минуту словами «см. Очередь операций», то есть отправляет человека
    искать самому.
    """
    bar_js = (ROOT / "mini_app" / "screens" / "op_live_bar.js").read_text(encoding="utf-8")
    stand = """
let body = [];
const document = {
  getElementById: id => body.find(x => x.id === id) || null,
  createElement: () => ({id: '', className: '', innerHTML: '', onclick: null,
                         parentNode: null, setAttribute() {}}),
  body: {appendChild(n) { n.parentNode = {removeChild(c) {
    body = body.filter(x => x !== c); }}; body.push(n); }},
};
const setTimeout = () => 0;
const clearTimeout = () => {};
function esc(s) { return String(s == null ? '' : s); }
function num(v) { return String(v); }
function _opEta() { return '≈ ещё 7 мин'; }
let opened = null;
function openOpDetail(id) { opened = id; }
let _opBarId = null, _opBarTimer = null;
const OP_BAR_FAST = 20000;
"""
    src = stand + re.search(r"\nfunction renderOpLiveBar\([\s\S]*?\n}\n", bar_js).group(0) + r"""
renderOpLiveBar({id: 77, label: 'Инвайт в чат', done: 43, total: 100, started_at: '2026-11-24T09:00:00+00:00'});
const shown = body[0].innerHTML;
body[0].onclick();
const afterTap = opened;
renderOpLiveBar(null);
console.log(JSON.stringify({shown, afterTap, left: body.length}));
"""
    out = json.loads(subprocess.check_output(["node", "-e", src], text=True))
    assert "Инвайт в чат" in out["shown"] and "43" in out["shown"]
    assert "43%" in out["shown"], "процент обязан быть виден без счёта в уме"
    assert "≈ ещё 7 мин" in out["shown"], "остаток времени считает тот же код, что и экран"
    assert out["afterTap"] == 77, "нажатие обязано вести в саму операцию"
    assert out["left"] == 0, "операция кончилась — плашка обязана уйти"


def test_the_live_bar_is_fed_by_the_badges_answer():
    tb = (ROOT / "mini_app" / "screens" / "tab_badges.js").read_text(encoding="utf-8")
    assert "renderOpLiveBar(d.op)" in tb, "плашка просит числа отдельным запросом"
    assert '<script src="screens/op_live_bar.js"></script>' in HTML
    assert ".op-live{" in HTML
