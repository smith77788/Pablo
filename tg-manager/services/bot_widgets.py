"""HTML-виджеты в чатах (Telegram HTMLBubbles, Bot API 10.4): проверка и отправка.

ЗАЧЕМ. С Telegram 13.2 сообщение бота может быть маленькой HTML-страницей
прямо в пузыре чата: отсчёт до события, карточка с кнопкой, викторина, опрос с
живыми голосами. Отправить такое может только бот — в личку человеку, который
его запустил. С выхода Bot API 10.4 (9 ноября 2026) Telegram в рабочей среде
пускает виджеты только в личные чаты и с защитой от пересылки и копирования
(copyMessage виджет не копирует); группы и инлайн-режим — позже. У владельца в Infragram есть свои боты —
значит, он может слать виджеты от их имени.

Как устроен виджет (TelegramMessenger/widget-sandbox: docs/tgw-format.md,
docs/sdk-v1.md, docs/profile-v1.md):
  * код (HTML ≤ 32768 байт, после zlib уровня 9 ≤ 4096 байт) после отправки
    не меняется НИКОГДА;
  * меняются только данные: public_data (видят все, и пересланные копии) и
    chat_data (только этот чат), до 1024 байт каждое; меняет их только бот;
  * внутри — без сети, без внешних картинок и скриптов, без полей ввода и
    клавиатуры; данные виджет читает из window.TelegramWidget, а спросить бота
    может TelegramWidget.request(...) — бот отвечает answerWidgetQuery.

Поэтому шаблоны здесь — неизменный код, а всё, что вводит владелец (тексты,
дата, варианты ответа), уходит в public_data. Виджет шлётся ОБЩИМ (is_shared):
у него есть widget_id, и тогда
  * пересланные копии голосуют наравне с оригиналом (shared_widget_query);
  * правка данных (editSharedWidgetData) доходит до всех копий сразу;
  * тот же виджет можно разослать подписчикам (sendSharedWidget) — и у всех
    будет один общий счёт голосов, а не тысяча отдельных опросов.

ОСНОВНОЙ BOT API — ГЛАВНЫЙ ПУТЬ. До 9 ноября 2026 методы виджетов были только
на бета-шлюзе (https://api.telegram.org/beta). С Bot API 10.4 они в основном
API, и все вызовы идут туда. Бета осталась запасным путём: если основной адрес
отвечает «метод не найден» (404 — сервер ещё на старой версии), вызов
повторяется через бету, и на MAIN_RECHECK_S секунд следующие вызовы идут сразу
туда, а не стучатся дважды. Как только основной адрес снова ответит по-
настоящему, запасной путь забывается. Шлюз переназначает переменная
TG_WIDGETS_API_BASE; значение off выключает запасной путь совсем.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import zlib

from services import bot_api

log = logging.getLogger(__name__)

HTML_MAX = 32768
PACKED_MAX = 4096
PACKED_WARN = 3800
DATA_MAX = 1024
CAPTION_MAX = 1024
SIZE_MAX = 600  # ширина/высота пузыря в точках: 0 — по содержимому
SHARED_CACHE_S = 5  # как часто копии общего виджета перечитывают данные

BETA_BASE = "https://api.telegram.org/beta"
MAIN_RECHECK_S = 600  # сколько не стучаться в основной адрес после его 404
_OFF = {"off", "0", "no", "none", "false", "-"}

# Монотонное время, до которого основной Bot API считается «ещё без виджетов».
_main_missing_until = 0.0


def beta_base() -> str | None:
    """Запасной шлюз для методов виджетов; None — запасного пути нет."""
    v = (os.environ.get("TG_WIDGETS_API_BASE") or "").strip()
    if v.lower() in _OFF:
        return None
    return v or BETA_BASE


def route() -> str:
    """Куда сейчас идут вызовы виджетов: "main" или "beta" (для диагностики)."""
    return "beta" if beta_base() and time.monotonic() < _main_missing_until else "main"


def packed_size(html_bytes: bytes) -> int:
    """Сколько насчитает Telegram: zlib уровня 9 (PHP gzcompress($html, 9))."""
    return len(zlib.compress(html_bytes, 9))


# Что Telegram в виджете вырежет или не выполнит (docs/profile-v1.md). Это
# предупреждения, а не отказ: виджет отправится, но эта часть работать не будет.
_WARN_RULES: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"<script[^>]*\bsrc\s*=", re.I),
     "Внешние скрипты (<script src>) в виджете не выполняются — код должен быть внутри."),
    (re.compile(r"<link\b|@import\b", re.I),
     "Внешние стили (<link>, @import) не загружаются — стили пишите в <style>."),
    (re.compile(r"\bsrc\s*=\s*[\"']?\s*(?:https?:)?//", re.I),
     "Картинки и файлы по ссылке не загружаются — только встроенные data:."),
    (re.compile(r"\b(?:fetch|XMLHttpRequest|WebSocket|EventSource|sendBeacon)\b"),
     "Сети в виджете нет: fetch, XMLHttpRequest и WebSocket не работают. "
     "Связь с ботом — через TelegramWidget.request()."),
    (re.compile(r"<(?:input|select|textarea|option|datalist|fieldset)\b", re.I),
     "Поля ввода (input, select, textarea) Telegram удаляет — используйте кнопки."),
    (re.compile(r"<(?:iframe|video|audio|embed|object|frame)\b", re.I),
     "Видео, аудио и вложенные страницы (iframe) в виджете не показываются."),
    (re.compile(r"\bkey(?:down|up|press)\b", re.I),
     "Клавиатура в виджете не работает — только касания."),
)


def check(html: str) -> dict:
    """Проверить код виджета: размеры как у Telegram и то, что не сработает.

    {ok, bytes, packed, errors: [..], warnings: [..]} — тексты по-русски.
    ok=False — Telegram такой виджет не примет (errors объясняют почему).
    """
    errors: list[str] = []
    warnings: list[str] = []
    text = html if isinstance(html, str) else ""
    try:
        raw = text.encode("utf-8")
    except UnicodeEncodeError:
        raw = text.encode("utf-8", "replace")
        errors.append("В коде есть испорченные символы — Telegram примет только чистый текст UTF-8.")
    packed = packed_size(raw) if raw else 0
    if not text.strip():
        errors.append("Код виджета пустой.")
    if "\x00" in text:
        errors.append("В коде есть нулевой символ — Telegram такой код не примет.")
    if len(raw) > HTML_MAX:
        errors.append(f"Код больше {HTML_MAX // 1024} КБ ({len(raw)} байт) — сократите его.")
    if packed > PACKED_MAX:
        errors.append(f"После сжатия код занимает {packed} байт, а Telegram берёт не больше "
                      f"{PACKED_MAX} — сократите стили и скрипты.")
    elif packed > PACKED_WARN:
        warnings.append(f"После сжатия {packed} из {PACKED_MAX} байт — почти предел, "
                        "лишняя правка может не поместиться.")
    for rx, msg in _WARN_RULES:
        if rx.search(text):
            warnings.append(msg)
    return {"ok": not errors, "bytes": len(raw), "packed": packed,
            "errors": errors, "warnings": warnings}


def data_bytes(value: str | None) -> int:
    return len((value or "").encode("utf-8", "replace"))


def data_error(value: str | None, what: str = "Данные виджета") -> str | None:
    """None — годится; иначе причина по-русски. Предел — 1024 байта UTF-8."""
    if not value:
        return None
    n = data_bytes(value)
    if n > DATA_MAX:
        return f"{what} занимают {n} байт, а Telegram берёт не больше {DATA_MAX} — сократите тексты."
    return None


# ── Шаблоны ─────────────────────────────────────────────────────────────────
# Код шаблона неизменен; данные — public_data (JSON). Цвета — из темы чата
# (--tg-theme-*), поэтому виджет одинаково выглядит в светлой и тёмной теме.
# Общая часть: разбор данных, тактильный отклик, ответ бота. Шаблоны без
# запросов к боту её тоже несут — она маленькая и сжимается почти в ноль.

_BASE_CSS = """body{margin:0;padding:12px 10px}
[hidden]{display:none!important}
h1{font-size:16px;font-weight:600;line-height:1.3;margin:0 4px 6px}
.h{font-size:13px;line-height:1.4;color:var(--tg-theme-hint-color)}
.e{color:var(--tg-theme-destructive-text-color)}
button{font:inherit;color:inherit;border:0}
"""

_BASE_JS = """const T=window.TelegramWidget,$=i=>document.getElementById(i),OK=!!T&&!T.isPreview;
let P={};
const J=s=>{try{return JSON.parse(s||'{}')||{}}catch(e){return{}}},
hap=k=>{try{const h=T.HapticFeedback;k?h.notificationOccurred(k):h.impactOccurred('light')}catch(e){}};
"""

# Голосование (опрос, оценка, «кто идёт»): один протокол. Касание — запрос
# «vote:N», бот переключает голос и отвечает {my, v, r} или {err}. r — номер
# правки: опрос копий может принести старые данные после ответа, и без r
# счёт прыгал бы туда-обратно.
_VOTE_JS = """const E={NOT_ALLOWED:'Голосовать здесь нельзя',BUSY:'Подождите, голос отправляется',RATE_LIMITED:'Слишком часто — попробуйте чуть позже',TIMEOUT:'Бот не ответил — попробуйте ещё раз',FAILED:'Не удалось отправить голос',INVALID_ANSWER:'Бот ответил непонятно'};
let my=-1,q=-1,msg='',bad=0;
try{my=+(localStorage.getItem('my')??-1)}catch(e){}
function load(){const p=J(T&&T.publicData);if(!P.o&&!P.v||(p.r|0)>=(P.r|0))P=p;draw()}
function cnt(n){return(P.v||[]).slice(0,n).map(x=>Math.max(0,+x||0)).concat(Array(n).fill(0)).slice(0,n)}
function vote(i){
if(!OK||q>=0)return;
hap();q=i;msg='';bad=0;draw();
T.request('vote:'+i).then(a=>{const r=J(a&&a.result);
if(r.err){msg=r.err;bad=1;hap('error');return}
if(Array.isArray(r.v)&&(r.r|0)>=(P.r|0)){P.v=r.v;P.r=r.r;if(r.n)P.n=r.n}
if(r.my!=null){my=+r.my;try{localStorage.setItem('my',my)}catch(e){}}
hap('success');
},e=>{msg=E[e&&e.code]||E.FAILED;bad=1;hap('error')}).finally(()=>{q=-1;draw()});
}
function note(el,idle){el.textContent=msg||(OK?idle:'');el.className='h'+(bad?' e':'')}
function plural(n,a,b,c){const x=n%10,y=n%100;return x==1&&y!=11?a:x>=2&&x<=4&&(y<12||y>14)?b:c}
function live(){if(T){T.onEvent('publicDataChanged',load);if(OK&&T.watchPublicData)T.watchPublicData()}}
"""


def _page(css: str, body: str, js: str, *, vote: bool = False) -> str:
    return ("<style>\n" + _BASE_CSS + css + "</style>\n" + body + "\n<script>\n"
            + _BASE_JS + (_VOTE_JS if vote else "") + js + "</script>\n")


_HTML_COUNTDOWN = _page(
    """body{text-align:center;padding:14px 12px}
