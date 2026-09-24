"""Регрессия: «Скопировано» показывается только когда копирование произошло.

`navigator.clipboard` существует лишь в защищённом контексте, а встроенный
браузер Telegram даёт его не всегда. В мини-аппе было два способа копировать, и
оба обманывали пользователя:

  * `navigator.clipboard?.writeText(x).then(…).catch(…)` — при отсутствии
    clipboard необязательная цепочка возвращает undefined, и `.then` падает
    TypeError МИМО собственного `.catch`. Пользователь не получал ни текста в
    буфере, ни сообщения — кнопка просто молчала;
  * `navigator.clipboard?.writeText(x); toast('Скопировано')` — подпись об
    успехе показывалась всегда. На экспорте сессии аккаунта это читалось как
    «сессия у вас в буфере» при пустом буфере, а сессия — самое ценное, что
    есть у аккаунта. Там же перед этим спрашивали согласие словами «будет
    скопирована в буфер обмена».

Теперь копирование идёт через один хелпер: сначала штатный буфер, затем
запасной путь через скрытое поле, и только если не вышло ни то ни другое —
честное сообщение о неудаче. Возвращает true лишь при реальном успехе.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

MINI = Path(__file__).resolve().parents[1] / "mini_app"
INDEX = MINI / "index.html"


def _sources() -> dict[str, str]:
    out = {"mini_app/index.html": INDEX.read_text("utf-8")}
    for js in sorted((MINI / "screens").glob("*.js")):
        out[f"mini_app/screens/{js.name}"] = js.read_text("utf-8")
    return out


def _fn_source(name: str) -> str:
    src = INDEX.read_text("utf-8")
    for prefix in (f"\nasync function {name}(", f"\nfunction {name}("):
        idx = src.find(prefix)
        if idx >= 0:
            return src[idx:src.index("\n}", idx) + 2]
    raise AssertionError(f"функция {name} не найдена")


def test_no_optional_chain_then_on_clipboard():
    """`clipboard?.writeText(x).then(...)` падает TypeError мимо своего .catch."""
    bad = []
    for path, src in _sources().items():
        # Комментарии описывают прежнюю ошибку — они не код.
        code = re.sub(r"^\s*//.*$", "", src, flags=re.M)
        for m in re.finditer(r"navigator\.clipboard\s*\?\.\s*writeText\([^\n]*", code):
            bad.append(f"{path}:{code.count(chr(10), 0, m.start()) + 1}  {m.group(0)[:100]}")
    assert not bad, (
        "прямое копирование мимо copyToClipboard():\n  " + "\n  ".join(bad))


def test_clipboard_access_only_inside_the_helper():
    """Один источник правды: обращаться к буферу вправе только хелпер."""
    src = INDEX.read_text("utf-8")
    helper = _fn_source("copyToClipboard")
    outside = src.replace(helper, "")
    # Комментарии хелпера описывают прежние ошибки — из подсчёта их убираем.
    outside = re.sub(r"^\s*//.*$", "", outside, flags=re.M)
    hits = re.findall(r"navigator\.clipboard", outside)
    assert not hits, (
        f"{len(hits)} обращений к буферу обмена вне copyToClipboard() — "
        "подписи об успехе снова разъедутся")


def test_helper_has_fallback_and_honest_failure():
    body = _fn_source("copyToClipboard")
    assert "execCommand" in body, "нет запасного пути для незащищённого контекста"
    assert "return false" in body and "return true" in body, (
        "хелпер обязан сообщать вызывающему, удалось ли копирование")
    assert "Буфер обмена недоступен" in body, "нет честного сообщения о неудаче"


def test_session_export_awaits_the_copy():
    """Подтверждение обещает буфер обмена — обещание должно быть проверено."""
    body = _fn_source("exportAccSession")
    assert "await copyToClipboard(r.session" in body, (
        "экспорт сессии обязан дождаться копирования, а не сообщать об успехе заранее")
    assert "toast('💾 Сессия скопирована" not in body, (
        "отдельный тост об успехе снова обманет при пустом буфере")


def test_invite_link_shown_when_copy_failed():
    body = _fn_source("getChannelInviteLink")
    assert "copyToClipboard(r.invite_link" in body
    assert "!copied" in body, "при неудаче ссылку нужно показать текстом"


# ── Поведение хелпера в node при разных возможностях среды ───────────────────

_RUNNER = """
%(helper)s
const toasts = [];
function toast(m) { toasts.push(String(m)); }

