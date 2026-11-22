"""«Найдено у вас»: «Ещё N» открывает раздел уже с этим поиском.

Раньше кнопка открывала раздел без запроса: из 40 найденных контактов
владелец видел все 3000 и набирал то же слово второй раз. Где у раздела есть
своё поле поиска, запрос переносится туда через обработчик раздела.
"""
import json
import pathlib
import re
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
JS = (ROOT / "mini_app" / "screens" / "find_mine.js").read_text(encoding="utf-8")
HTML = (ROOT / "mini_app" / "index.html").read_text(encoding="utf-8")

pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="нужен node")


def test_search_fields_exist_where_the_query_is_carried():
    for fid in ("contactSearch", "botSearch", "accSearch", "chSearch"):
        assert f'id="{fid}"' in HTML, fid
    assert "new Function" not in JS and "eval(" not in JS


def test_more_button_carries_query_into_the_section():
    src = ("function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/'/g,'&#39;');}\n"
           "function num(n){return String(n);}\n" + JS + r"""
const els = {moreFindMine: {style: {}, innerHTML: ''}, contactSearch: {value: ''}, botSearch: {value: ''}};
const document = {getElementById: id => els[id]};
let calls = [];
function openContacts() { calls.push(['contacts', els.contactSearch.value]); }
function goTab(t) { calls.push(['tab', t]); }
function onBotSearch(v) { calls.push(['botSearch', v]); }
function openDeals() { calls.push(['deals']); }
function toast(m) { calls.push(['toast', m]); }
function errRu() { return ''; }
async function api() { return {total: 50, groups: [
  {key: 'contacts', label: 'Контакты', total: 40, has_more: true, items: [{id: 'u1', title: 'Иван'}]},
  {key: 'bots', label: 'Боты', total: 3, has_more: true, items: [{id: 1, title: '@shop'}]},
  {key: 'deals', label: 'Сделки', total: 9, has_more: true, items: [{id: 2, title: 'Сделка'}]},
]}; }
(async () => {
  await _fmRun('Иван');
  const html = els.moreFindMine.innerHTML;
  fmOpenMore('contacts');
  fmOpenMore('bots');
  console.log(JSON.stringify({html, calls, bot: els.botSearch.value}));
})();
""")
    out = json.loads(subprocess.check_output(["node", "-e", src], text=True))
    h = out["html"]
    assert "Ещё 39 — открыть с поиском «Иван»" in h
    assert "onclick=\"fmOpenMore('contacts')\"" in h or "onclick=\"fmOpenMore(&#39;contacts&#39;)\"" in h
    # У сделок поля поиска нет — кнопка ведёт в раздел, как раньше.
    assert 'onclick="openDeals()"' in h and "Ещё 8 — открыть раздел" in h
    assert out["calls"] == [["contacts", "Иван"], ["tab", "bots"], ["botSearch", "Иван"]]
    assert out["bot"] == "Иван"