#c{display:flex;justify-content:center;gap:8px}
#c div{min-width:56px;padding:8px 4px;border-radius:12px;background:var(--tg-theme-secondary-bg-color)}
#c b{display:block;font-size:24px;font-weight:600;font-variant-numeric:tabular-nums}
#c span{font-size:12px;color:var(--tg-theme-hint-color)}
#d{font-size:20px;font-weight:600;color:var(--tg-theme-accent-text-color)}
#w{margin-top:8px}
""",
    '<h1 id="t"></h1><div id="c"></div><div id="d" hidden></div><div id="w" class="h"></div>',
    """const L=[['дн.',86400],['ч',3600],['мин',60],['сек',1]];
function draw(){
const at=+P.at||0,s=Math.floor(at-Date.now()/1000),done=s<=0;
$('t').textContent=P.t||'';
$('w').textContent=at?new Date(at*1000).toLocaleString('ru-RU',{day:'numeric',month:'long',hour:'2-digit',minute:'2-digit'}):'';
$('c').hidden=done;$('d').hidden=!done;
if(done){$('d').textContent=P.d||'Время пришло!';return}
let r=s;$('c').innerHTML=L.filter(x=>x[1]<86400||s>=86400).map(([n,k])=>{const v=Math.floor(r/k);r%=k;return'<div><b>'+v+'</b><span>'+n+'</span></div>'}).join('');
}
function load(){P=J(T&&T.publicData);draw()}
load();setInterval(draw,1000);
if(T)T.onEvent('publicDataChanged',load);
""")

_HTML_CARD = _page(
    """body{padding:14px 12px}