// В node 22 globalThis.navigator — геттер, простым присваиванием не заменить.
Object.defineProperty(globalThis, 'navigator', {
  value: { %(clipboard)s }, configurable: true, writable: true,
});
const copied = [];
Object.defineProperty(globalThis, 'document', { configurable: true, writable: true, value: {
  execCommand: function (cmd) { %(exec)s },
  createElement: function () {
    return { style: {}, setAttribute() {}, select() {}, setSelectionRange() {},
             set value(v) { this._v = v; copied.push(v); }, get value() { return this._v; } };
  },
  body: { appendChild() {}, removeChild() {} },
}});

(async () => {
  const ok = await copyToClipboard('секрет-сессии', 'СКОПИРОВАНО');
  console.log(JSON.stringify({ ok, toasts }));
})();
"""

_CLIPBOARD_OK = "clipboard: { writeText: async function (s) { return; } }"
_CLIPBOARD_MISSING = ""
_CLIPBOARD_THROWS = ("clipboard: { writeText: async function () "
                     "{ throw new Error('нет разрешения'); } }")


def _run(clipboard: str, exec_body: str) -> dict:
    script = _RUNNER % {"helper": _fn_source("copyToClipboard"),
                        "clipboard": clipboard, "exec": exec_body}
    with tempfile.NamedTemporaryFile("w", suffix=".mjs", delete=False,
                                     encoding="utf-8") as fh:
        fh.write(script)
        path = fh.name
    res = subprocess.run(["node", path], capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr[:800]
    return json.loads(res.stdout)


@pytest.mark.skipif(not shutil.which("node"), reason="нет node")
def test_reports_success_when_clipboard_works():
    out = _run(_CLIPBOARD_OK, "return true;")
    assert out["ok"] is True
    assert out["toasts"] == ["СКОПИРОВАНО"]


@pytest.mark.skipif(not shutil.which("node"), reason="нет node")
def test_falls_back_when_clipboard_is_absent():
    """Ровно тот случай, в котором старый код падал молча."""
    out = _run(_CLIPBOARD_MISSING, "return true;")
    assert out["ok"] is True, "запасной путь не сработал"
    assert out["toasts"] == ["СКОПИРОВАНО"]


@pytest.mark.skipif(not shutil.which("node"), reason="нет node")
def test_falls_back_when_clipboard_rejects():
    out = _run(_CLIPBOARD_THROWS, "return true;")
    assert out["ok"] is True
    assert out["toasts"] == ["СКОПИРОВАНО"]


@pytest.mark.skipif(not shutil.which("node"), reason="нет node")
def test_admits_failure_when_nothing_works():
    out = _run(_CLIPBOARD_MISSING, "return false;")
    assert out["ok"] is False, "неудача не должна выдаваться за успех"
    assert out["toasts"] and "недоступен" in out["toasts"][0], (
        "пользователю не сказали, что скопировать не вышло")
    assert "СКОПИРОВАНО" not in out["toasts"]


@pytest.mark.skipif(not shutil.which("node"), reason="нет node")
def test_empty_text_is_refused():
    script = _RUNNER % {"helper": _fn_source("copyToClipboard"),
                        "clipboard": _CLIPBOARD_OK, "exec": "return true;"}
    script = script.replace("copyToClipboard('секрет-сессии', 'СКОПИРОВАНО')",
                            "copyToClipboard('', 'СКОПИРОВАНО')")
    with tempfile.NamedTemporaryFile("w", suffix=".mjs", delete=False,
                                     encoding="utf-8") as fh:
        fh.write(script)
        path = fh.name
    res = subprocess.run(["node", path], capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr[:800]
    out = json.loads(res.stdout)
    assert out["ok"] is False
    assert out["toasts"] == ["Нечего копировать"]
