"""Мини-апп не должен умирать целиком, если SDK Telegram не загрузился.

ЧТО ЭТО БЫЛО. Приложение лежало у пользователя двое суток, и ни один из 3600
тестов этого не видел. Логи сервера показали картину точно: `/miniapp/` отдаётся
(304), `screens/*.js` отдаются, `/favicon.ico` — 404, и НИ ОДНОГО запроса к
`/api/…`. То есть страница пришла, скрипты пришли, а приложение не сделало
вообще ничего.

Причина — третья строка главного скрипта:

    const tg = window.Telegram.WebApp;

Весь мини-апп (766 КБ) лежит в ОДНОМ inline-`<script>`, и эта строка стоит в его
начале без всякой защиты. SDK подключается внешним тегом с telegram.org; стоит
ему не открыться — сеть, VPN, корпоративный прокси, блокировщик — и
`window.Telegram` не существует. TypeError на третьей строке убивает весь
скрипт: не объявляется ни одна функция, не уходит ни один запрос. Пользователь
видит статическую разметку — шапку, плитки, вечные спиннеры — и «приложение не
загружается». Ошибка при этом видна только в консоли, которой у него нет.

ЗДЕСЬ мы выполняем НАСТОЯЩИЙ скрипт страницы в окружении без SDK и требуем,
чтобы он не падал. Проверка делается через node — если его нет, файл
пропускается (статический храповик ниже работает всегда).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import textwrap

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(ROOT, "mini_app", "index.html")
_NODE = shutil.which("node") or shutil.which("nodejs")

_INLINE_SCRIPT = re.compile(
    r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S)


def _html() -> str:
    return open(INDEX, encoding="utf-8").read()


def _inline_scripts() -> list[str]:
    return _INLINE_SCRIPT.findall(_html())


# ── выполнение настоящего скрипта без SDK ────────────────────────────────────

_HARNESS = textwrap.dedent("""
    const fs = require('fs');
    const src = fs.readFileSync(process.argv[2], 'utf8');
    const noop = () => {};
    function mkEl() {
      return {
        style: {}, dataset: {},
        classList: { add: noop, remove: noop, contains: () => false },
        innerHTML: '', textContent: '', value: '', checked: false,
        appendChild: noop, removeChild: noop,
        addEventListener: noop, removeEventListener: noop,
        querySelector: () => mkEl(), querySelectorAll: () => [],
        click: noop, focus: noop, scrollIntoView: noop,
        getContext: () => null, setAttribute: noop, getAttribute: () => null,
        closest: () => null, remove: noop, insertAdjacentHTML: noop,
      };
    }
    // Ключевое условие опыта: SDK Telegram НЕ загрузился.
    global.window = { addEventListener: noop, location: { hash: '', href: '' },
                      Telegram: undefined };
    global.document = {
      getElementById: () => mkEl(), querySelector: () => mkEl(),
      querySelectorAll: () => [], createElement: () => mkEl(),
      addEventListener: noop, body: mkEl(), documentElement: mkEl(),
    };
    global.localStorage = { getItem: () => null, setItem: noop, removeItem: noop };
    global.navigator = { clipboard: { writeText: async () => {} }, userAgent: 'node' };
    global.fetch = async () => { throw new Error('нет сети'); };
    global.EventSource = function () { return { addEventListener: noop, close: noop }; };
    global.setInterval = () => 0;
    global.clearInterval = noop;
    global.AbortController = AbortController;
    try {
      new Function(src)();
      console.log(JSON.stringify({ ok: true }));
    } catch (e) {
      console.log(JSON.stringify({ ok: false, err: e.constructor.name + ': ' + e.message }));
    }
""")


def _run_without_sdk(script: str, tmp_path) -> dict:
    src = tmp_path / "app.js"
    src.write_text(script, encoding="utf-8")
    harness = tmp_path / "harness.js"
    harness.write_text(_HARNESS, encoding="utf-8")
    out = subprocess.run([_NODE, str(harness), str(src)],
                         capture_output=True, text=True, timeout=120)
    line = (out.stdout or "").strip().split("\n")[-1] if out.stdout else ""
    try:
        return json.loads(line)
    except Exception:
        pytest.fail(f"песочница не отработала:\nSTDOUT: {out.stdout}\nSTDERR: {out.stderr}")


@pytest.mark.skipif(not _NODE, reason="нужен node для выполнения скрипта страницы")
def test_main_script_does_not_die_without_the_sdk(tmp_path):
    """Главное: скрипт выполняется до конца, а не умирает на третьей строке."""
    scripts = _inline_scripts()
    assert scripts, "в index.html не найдено ни одного inline-скрипта"
    res = _run_without_sdk(scripts[-1], tmp_path)
    assert res.get("ok"), (
        "главный скрипт падает без SDK Telegram: " + str(res.get("err"))
        + "\n\nЭто убивает ВЕСЬ мини-апп: не объявляется ни одна функция и не "
          "уходит ни один запрос к API. Пользователь видит вечные спиннеры."
    )


@pytest.mark.skipif(not _NODE, reason="нужен node для проверки синтаксиса")
def test_every_inline_script_parses(tmp_path):
    """Синтаксическая ошибка в разметке ломает страницу так же насмерть."""
    for i, script in enumerate(_inline_scripts()):
        f = tmp_path / f"s{i}.js"
        f.write_text(script, encoding="utf-8")
        r = subprocess.run([_NODE, "--check", str(f)],
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, f"inline-скрипт #{i} не разбирается:\n{r.stderr[:800]}"


_EARLY_HARNESS = textwrap.dedent("""
    const noop = () => {};
    let ready = null;
    const box = { style: {}, innerHTML: '' };
    // Ранний скрипт стоит в <head>: тела страницы ещё нет, элемента тоже.
    global.window = { addEventListener: noop, Telegram: undefined };
    global.document = {
      getElementById: () => (ready === true ? box : null),
      addEventListener: (e, f) => { if (e === 'DOMContentLoaded') ready = f; },
    };
    new Function(process.argv[2])();
    if (typeof ready !== 'function') {
      console.log(JSON.stringify({ ok: false, err: 'не подписался на DOMContentLoaded' }));
    } else {
      const f = ready; ready = true; f();      // разметка разобрана
      console.log(JSON.stringify({ ok: true, html: box.innerHTML }));
    }