#e{font-size:34px;line-height:1.1}
h1{font-size:17px;margin:6px 0 4px}
p{margin:0 0 12px;font-size:14px;line-height:1.4;color:var(--tg-theme-hint-color);white-space:pre-line}
#b{display:block;width:100%;padding:11px;border-radius:10px;background:var(--tg-theme-button-color);color:var(--tg-theme-button-text-color);font-weight:600}
""",
    '<div id="e"></div><h1 id="t"></h1><p id="d"></p><button id="b" hidden></button>',
    """function go(){
if(!OK||!P.u)return;hap();
/^https:\\/\\/t\\.me\\//.test(P.u)?T.openTelegramLink(P.u):T.openLink(P.u);
}
function load(){
P=J(T&&T.publicData);
$('e').textContent=P.e||'';$('e').hidden=!P.e;
$('t').textContent=P.t||'';$('d').textContent=P.d||'';$('d').hidden=!P.d;
$('b').textContent=P.b||'Открыть';$('b').hidden=!P.u;
}
$('b').onclick=go;load();
if(T)T.onEvent('publicDataChanged',load);
""")

_HTML_QUIZ = _page(
    """#o button{display:block;width:100%;margin:0 0 6px;padding:10px 12px;border-radius:10px;background:var(--tg-theme-secondary-bg-color);text-align:left;transition:background .25s}
#o .ok{background:#31b545;color:#fff}
#o .no{background:var(--tg-theme-destructive-text-color);color:#fff}
#x{margin:8px 4px 0}
""",
    '<h1 id="q"></h1><div id="o"></div><div id="x" class="h"></div>',
    """let pick=-1;
try{pick=+(localStorage.getItem('pick')??-1)}catch(e){}
function draw(){
const o=Array.isArray(P.o)?P.o.slice(0,6):[],a=+P.a;
$('q').textContent=P.q||'';$('o').textContent='';
o.forEach((t,i)=>{const b=document.createElement('button');b.textContent=t;
if(pick>=0)b.className=i==a?'ok':i==pick?'no':'';
b.onclick=()=>choose(i);$('o').appendChild(b)});
$('x').textContent=pick<0?'Выберите ответ':(pick==a?'Верно! ':'Неверно. ')+(P.x||'');
}
function choose(i){
if(pick>=0||!OK)return;
pick=i;try{localStorage.setItem('pick',i)}catch(e){}
hap(i==+P.a?'success':'error');draw();
}
function load(){P=J(T&&T.publicData);draw()}
load();
if(T)T.onEvent('publicDataChanged',load);
""")

_HTML_POLL = _page(
    """#s{margin:0 4px 8px}
#o button{position:relative;display:block;width:100%;margin:0 0 6px;padding:9px 12px;border-radius:10px;background:var(--tg-theme-secondary-bg-color);text-align:left;overflow:hidden;transition:opacity .3s}
#o i{position:absolute;left:0;top:0;bottom:0;width:0;background:var(--tg-theme-button-color);opacity:.18;transition:width .5s}
#o .m i{opacity:.35}
#o .bz{opacity:.5}
#o span{position:relative;display:flex;justify-content:space-between;gap:8px}
#o b{font-weight:600;font-variant-numeric:tabular-nums}
#n{margin:4px 4px 0}
""",
    '<h1 id="q"></h1><div id="s" class="h"></div><div id="o"></div><div id="n" class="h"></div>',
    """function draw(){
const o=Array.isArray(P.o)?P.o:[],v=cnt(o.length),t=v.reduce((a,b)=>a+b,0);
$('q').textContent=P.q||'Опрос';
$('s').textContent=t?t+' '+plural(t,'голос','голоса','голосов'):'Пока никто не голосовал';
$('o').textContent='';
o.forEach((n,i)=>{const b=document.createElement('button'),p=t?Math.round(v[i]*100/t):0,w=document.createElement('i'),r=document.createElement('span'),l=document.createElement('span'),c=document.createElement('b');
l.textContent=(i==my?'✓ ':'')+n;c.textContent=t?p+'%':'';w.style.width=p+'%';
r.append(l,c);b.append(w,r);b.className=(i==my?'m':'')+(q>=0&&i!=q?' bz':'');
b.onclick=()=>vote(i);$('o').appendChild(b)});
note($('n'),my>=0?'Нажмите свой вариант ещё раз, чтобы отменить голос':'Нажмите вариант, чтобы проголосовать');
}
load();live();
""", vote=True)

_HTML_RATING = _page(
    """body{text-align:center}
