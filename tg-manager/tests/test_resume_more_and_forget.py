"""Три последних шага итерации: «ещё», «продолжить» и «забыть».

* Поиск отдавал по шесть находок в группе и молчал о том, что их больше, —
  «показать ещё» теперь просит ту же выдачу ШИРЕ (вторая страница у шести
  групп означала бы шесть счётчиков смещения на экране).
* Мини-апп закрывают десятки раз в день — на звонок, на другой чат, — и он
  всегда открывается на «Итогах»: человек, разбиравший контакты, возвращался
  к началу пути. Теперь главный предлагает вернуться туда, где он был.
* Приложение помнит на устройстве закреплённое, недавнее, историю поиска,
  кэш списков и тему, и убрать это было нельзя ничем, кроме переустановки
  Telegram.
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
KIT = (ROOT / "mini_app" / "screens" / "ux_kit.js").read_text(encoding="utf-8")
FA = (ROOT / "mini_app" / "screens" / "find_all.js").read_text(encoding="utf-8")
HTML = (ROOT / "mini_app" / "index.html").read_text(encoding="utf-8")

pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="нужен node")


def _fn(src: str, name: str) -> str:
    m = re.search(r"\nfunction " + name + r"\([\s\S]*?\n}\n", src)
    assert m, name
    return m.group(0)


_LS = """
let _LS = {};
const localStorage = {
  getItem: k => _LS[k] == null ? null : _LS[k],
  setItem: (k, v) => { _LS[k] = String(v); },
  removeItem: k => { delete _LS[k]; },
  key: i => Object.keys(_LS)[i],
  get length() { return Object.keys(_LS).length; },
};
const UX_LAST_KEY = 'infragram_last_screen';
const UX_LAST_TTL = 24 * 3600e3;
const UX_LOCAL_KEYS = ['infragram_nav_memory', 'infragram_find_history',
                       'infragram_last_screen', 'infragram_theme'];
"""


def _run(src: str):
    return json.loads(subprocess.check_output(["node", "-e", src], text=True))


def test_the_resume_offer_expires_in_a_day():
    out = _run(_LS + _fn(KIT, "uxRememberScreen") + _fn(KIT, "uxLastScreen") + r"""
uxRememberScreen('Контакты', 'openContacts()');
const fresh = uxLastScreen();
const old = JSON.parse(_LS[UX_LAST_KEY]);
old.ts = Date.now() - 30 * 3600e3;            // вчерашний день
_LS[UX_LAST_KEY] = JSON.stringify(old);
const stale = uxLastScreen();
_LS[UX_LAST_KEY] = 'не json';
console.log(JSON.stringify({fresh, stale, junk: uxLastScreen()}));
""")
    assert out["fresh"]["title"] == "Контакты" and out["fresh"]["h"] == "openContacts()"
    assert out["stale"] is None, "через день это уже не «то место»"
    assert out["junk"] is None


def test_the_resume_row_is_silent_when_there_is_nothing_to_resume():
    stand = _LS + """
const box = {id: 'homeResume', innerHTML: ''};
const document = { getElementById: id => id === 'homeResume' ? box : null };
"""
    out = _run(stand + _fn(KIT, "uxLastScreen") + _fn(KIT, "uxNavEsc")
               + _fn(KIT, "uxResumeHome") + r"""
uxResumeHome();
const empty = box.innerHTML;
_LS[UX_LAST_KEY] = JSON.stringify({title: 'Жалобы <b>', h: "openComplaints()", ts: Date.now()});
uxResumeHome();
console.log(JSON.stringify({empty, full: box.innerHTML}));
""")
    assert out["empty"] == ""
    assert "openComplaints()" in out["full"] and "Продолжить" in out["full"]
    assert "&lt;b&gt;" in out["full"], "подпись из памяти — текст, а не разметка"


def test_forgetting_clears_exactly_what_it_promises():
    out = _run(_LS + _fn(KIT, "uxForgetLocal") + r"""
_LS['infragram_nav_memory'] = '{}';
_LS['infragram_find_history'] = '[]';
_LS['infragram_theme'] = 'dark';
_LS['infragram_cache_home'] = '{}';
_LS['infragram_cache_accounts'] = '{}';
_LS['tg_session_something'] = 'чужое';       // не наше — не трогаем
const n = uxForgetLocal();
console.log(JSON.stringify({n, left: Object.keys(_LS)}));
""")
    assert out["n"] == 5
    assert out["left"] == ["tg_session_something"], (
        "чистка обязана забирать только своё")


def test_the_wider_search_is_asked_for_only_when_it_helps():
    from services import mini_app_global_search as G

    class _Pool:
        def __init__(self):
            self.limits = []

        async def fetch(self, sql, uid, pattern, limit):
            self.limits.append(limit)
            return []

    pool = _Pool()
    asyncio.new_event_loop().run_until_complete(G.search_all(pool, 1, "вега", 25))
    assert set(pool.limits) == {25}, "ширину просят у каждой группы"
    assert "faMore()" in FA and "FA_PER = 25" in FA
    assert "d.capped" in FA, "«показать ещё» предлагается всегда, даже когда нечего"
    assert "if (next !== FA_Q) FA_PER = 0;" in FA, (
        "новый запрос обязан снова давать узкую выдачу")


def test_everything_has_a_way_in():
    assert 'id="homeResume"' in HTML and "uxResumeHome();" in HTML
    assert "uxRememberScreen(" in HTML, "переход по каталогу не запоминается как «то место»"
    assert "forgetLocalData()" in HTML and "Память приложения" in HTML
    # Необратимый шаг спрашивает ДО и называет, что исчезнет.
    row = HTML[HTML.index("async function forgetLocalData()"):][:700]
    assert "askConfirm(" in row and "кэш списков" in row