""")


@pytest.mark.skipif(not _NODE, reason="нужен node для выполнения раннего скрипта")
def test_user_actually_sees_the_reason(tmp_path):
    """Сообщение обязано ПОЯВИТЬСЯ на экране, а не просто существовать в коде.

    Ранний скрипт живёт в <head>, где тела страницы ещё нет: если показать
    сразу, элемента не найдётся и пользователь снова увидит пустоту. Здесь
    проверяется весь путь — отложили и показали, когда разметка разобрана.
    """
    early = _inline_scripts()[0]
    harness = tmp_path / "early.js"
    harness.write_text(_EARLY_HARNESS, encoding="utf-8")
    out = subprocess.run([_NODE, str(harness), early],
                         capture_output=True, text=True, timeout=60)
    line = (out.stdout or "").strip().split("\n")[-1] if out.stdout else ""
    try:
        res = json.loads(line)
    except Exception:
        pytest.fail(f"песочница не отработала:\n{out.stdout}\n{out.stderr}")
    assert res.get("ok"), res.get("err")
    assert "Telegram SDK" in res.get("html", ""), (
        "экран ошибки пуст — пользователь снова увидит замерший интерфейс без "
        f"объяснений: {res.get('html', '')[:200]!r}")
    assert "Перезагрузить" in res.get("html", ""), "нет кнопки перезагрузки"


# ── храповики, работающие без node ───────────────────────────────────────────

def test_sdk_is_dereferenced_defensively():
    """`window.Telegram.WebApp` не должен разыменовываться без проверки.

    Защищённым считаем чтение, перед которым в том же выражении стоит проверка
    существования (`window.Telegram && …` или `window.Telegram?.`). Строки
    комментариев не в счёт — они ничего не исполняют.
    """
    html = _html()
    bad = []
    for m in re.finditer(r"\bwindow\.Telegram\.WebApp\b", html):
        line_start = html.rfind("\n", 0, m.start()) + 1
        line = html[line_start:html.find("\n", m.start())]
        stripped = line.strip()
        if stripped.startswith(("*", "//", "#")) or "`" in line[:m.start() - line_start]:
            continue                    # комментарий/документация
        before = html[max(0, m.start() - 60):m.start()]
        if "window.Telegram &&" in before or "window.Telegram?." in before:
            continue                    # существование проверено рядом
        bad.append(line.strip()[:90])
    assert not bad, (
        "window.Telegram.WebApp читается без защиты — если SDK не загрузился, "
        "TypeError убьёт весь скрипт мини-аппа. Нужна проверка "
        "`(window.Telegram && window.Telegram.WebApp) || <заглушка>`."
    )


def test_early_error_screen_exists_before_the_main_script():
    """Ошибка старта обязана быть ВИДНА, а не только в консоли.

    Ранний обработчик ставится до основного скрипта: если приложение не встало,
    пользователь должен получить объяснение и кнопку перезагрузки, а не
    замерший интерфейс.
    """
    html = _html()
    i_guard = html.find("__fatalBoot")
    i_main = html.rfind("const tg =")
    assert i_guard != -1, "нет раннего экрана ошибки старта"
    assert i_guard < i_main, (
        "обработчик стоит ПОСЛЕ основного скрипта — он не поможет, если тот "
        "упадёт на первых строках")
    assert "location.reload()" in html, "на экране ошибки нет кнопки перезагрузки"


def test_missing_sdk_is_explained_to_the_user():
    """Сообщение обязано называть причину и что делать, а не «ошибка»."""
    html = _html()
    i = html.find("Не загрузился Telegram SDK")
    assert i != -1, "случай «SDK не загрузился» не объясняется отдельно"
    near = html[i:i + 700]
    for hint in ("сеть", "заново"):
        assert hint in near, f"в подсказке нет слова «{hint}» — совет неполный"