#a{font-size:30px;font-weight:700;font-variant-numeric:tabular-nums}
#st{display:flex;justify-content:center;gap:4px;margin:8px 0}
#st button{width:44px;height:44px;border-radius:12px;background:var(--tg-theme-secondary-bg-color);font-size:24px;line-height:44px;padding:0;filter:grayscale(1);opacity:.5;transition:opacity .2s}
#st .on{filter:none;opacity:1}
#st .bz{opacity:.3}
""",
    '<h1 id="t"></h1><div id="a"></div><div id="c" class="h"></div><div id="st"></div><div id="n" class="h"></div>',
    """function draw(){
const v=cnt(5),t=v.reduce((a,b)=>a+b,0),s=v.reduce((a,b,i)=>a+b*(i+1),0);
$('t').textContent=P.t||'Оцените';
$('a').textContent=t?(s/t).toFixed(1).replace('.',','):'—';
$('c').textContent=t?t+' '+plural(t,'оценка','оценки','оценок'):'Оценок пока нет';
$('st').textContent='';
for(let i=0;i<5;i++){const b=document.createElement('button');b.textContent='⭐';
b.className=(my>=0&&i<=my?'on':'')+(q>=0&&i!=q?' bz':'');b.onclick=()=>vote(i);$('st').appendChild(b)}
note($('n'),my>=0?'Ваша оценка: '+(my+1)+'. Нажмите её ещё раз, чтобы отменить':'Нажмите на звезду');
}
load();live();
""", vote=True)

_HTML_RSVP = _page(
    """#m{margin:0 4px 10px}
#k{display:flex;gap:6px}
#k button{flex:1;padding:10px 4px;border-radius:10px;background:var(--tg-theme-secondary-bg-color);font-weight:600;transition:opacity .3s}
#k .m{background:var(--tg-theme-button-color);color:var(--tg-theme-button-text-color)}
#k .bz{opacity:.5}
#g{margin:10px 4px 0;font-size:14px;line-height:1.4}
#n{margin:6px 4px 0}
""",
    '<h1 id="t"></h1><div id="m" class="h"></div><div id="k"></div><div id="g"></div><div id="n" class="h"></div>',
    """const K=['Иду','Может быть','Не иду'];
function draw(){
const v=cnt(3),n=Array.isArray(P.n)?P.n:[];
$('t').textContent=P.t||'Кто идёт?';
$('m').textContent=[P.w,P.p].filter(Boolean).join(' · ');$('m').hidden=!P.w&&!P.p;
$('k').textContent='';
K.forEach((x,i)=>{const b=document.createElement('button');b.textContent=x+(v[i]?' · '+v[i]:'');
b.className=(i==my?'m':'')+(q>=0&&i!=q?' bz':'');b.onclick=()=>vote(i);$('k').appendChild(b)});
$('g').textContent=v[0]?'Идут: '+n.join(', ')+(v[0]>n.length?' и ещё '+(v[0]-n.length):''):'Пока никто не отметился';
note($('n'),my>=0?'Нажмите свой ответ ещё раз, чтобы снять его':'Отметьтесь — это видят все');
}
load();live();
""", vote=True)

_HTML_GOAL = _page(
    """body{padding:14px 12px}
#bar{height:10px;border-radius:5px;background:var(--tg-theme-secondary-bg-color);overflow:hidden;margin:10px 0 6px}
#f{height:100%;width:0;border-radius:5px;background:var(--tg-theme-button-color);transition:width .8s}
#v{display:flex;justify-content:space-between;font-variant-numeric:tabular-nums}
#v b{font-size:18px}
p{margin:8px 0 12px;font-size:14px;line-height:1.4;color:var(--tg-theme-hint-color);white-space:pre-line}
#b{display:block;width:100%;padding:11px;border-radius:10px;background:var(--tg-theme-button-color);color:var(--tg-theme-button-text-color);font-weight:600}
""",
    '<h1 id="t"></h1><div id="bar"><div id="f"></div></div><div id="v"><b id="h"></b><span id="g" class="h"></span></div><p id="d"></p><button id="b" hidden></button>',
    """const F=n=>(+n||0).toLocaleString('ru-RU');
