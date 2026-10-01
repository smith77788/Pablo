"""«Сессия истекла, перезапустите приложение» — на сессии, которая продлилась.

Токен мини-аппа живёт два часа. Когда он истёк, `_apiRaw` получает 401, зовёт
`_reAuth()` и повторяет запрос. Защита от лавины обновлений была такой:

    let _reAuthPending = false;
    async function _reAuth() {
      if (_reAuthPending) return;      // ← возврат СРАЗУ, без ожидания
      ...
    }

Второй и последующие вызовы возвращали управление немедленно, не дождавшись
обновления. То есть повторяли свой запрос со СТАРЫМ токеном, получали второй 401
и бросали «Сессия истекла, перезапустите приложение».

И это был обычный исход, а не редкость: `boot()` запускает `loadHome`,
`loadSub`, `loadRef`, `loadBots` и `loadAdminSection` не дожидаясь друг друга.
Вернулся человек в приложение через два часа — все пять получают 401
одновременно: один чинится, остальные врут, что надо перезапускать.

Здесь берётся НАСТОЯЩИЙ код `_reAuth` и `_apiRaw` из index.html и исполняется в
node с подставным сервером: первый 401 на каждый запрос, обновление токена одно
на всех, повтор уходит уже с новым токеном.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

INDEX = Path(__file__).resolve().parents[1] / "mini_app" / "index.html"


def _fn_source(name: str) -> str:
    """Функция целиком, по балансу фигурных скобок.

    Начинать поиск тела с первой же `{` после имени нельзя: у `_apiRaw(path,
    opts={})` фигурная скобка стоит В СПИСКЕ ПАРАМЕТРОВ, и такой разбор
    обрывался на `opts={}` — тридцать шесть символов вместо функции. Сначала
    проходим список параметров до его закрывающей скобки.
    """
    src = INDEX.read_text("utf-8")
    m = re.search(r"^(?:async )?function " + re.escape(name) + r"\s*\(", src, re.M)
    assert m, f"функция {name} не найдена"
    pd, k = 0, m.end() - 1
    while k < len(src):
        if src[k] == "(":
            pd += 1
        elif src[k] == ")":
            pd -= 1
            if pd == 0:
                break
        k += 1
    i = src.index("{", k)
    depth, j = 0, i
    while j < len(src):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                break
        j += 1
    return src[m.start():j + 1]


HARNESS = r"""
'use strict';
const log = {authCalls: 0, tokensUsed: [], errors: [], results: []};

let TK = 'СТАРЫЙ';
const FETCH_TIMEOUT_MS = 30000;
const tg = {initData: 'подписанные-данные'};
function openSSE() {}

// Подставной сервер: пока предъявлен старый токен — 401; с новым — 200.
// Так ведёт себя продлённая сессия, и именно на ней старый код врал.
async function fetchT(path, opts) {
  if (path === '/api/miniapp/auth') {
    log.authCalls += 1;
    // Обновление не мгновенно: именно в этом окне и сходились остальные вызовы.
    await new Promise(r => setTimeout(r, 20));
    return {ok: true, status: 200, json: async () => ({token: 'НОВЫЙ'})};
  }
  const sent = (opts && opts.headers && opts.headers['Authorization']) || '';
  log.tokensUsed.push(sent);
  if (sent !== 'Bearer НОВЫЙ') {
    return {ok: false, status: 401, json: async () => ({error: 'истёк'})};
  }
  return {ok: true, status: 200, json: async () => ({ok: true, path})};
}

__REAUTH__
__APIRAW__

(async () => {
  // Пять запросов сразу — ровно как в boot().
  const paths = ['/api/miniapp/home', '/api/miniapp/sub', '/api/miniapp/ref',
                 '/api/miniapp/bots', '/api/miniapp/admin'];
  const out = await Promise.all(paths.map(p =>
    _apiRaw(p).then(v => ({ok: true, v}))
              .catch(e => { log.errors.push(String(e && e.message)); return {ok: false}; })));
  log.results = out.map(o => o.ok);
  console.log(JSON.stringify(log));
})();
"""


def _run() -> dict:
    src = (HARNESS
           .replace("__REAUTH__", _fn_source("_reAuth"))
           .replace("__APIRAW__", _fn_source("_apiRaw")))
    # `_reAuth` опирается на переменную-ссылку рядом с собой; в вырезанном куске
    # её нет, поэтому объявляем сами — так же, как объявлены подставные fetchT и tg.
    if "_reAuthInFlight" in src:
        src = "let _reAuthInFlight = null;\n" + src
    if "_reAuthPending" in src:
        src = "let _reAuthPending = false;\n" + src
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8") as fh:
        fh.write(src)
        path = fh.name
    res = subprocess.run(["node", path], capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr[:1200]
    return json.loads(res.stdout)


@pytest.mark.skipif(not shutil.which("node"), reason="нет node")
def test_every_request_recovers_after_the_token_expires():
    out = _run()
    assert out["results"] == [True] * 5, (
        "часть запросов не восстановилась после продления сессии: "
        f"{out['errors']}")
    assert not out["errors"], (
        "человеку сказали про истёкшую сессию, хотя она продлилась: "
        + "; ".join(out["errors"]))


@pytest.mark.skipif(not shutil.which("node"), reason="нет node")
def test_the_token_is_renewed_once_for_everyone():
    """Иначе пять одновременных 401 дают пять обновлений — лавина на сервер."""
    out = _run()
    assert out["authCalls"] == 1, (
        f"обновлений токена {out['authCalls']} вместо одного")


@pytest.mark.skipif(not shutil.which("node"), reason="нет node")
def test_the_retry_uses_the_new_token_not_the_old_one():
    """Суть дефекта: повтор уходил со старым токеном и получал второй 401."""
    out = _run()
    used = out["tokensUsed"]
    assert used.count("Bearer СТАРЫЙ") == 5, (
        "подставной сервер отработал не так, как задумано — проверка измеряет не то")
    assert used.count("Bearer НОВЫЙ") == 5, (
        "повтор ушёл со старым токеном: "
        f"новым воспользовались {used.count('Bearer НОВЫЙ')} раз из пяти")


def test_the_guard_waits_instead_of_bailing_out():
    """Флаг, по которому просто выходят, — это и есть исходный дефект."""
    body = _fn_source("_reAuth")
    assert "if (_reAuthPending) return;" not in body, (
        "вернулся флаг с немедленным возвратом: параллельные 401 снова "
        "повторятся со старым токеном")
    assert "return _reAuthInFlight" in body, (
        "_reAuth не отдаёт общее обещание — ждать параллельным вызовам нечего")
