"""boot() не зовёт то, чего ещё нет на странице.

ЧТО СЛУЧИЛОСЬ. Часть экранов вынесена в `mini_app/screens/*.js`, и эти
`<script src>` подключены ПОСЛЕ основного скрипта — то есть их функций не
существует до конца разбора страницы. При этом `boot()` в основном скрипте
вызывает `loadBots()`, который определён в `screens/bots_list.js`.

Сходилось это только по совпадению. `boot()` сначала ждёт ответа
`/api/miniapp/auth`, ответ идёт из сети — и разбор страницы за это время
успевает дойти до конца `<body>`. Стоило ответу прийти без выхода в цикл
событий — заглушка в проверке вёрстки отдавала готовый Response — и
`loadBots()` бросал `ReferenceError` ровно посередине boot(). Всё, что идёт
после него, молча не выполнялось: админский раздел, разбор deep-link из нуджа,
спасательный `recoverNudgeOpen`. Снаружи — одно «сбой в приложении» и главная
без половины данных; в проверке это и наблюдалось живьём.

ЧТО ДЕРЖИТ ЭТА ПРОВЕРКА. В boot() до явного ожидания готовности документа не
должно стоять ни одного вызова функции, определённой в `screens/*.js`. Само
ожидание тоже обязано быть на месте: без него «до ожидания» — это весь boot, и
проверка должна падать, а не молчать.

Мерка проверяется на заведомо больных образцах: подделанный boot с вызовом до
ожидания обязан быть пойман, он же после ожидания — обязан пройти.
"""
from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
_INDEX = ROOT / "mini_app" / "index.html"
_SCREENS = ROOT / "mini_app" / "screens"

# Функция верхнего уровня в вынесенном экране: именно она и попадает в window
# только к концу разбора страницы.
_TOP_FUNC = re.compile(r"^(?:async\s+)?function\s+([A-Za-z_$][\w$]*)", re.M)

# Явное ожидание готовности документа — рубеж, после которого экранные
# загрузчики уже есть.
_GATE = re.compile(r"document\.readyState[\s\S]{0,400}?DOMContentLoaded")


def _screen_funcs() -> set[str]:
    out: set[str] = set()
    for f in sorted(_SCREENS.glob("*.js")):
        out |= set(_TOP_FUNC.findall(f.read_text(encoding="utf-8")))
    return out


def _boot_body(src: str | None = None) -> str:
    src = _INDEX.read_text(encoding="utf-8") if src is None else src
    i = src.index("(async function boot() {")
    m = re.search(r"^\s*\}\)\(\);", src[i:], re.M)
    assert m, "конец boot() не найден — мерка ослепла"
    return src[i:i + m.end()]


def _findings(body: str, names: set[str]) -> list[str]:
    """Вызовы экранных функций в boot() до ожидания готовности документа."""
    g = _GATE.search(body)
    if g is None:
        return ["НЕТ ОЖИДАНИЯ ГОТОВНОСТИ ДОКУМЕНТА"]
    head = body[: g.start()]
    # Комментарии в расчёт не идут: в них эта беда как раз и объяснена.
    head = re.sub(r"//[^\n]*", "", head)
    return sorted({n for n in names if re.search(r"\b" + re.escape(n) + r"\s*\(", head)})


# ── Сама проверка ──────────────────────────────────────────────────────────

def test_boot_waits_before_it_calls_a_screen_function():
    names = _screen_funcs()
    assert names, "список функций вынесенных экранов пуст — мерка ослепла"
    bad = _findings(_boot_body(), names)
    assert not bad, (
        "boot() зовёт функции вынесенных экранов до ожидания готовности "
        "документа, их там ещё нет: " + ", ".join(bad))


def test_the_wait_is_actually_in_boot():
    assert _GATE.search(_boot_body()), (
        "в boot() нет явного ожидания готовности документа — порядок снова "
        "держится на совпадении со скоростью сети")


def test_the_screen_function_list_did_not_run_dry():
    names = _screen_funcs()
    assert "loadBots" in names, (
        "loadBots не виден в screens/*.js — именно он и падал, значит мерка "
        "смотрит не туда")
    assert len(names) > 100, f"функций вынесенных экранов найдено всего {len(names)}"


# ── Мерка на заведомо больных образцах ─────────────────────────────────────

_PROBE_CALL_BEFORE_WAIT = """(async function boot() {
  TK = await acquireSessionToken();
  loadBots();
  if (document.readyState === 'loading') {
    await new Promise(r => document.addEventListener('DOMContentLoaded', r, { once: true }));
  }
  loadHome();
})();
"""

_PROBE_CALL_AFTER_WAIT = """(async function boot() {
  TK = await acquireSessionToken();
  if (document.readyState === 'loading') {
    await new Promise(r => document.addEventListener('DOMContentLoaded', r, { once: true }));
  }
  loadBots();
  loadHome();
})();
"""

_PROBE_NO_WAIT = """(async function boot() {
  TK = await acquireSessionToken();
  loadHome();
})();
"""


def test_a_call_before_the_wait_is_caught():
    assert _findings(_boot_body(_PROBE_CALL_BEFORE_WAIT), {"loadBots"}) == ["loadBots"]


def test_a_call_after_the_wait_is_clean():
    assert _findings(_boot_body(_PROBE_CALL_AFTER_WAIT), {"loadBots"}) == []


def test_a_boot_without_the_wait_is_caught():
    assert _findings(_boot_body(_PROBE_NO_WAIT), {"loadBots"}) == [
        "НЕТ ОЖИДАНИЯ ГОТОВНОСТИ ДОКУМЕНТА"]


def test_a_mention_in_a_comment_is_not_a_call():
    probe = """(async function boot() {
  // loadBots() раньше падал здесь — это объяснение, а не вызов.
  if (document.readyState === 'loading') {
    await new Promise(r => document.addEventListener('DOMContentLoaded', r, { once: true }));
  }
  loadBots();
})();
"""
    assert _findings(_boot_body(probe), {"loadBots"}) == []