function load(){
P=J(T&&T.publicData);
const g=+P.g||0,h=+P.h||0,c=P.c?' '+P.c:'';
$('t').textContent=P.t||'';
$('h').textContent=F(h)+c;
$('g').textContent=g?'из '+F(g)+c+' · '+Math.floor(h*100/g)+'%':'';
$('f').style.width=(g?Math.min(100,h*100/g):0)+'%';
$('d').textContent=P.d||'';$('d').hidden=!P.d;
$('b').textContent=P.b||'Поддержать';$('b').hidden=!P.u;
}
$('b').onclick=()=>{if(!OK||!P.u)return;hap();/^https:\\/\\/t\\.me\\//.test(P.u)?T.openTelegramLink(P.u):T.openLink(P.u)};
load();
if(T)T.onEvent('publicDataChanged',load);
""")

_HTML_PRIZE = _page(
    """body{text-align:center;padding:14px 12px}
#r{margin:10px 0;padding:18px 8px;border-radius:14px;background:var(--tg-theme-secondary-bg-color);font-size:20px;font-weight:700;min-height:26px}
#r.w{background:var(--tg-theme-button-color);color:var(--tg-theme-button-text-color)}
#go{display:block;width:100%;padding:11px;border-radius:10px;background:var(--tg-theme-button-color);color:var(--tg-theme-button-text-color);font-weight:600}
#x{margin-top:8px}
""",
    '<h1 id="t"></h1><div id="r"></div><button id="go">🎲 Испытать удачу</button><div id="x" class="h"></div>',
    """let won=null,spin=0;
try{won=localStorage.getItem('won')}catch(e){}
function draw(){
const p=Array.isArray(P.p)?P.p:[];
$('t').textContent=P.t||'Испытайте удачу';
if(won!==null){$('r').textContent=won;$('r').className='w';$('go').hidden=true;$('x').textContent=P.x||'Сохраните этот результат';return}
$('r').className='';$('go').hidden=!p.length;
if(!spin){$('r').textContent='❓';$('x').textContent=p.length?'Одна попытка на человека':''}
}
function go(){
const p=Array.isArray(P.p)?P.p:[];
if(!OK||spin||won!==null||!p.length)return;
hap();spin=1;let k=0;const pick=p[crypto.getRandomValues(new Uint32Array(1))[0]%p.length];
const t=setInterval(()=>{$('r').textContent=p[k++%p.length];if(k>18){clearInterval(t);won=pick;
try{localStorage.setItem('won',won)}catch(e){}spin=0;hap('success');draw()}},90);
}
function load(){P=J(T&&T.publicData);draw()}
$('go').onclick=go;load();
if(T)T.onEvent('publicDataChanged',load);
""")

# Поля шаблона для формы мини-аппа: key, label, kind (text | long | url |
# when | options | answer | number), max — предел символов, need — обязательно.
# vote — сколько вариантов у голосования (опрос: по числу вариантов).
TEMPLATES: dict[str, dict] = {
    "poll": {
        "name": "Опрос с живыми голосами",
        "desc": "Голоса считает ваш бот; проценты обновляются у всех — и в пересланных копиях.",
        "html": _HTML_POLL, "vote": "options",
        "fields": [
            {"key": "q", "label": "Вопрос", "kind": "text", "max": 140, "need": True},
            {"key": "o", "label": "Варианты (каждый с новой строки, 2–6)", "kind": "options",
             "need": True},
        ],
    },
    "rating": {
        "name": "Оценка звёздами",
        "desc": "От 1 до 5 звёзд: средняя оценка и число голосов видны всем.",
        "html": _HTML_RATING, "vote": 5,
        "fields": [
            {"key": "t", "label": "Что оцениваем", "kind": "text", "max": 100, "need": True,
             "hint": "Например: Как вам наш новый продукт?"},
        ],
    },
    "rsvp": {
        "name": "Кто идёт",
        "desc": "Событие с кнопками «Иду / Может быть / Не иду» и списком тех, кто идёт.",
        "html": _HTML_RSVP, "vote": 3,
        "fields": [
            {"key": "t", "label": "Событие", "kind": "text", "max": 80, "need": True},
            {"key": "w", "label": "Когда", "kind": "text", "max": 60,
             "hint": "Например: суббота, 19:00"},
            {"key": "p", "label": "Где", "kind": "text", "max": 80},
        ],
    },
    "countdown": {
        "name": "Обратный отсчёт",
        "desc": "Таймер до события: дни, часы, минуты и секунды тикают прямо в чате.",
        "html": _HTML_COUNTDOWN,
        "fields": [
            {"key": "t", "label": "Заголовок", "kind": "text", "max": 80, "need": True,
             "hint": "Например: До старта распродажи"},
            {"key": "at", "label": "Дата и время события", "kind": "when", "need": True},
            {"key": "d", "label": "Текст, когда время придёт", "kind": "text", "max": 80,
             "hint": "Например: Распродажа началась!"},
        ],
    },
    "card": {
        "name": "Карточка с кнопкой",
        "desc": "Заголовок, текст и кнопка со ссылкой — на сайт, канал или бота.",
        "html": _HTML_CARD,
        "fields": [
            {"key": "e", "label": "Эмодзи", "kind": "text", "max": 8, "hint": "Например: 🎁"},
            {"key": "t", "label": "Заголовок", "kind": "text", "max": 80, "need": True},
            {"key": "d", "label": "Текст", "kind": "long", "max": 400},
            {"key": "b", "label": "Надпись на кнопке", "kind": "text", "max": 32,
             "hint": "Например: Перейти"},
            {"key": "u", "label": "Ссылка кнопки (https://…)", "kind": "url"},
        ],
    },
    "goal": {
        "name": "Сбор на цель",
        "desc": "Полоса прогресса к цели: сколько собрано и сколько осталось. "
                "Сумму можно обновлять уже после отправки.",
        "html": _HTML_GOAL,
        "fields": [
            {"key": "t", "label": "На что собираем", "kind": "text", "max": 80, "need": True},
            {"key": "g", "label": "Цель (число)", "kind": "number", "need": True},
            {"key": "h", "label": "Уже собрано (число)", "kind": "number"},
            {"key": "c", "label": "Единица", "kind": "text", "max": 12,
             "hint": "Например: ₽, $, участников"},
            {"key": "d", "label": "Описание", "kind": "long", "max": 300},
            {"key": "b", "label": "Надпись на кнопке", "kind": "text", "max": 32},
            {"key": "u", "label": "Ссылка кнопки (https://…)", "kind": "url"},
        ],
    },
    "quiz": {
        "name": "Викторина",
        "desc": "Вопрос с вариантами: человек выбирает, виджет сразу говорит, верно ли.",
        "html": _HTML_QUIZ,
        "fields": [
            {"key": "q", "label": "Вопрос", "kind": "text", "max": 140, "need": True},
            {"key": "o", "label": "Варианты ответа (каждый с новой строки, 2–6)",
             "kind": "options", "need": True},
            {"key": "a", "label": "Номер верного варианта", "kind": "answer", "need": True},
            {"key": "x", "label": "Пояснение после ответа", "kind": "text", "max": 200},
        ],
    },
    "prize": {
        "name": "Испытай удачу",
        "desc": "Розыгрыш в одно касание: варианты мелькают и останавливаются на одном. "
                "Одна попытка на человека; результат видит только он сам.",
        "html": _HTML_PRIZE,
        "fields": [
            {"key": "t", "label": "Заголовок", "kind": "text", "max": 80, "need": True,
             "hint": "Например: Ваш подарок к заказу"},
            {"key": "p", "label": "Варианты (каждый с новой строки, 2–6)", "kind": "options",
             "need": True, "hint": "Например: Скидка 10%"},
            {"key": "x", "label": "Что делать с результатом", "kind": "text", "max": 120,
             "hint": "Например: Покажите этот экран менеджеру"},
        ],
    },
}

OPTIONS_MIN, OPTIONS_MAX, OPTION_LEN = 2, 6, 60
NUMBER_MAX = 10 ** 12
_WHEN_MAX_AHEAD_S = 5 * 366 * 86400
RSVP_NAMES_MAX = 12


class WidgetError(ValueError):
    """Неверные данные виджета; текст — для человека, по-русски."""


def is_vote_template(template: str) -> bool:
    return bool((TEMPLATES.get(template) or {}).get("vote"))


def vote_options(template: str, data: dict) -> int:
    """Сколько вариантов у голосования этого виджета (0 — не голосование)."""
    v = (TEMPLATES.get(template) or {}).get("vote")
    if v == "options":
        return len(data.get("o") or []) if isinstance(data.get("o"), list) else 0
    return int(v or 0)


def templates_public() -> list[dict]:
    """Шаблоны для мини-аппа: поля и размер после сжатия, без самого кода."""
    return [{"id": tid, "name": t["name"], "desc": t["desc"], "fields": t["fields"],
             "votes": bool(t.get("vote")), "packed": packed_size(t["html"].encode("utf-8"))}
            for tid, t in TEMPLATES.items()]


def template_html(template: str) -> str | None:
    t = TEMPLATES.get(template)
    return t["html"] if t else None


def _clean_text(v, field: dict) -> str:
    s = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", str(v if v is not None else "")).strip()
    mx = int(field.get("max") or 0)
    if mx and len(s) > mx:
        raise WidgetError(f"«{field['label']}»: не длиннее {mx} символов.")
    return s


def build_data(template: str, values: dict, *, now: float | None = None) -> dict:
    """Значения формы → public_data шаблона. WidgetError — что не так, по-русски."""
    t = TEMPLATES.get(template)
    if not t:
        raise WidgetError("Такого шаблона нет.")
    vals = values if isinstance(values, dict) else {}
    out: dict = {}
    n_opts = 0
    for f in t["fields"]:
        k, kind, raw = f["key"], f["kind"], vals.get(f["key"])
        if kind == "when":
            try:
                at = int(float(raw))
            except (TypeError, ValueError):
                raise WidgetError(f"«{f['label']}»: укажите дату и время.") from None
            cur = int(now if now is not None else time.time())
            if at <= cur:
                raise WidgetError("Дата события уже прошла — выберите время в будущем.")
            if at > cur + _WHEN_MAX_AHEAD_S:
                raise WidgetError("Дата события слишком далеко — не дальше пяти лет.")
            out[k] = at
        elif kind == "options":
            items = raw if isinstance(raw, list) else str(raw or "").split("\n")
            opts = [re.sub(r"[\x00-\x1f\x7f]", "", str(x)).strip() for x in items]
            opts = [x for x in opts if x]
            if not opts and not f.get("need"):
                continue
            if not OPTIONS_MIN <= len(opts) <= OPTIONS_MAX:
                raise WidgetError(f"Вариантов нужно от {OPTIONS_MIN} до {OPTIONS_MAX}.")
            if any(len(x) > OPTION_LEN for x in opts):
                raise WidgetError(f"Каждый вариант — не длиннее {OPTION_LEN} символов.")
            if len({x.lower() for x in opts}) != len(opts):
                raise WidgetError("Варианты повторяются.")
            out[k] = opts
            n_opts = len(opts)
        elif kind == "answer":
            if raw in (None, ""):
                raise WidgetError("Укажите номер верного варианта.")
            try:
                out[k] = int(raw)  # проверяется ниже, когда варианты уже известны
            except (TypeError, ValueError):
                raise WidgetError("Укажите номер верного варианта.") from None
        elif kind == "number":
            if raw in (None, ""):
                if f.get("need"):
                    raise WidgetError(f"Заполните поле «{f['label']}».")
                continue
            try:
                n = float(str(raw).replace(",", ".").replace(" ", ""))
            except (TypeError, ValueError):
                raise WidgetError(f"«{f['label']}»: нужно число.") from None
            if not 0 <= n <= NUMBER_MAX:
                raise WidgetError(f"«{f['label']}»: число от 0 до {NUMBER_MAX:,}.".replace(",", " "))
            out[k] = int(n) if n == int(n) else round(n, 2)
        elif kind == "url":
            s = _clean_text(raw, {"label": f["label"], "max": 256})
            if s and not re.match(r"^https://[^\s/]+\.[^\s]+$", s):
                raise WidgetError("Ссылка должна начинаться с https:// — другие Telegram не откроет.")
            if s:
                out[k] = s
        else:
            s = _clean_text(raw, f)
            if s:
                out[k] = s
        if f.get("need") and k not in out:
            raise WidgetError(f"Заполните поле «{f['label']}».")
    if template == "quiz":
        if not 1 <= out["a"] <= n_opts:
            raise WidgetError(f"Номер верного варианта — от 1 до {n_opts}.")
        out["a"] -= 1  # в форме счёт с единицы, в виджете — с нуля
    if template == "goal" and not out.get("g"):
        raise WidgetError("Цель должна быть больше нуля.")
    if template in ("card", "goal") and out.get("b") and not out.get("u"):
        raise WidgetError("У кнопки нет ссылки — укажите ссылку или уберите надпись.")
    n = vote_options(template, out)
    if n:
        out["v"] = [0] * n
        out["r"] = 0
    return out


def dump_data(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def load_data(text: str | None) -> dict:
    try:
        d = json.loads(text or "{}")
    except (TypeError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def title_of(template: str, data: dict) -> str:
    return str(data.get("t") or data.get("q") or (TEMPLATES.get(template) or {}).get("name")
               or "Свой виджет")[:120]


def merge_edit(template: str, old: dict, new: dict) -> dict:
    """Правка данных отправленного виджета: новые тексты, но СТАРЫЕ голоса.

    Варианты опроса после отправки не меняются: голоса лежат по номеру
    варианта, и перестановка отдала бы голоса чужому варианту.
    """
    out = dict(new)
    n = vote_options(template, old)
    if n:
        if template == "poll" and list(new.get("o") or []) != list(old.get("o") or []):
            raise WidgetError("Варианты опроса после отправки менять нельзя — "
                              "голоса привязаны к вариантам. Отправьте новый опрос.")
        out["v"] = list(old.get("v") or [0] * n)
        out["r"] = int(old.get("r") or 0) + 1
        if "n" in old:
            out["n"] = old["n"]
    return out


def vote_public(data: dict, counts: list[int], revision: int,
                names: list[str] | None = None) -> dict:
    """Данные голосования после голоса: счёт, номер правки, имена (кто идёт).

    Имена укорачиваются, пока данные не влезут в 1024 байта: длинные имена
    толпы не должны ломать сам счёт.
    """
    out = dict(data)
    out["v"] = [int(x) for x in counts]
    out["r"] = int(revision)
    if names is not None:
        keep = [str(x)[:24] for x in names[:RSVP_NAMES_MAX]]
        while True:
            out["n"] = keep
            if data_bytes(dump_data(out)) <= DATA_MAX or not keep:
                break
            keep = keep[:-1]
    return out


# ── Вызовы Telegram ─────────────────────────────────────────────────────────

def _desc(data: dict) -> str:
    return str((data or {}).get("description") or "")


def method_missing(data: dict) -> bool:
    """Telegram не знает метода (Bot API 10.4 ещё не включён на этом шлюзе)."""
    return int((data or {}).get("error_code") or 0) == 404 and "not found" in _desc(data).lower()


async def call(session, token: str, method: str, **params) -> dict:
    """Метод виджетов: основной Bot API, а если он метода не знает — бета.

    Порядок: основной адрес → (404) бета. Пока действует отметка «основной
    без виджетов», сначала бета; если и она метода не знает — всё равно
    пробуем основной, вдруг он уже обновился.
    """
    global _main_missing_until
    base = beta_base()
    beta_tried = False
    if base and time.monotonic() < _main_missing_until:
        data = await bot_api._call(session, token, method, api_base=base, **params)
        if not method_missing(data):
            return data
        beta_tried = True
    data = await bot_api._call(session, token, method, **params)
    if not method_missing(data):
        _main_missing_until = 0.0
        return data
    if not base or beta_tried:
        return data
    if not _main_missing_until:
        log.warning("bot_widgets: основной Bot API не знает %s — запасной путь через %s",
                    method, base)
    _main_missing_until = time.monotonic() + MAIN_RECHECK_S
    return await bot_api._call(session, token, method, api_base=base, **params)


_ERRORS_RU: tuple[tuple[str, str], ...] = (
    ("INPUT_MEDIA_WIDGET_TOO_LARGE",
     "Виджет слишком большой: код больше 32 КБ или после сжатия больше 4 КБ."),
    ("INPUT_MEDIA_WIDGET_HTML_INVALID",
     "Telegram не принял код виджета: нужен HTML-текст без нулевых символов."),
    ("INPUT_MEDIA_WIDGET_NOT_ALLOWED",
     "Сюда виджет отправить нельзя. Сейчас Telegram пускает виджеты только в личку "
     "тому, кто запустил бота; в группы и каналы — пока нельзя."),
    ("DATA_TOO_LARGE", "Данные виджета больше 1024 байт — сократите тексты."),
    ("DATA_INVALID", "Telegram не принял данные виджета."),
    ("WIDGET_QUERY_NOT_ALLOWED", "Этому виджету нельзя спрашивать бота."),
    ("WIDGET_ID_INVALID", "Виджет не найден у Telegram — возможно, его удалили."),
    ("MESSAGE_ID_INVALID", "Сообщение с виджетом не найдено — возможно, его удалили."),
    ("message to edit not found", "Сообщение с виджетом не найдено — возможно, его удалили."),
    ("CHAT_SEND_PHOTOS_FORBIDDEN",
     "В этой группе боту запрещено отправлять медиа — разрешите ему фото и медиа."),
    ("not enough rights",
     "В этой группе боту запрещено отправлять медиа — разрешите ему фото и медиа."),
    ("can't initiate conversation",
     "Бот не может написать первым: откройте бота, нажмите «Старт» и повторите."),
    ("bot was blocked", "Бот заблокирован в этом чате."),
    ("user is deactivated", "Аккаунт получателя удалён."),
    ("bot was kicked", "Бота удалили из этой группы."),
    ("bot is not a member", "Бота нет в этой группе — добавьте его и повторите."),
    ("chat not found",
     "Чат не найден: бота в нём нет, или человек ещё не запускал бота."),
)


def error_ru(data: dict) -> str:
    """Отказ Telegram — понятной фразой по-русски."""
    code = int((data or {}).get("error_code") or 0)
    desc = _desc(data)
    low = desc.lower()
    if method_missing(data):
        return ("Telegram пока не принимает виджеты от этого бота: Bot API 10.4 ещё "
                "не включён. Попробуйте позже.")
    for key, ru in _ERRORS_RU:
        if key.lower() in low:
            return ru
    if code == 429 or "flood" in low:
        wait = bot_api.retry_after_of(data)
        return f"Telegram просит подождать{f' {wait} с' if wait else ''} и повторить."
    if bot_api.token_rejected(code, desc):
        return "Токен бота больше не действует — обновите его в разделе «Боты»."
    if code == 0:
        return "Не удалось связаться с Telegram — повторите позже."
    return f"Telegram не принял виджет (код ошибки {code})."


def _sent(data: dict, chat_id) -> dict:
    msg = data.get("result") or {}
    w = msg.get("widget") or {}
    return {"ok": True, "message_id": msg.get("message_id"),
            "chat_id": (msg.get("chat") or {}).get("id", chat_id),
            "widget_id": str(w.get("id")) if w.get("id") else None}


async def send(session, token: str, chat_id, html: str, *, width: int = 0, height: int = 0,
               caption: str | None = None, allow_queries: bool = False,
               is_shared: bool = True, chat_data: str | None = None,
               public_data: str | None = None) -> dict:
    """sendWidget. {ok, message_id, chat_id, widget_id} или {ok: False, error}."""
    rep = check(html)
    if not rep["ok"]:
        return {"ok": False, "error": rep["errors"][0]}
    for value, what in ((public_data, "Общие данные"), (chat_data, "Данные чата")):
        err = data_error(value, what)
        if err:
            return {"ok": False, "error": err}
    data = await call(
        session, token, "sendWidget",
        chat_id=chat_id, html=html,
        width=int(width) if width else None,
        height=int(height) if height else None,
        caption=(caption or "")[:CAPTION_MAX] or None,
        allow_queries=True if allow_queries else None,
        is_shared=True if is_shared else None,
        chat_data=chat_data or None,
        public_data=public_data or None,
    )
    if not data.get("ok"):
        log.info("bot_widgets.send: отказ Telegram %s %s", data.get("error_code"),
                 _desc(data)[:200])
        return {"ok": False, "error": error_ru(data), "raw": data}
    return _sent(data, chat_id)


async def send_shared(session, token: str, chat_id, widget_id: str) -> dict:
    """sendSharedWidget: тот же общий виджет в другой чат — с общим счётом."""
    data = await call(session, token, "sendSharedWidget", chat_id=chat_id,
                      widget_id=str(widget_id))
    if not data.get("ok"):
        return {"ok": False, "error": error_ru(data), "raw": data}
    return _sent(data, chat_id)


async def send_shared_classified(session, token: str, chat_id, widget_id: str, *,
                                 disable_notification: bool = False
                                 ) -> tuple[bool, int | None, str]:
    """sendSharedWidget для рассылки: (ok, retry_after, категория ошибки).

    Та же форма, что у bot_api.send_message_classified: рассылка различает
    заблокировавшего бота, лимит Telegram и собственную ошибку одинаково для
    текста, фото и виджета.
    """
    data = await call(session, token, "sendSharedWidget", chat_id=chat_id,
                      widget_id=str(widget_id),
                      disable_notification=True if disable_notification else None)
    if data.get("ok"):
        return True, None, ""
    code = int(data.get("error_code") or 0)
    cat = bot_api.classify_send_error(code, _desc(data))
    if code == 429:
        return False, bot_api.retry_after_of(data) or 5, cat
    return False, None, cat


async def edit_data(session, token: str, *, chat_id=None, message_id: int | None = None,
                    widget_id: str | None = None, public_data: str | None = None,
                    chat_data: str | None = None) -> dict:
    """Поменять данные отправленного виджета.

    Общий виджет (widget_id) — editSharedWidgetData: доходит до всех копий.
    Иначе — editMessageWidgetData по чату и сообщению. Правка, которая ничего
    не меняет, у Telegram — «message is not modified»; это не ошибка.
    """
    for value, what in ((public_data, "Общие данные"), (chat_data, "Данные чата")):
        err = data_error(value, what)
        if err:
            return {"ok": False, "error": err}
    if widget_id and public_data is not None:
        data = await call(session, token, "editSharedWidgetData", widget_id=str(widget_id),
                          public_data=public_data, cache_time=SHARED_CACHE_S)
        if not (data.get("ok") or "not modified" in _desc(data).lower()):
            return {"ok": False, "error": error_ru(data)}
        if chat_data is None:
            return {"ok": True}
        public_data = None
    data = await call(session, token, "editMessageWidgetData",
                      chat_id=chat_id, message_id=int(message_id or 0),
                      public_data=public_data, chat_data=chat_data)
    if data.get("ok") or "not modified" in _desc(data).lower():
        return {"ok": True}
    return {"ok": False, "error": error_ru(data)}


async def answer_query(session, token: str, query_id: str, data: str | None = None) -> dict:
    """answerWidgetQuery: ответ на касание в виджете (не позже 15 секунд)."""
    return await call(session, token, "answerWidgetQuery",
                      widget_query_id=str(query_id), data=data or None)
