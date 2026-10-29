"""Отмена не ждёт конца ПЕЙСИНГОВОЙ паузы — иначе флот стоит до двадцати минут.

РАЗРЫВ. Флуд-паузы отмену уже слышат (`bounded_flood_sleep` +
`tests/test_cancel_does_not_wait_out_the_flood_pause.py`). А собственный темп
исполнителей — пауза между группами, между ботами, перед постом — оставался
одним `asyncio.sleep` на всю длину. Длины там не секунды:

* `_exec_niche_growth_post` — 10–20 минут между группами (и это ещё умножается
  губернатором, множитель ≥1), 3–8 минут между вступлением и постом, 5–10 минут
  после флуда;
* `_exec_global_presence_bot`, `_exec_bot_factory`, `_exec_gp_bulk_apply` —
  от получаса секунд до трёх минут между реальными действиями.

Всё это время отмена не замечается: владелец нажал «Отменить», операция
показана работающей, арендованные аккаунты заняты и слот параллельности (один
из восьми) занят. Это ровно то поведение, из-за которого владелец «начинает
отменять и запускать заново, добирая новых ограничений».

ЧТО ПРОВЕРЯЕМ. Длинная пауза дробится и опрашивает отмену — но ранний выход
допускается ТОЛЬКО там, где следом идёт либо следующий виток цикла (а он
начинается с проверки отмены), либо явная проверка на месте. Там, где за
паузой сразу идёт реальное действие, пауза остаётся целой: укоротить её
значит сделать действие раньше, чем позволяет анти-бан-темп, а это дороже
простоя.

ВТОРАЯ ПЕРЕПИСЬ — ПО ИМЕНИ. Перепись по длине судит только то, что видно из
исходника, и честно молчит об остальном: `flood_wait`, `wait_time`,
`_cool_left`, `_invite_pause(acc_id)` приходят из Telegram и из движка флуда, и
статически у них нет нижней границы. Это ровно самые ДОЛГИЕ паузы в слое, то
есть дыра была в том месте, где она дороже всего. Вторая перепись берёт их по
имени длительности (флуд, остывание, ожидание) и требует того же: либо дверь,
ждущая отмену, либо запись в `_UNKNOWN_LENGTH_WHOLE` с причиной.

ОБ ИЗМЕРИТЕЛЕ ВТОРОЙ ПЕРЕПИСИ. Первая её версия искала подстроку в тексте
выражения и считала находкой КАЖДЫЙ `await _governed_delay(...)`: в слове
`await` сидит `wait`. Выходило 28 находок вместо 13 — измеритель был сломан
ровно так, как об этом предупреждает CLAUDE.md. Поэтому имена берутся по дереву
(`ast.Name`/`ast.Attribute`), а не поиском по строке, и на это стоят две
собственные проверки: на заведомо здоровом и на заведомо больном примере.
"""
from __future__ import annotations

import ast
import asyncio
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "services", "op_worker.py")

# Ниже какой длины пауза простоем флота не считается: отмену владелец и так
# заметит не мгновенно, а дробить секундные паузы — это лишние запросы к БД.
_LONG_S = 30.0

# Паузы, которые обязаны остаться целыми, с причиной для каждой. Ключ —
# (функция, выражение длины): он не ломается от сдвига строк.
#
# Первые три: пауза стоит ВПЛОТНУЮ перед реальным действием (поставить
# username, опубликовать пост), и между ними нет проверки отмены. Ранний
# выход сделал бы действие раньше, чем разрешает темп, — то есть обменял бы
# простой флота на риск ограничения, а это не обмен.
_MUST_STAY_WHOLE = {
    ("_exec_global_presence_channel", "pause"),
    ("_exec_global_presence_channel", "_post_delay"),
    ("_exec_bulk_create_channels", "_pub_delay"),
}


# Длительность «минутного класса»: пауза Telegram, остывание, накопленное
# ожидание. Короткий пейсинг между целями называется иначе (delay, jitter) и
# под перепись по имени не попадает: обрывать его незачем, отмену спросят на
# следующем витке.
_MINUTE_CLASS = re.compile(r"flood|cool|wait|pause|retry_after", re.I)

# Паузы НЕИЗВЕСТНОЙ длины, которые обязаны остаться целыми, с причиной каждой.
# Правило то же, что и у переписи по длине: ранний выход допустим только там,
# где следом проверяют отмену.
_UNKNOWN_LENGTH_WHOLE = {
    # Пауза Telegram, а сразу за ней — повтор того же действия (`continue` в
    # цикле попыток, который об отмене не спрашивает). Выйти раньше значит
    # опубликовать В ОКНО паузы: Telegram вернёт новый FloodWait и продлит
    # штраф аккаунту — см. комментарий у отбора отдохнувших аккаунтов.
    ("_exec_mass_publish", "flood_wait + random.uniform(2, 8)"),
    # Пауза Telegram, а сразу за ней создание канала.
    ("_exec_global_presence_channel", "wait_time"),
    ("_exec_bulk_create_channels", "wait_time"),
    # Пауза Telegram, а сразу за ней выдача username тому же каналу.
    ("_exec_global_presence_channel", "flood_wait"),
    # Пауза Telegram, а сразу за ней повторное создание бота у BotFather.
    ("_exec_bot_factory", "wait_secs + 15"),
    # Остывание аккаунта, а сразу за ней пост в канал.
    ("_exec_bulk_post_chans", "_cool_left + random.uniform(2, 8)"),
}


def _identifiers(node: ast.AST) -> set[str]:
    """Имена, упомянутые в выражении — ПО ДЕРЕВУ, не поиском по тексту.

    Ключевое слово `await` именем не является и сюда не попадает: см. раздел
    «ОБ ИЗМЕРИТЕЛЕ ВТОРОЙ ПЕРЕПИСИ» в докстринге модуля.
    """
    names: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name):
            names.add(sub.id)
        elif isinstance(sub, ast.Attribute):
            names.add(sub.attr)
    return names


def _bare_minute_sleeps(src: str) -> list[tuple[str, str, int]]:
    """Голые `asyncio.sleep` минутного класса в функциях операции."""
    tree = ast.parse(src)
    funcs = [n for n in ast.walk(tree)
             if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))]
    found: list[tuple[str, str, int]] = []
    for fn in funcs:
        args = {a.arg for a in fn.args.args} | {a.arg for a in fn.args.kwonlyargs}
        if not {"pool", "op_id"} <= args:
            continue
        # Вложенная функция — чужое тело: её сны принадлежат ей, а не этой.
        inner = {id(x) for g in funcs if g is not fn for x in ast.walk(g)}
        for node in ast.walk(fn):
            if id(node) in inner:
                continue
            if not (isinstance(node, ast.Call)
                    and ast.unparse(node.func).endswith("asyncio.sleep")
                    and node.args):
                continue
            arg = node.args[0]
            if isinstance(arg, ast.Constant):
                continue  # константа — техпауза, её длительность видна глазом
            if not any(_MINUTE_CLASS.search(name) for name in _identifiers(arg)):
                continue
            found.append((fn.name, ast.unparse(arg), node.lineno))
    return found


def test_the_name_census_sees_a_real_offender():
    """Измеритель на заведомо БОЛЬНОМ примере: голый сон флуд-паузы виден."""
    sick = ("import asyncio\n"
            "async def _exec_x(pool, op_id):\n"
            "    flood_wait = 600\n"
            "    await asyncio.sleep(flood_wait + 8)\n")
    assert _bare_minute_sleeps(sick), (
        "перепись не видит голый сон флуд-паузы — мерить ею нечего")


def test_the_name_census_does_not_count_the_await_keyword():
    """Измеритель на заведомо ЗДОРОВОМ примере: `await` — не `wait`."""
    healthy = ("import asyncio\n"
               "async def _exec_x(pool, op_id):\n"
               "    await asyncio.sleep(await _governed_delay(pool, 1, 2.0))\n")
    assert _bare_minute_sleeps(healthy) == [], (
        "короткий пейсинг через _governed_delay принят за флуд-паузу: в слове "
        "`await` сидит `wait`, и перепись обязана брать имена по дереву")


def test_the_name_census_does_not_count_the_cancel_aware_doors():
    """Измеритель не считает находкой сами двери."""
    cured = ("import asyncio\n"
             "async def _exec_x(pool, op_id):\n"
             "    flood_wait = 600\n"
             "    await bounded_flood_sleep(flood_wait, 'x', pool=pool, op_id=op_id)\n"
             "    await cancellable_pause(pool, op_id, flood_wait, 'x')\n")
    assert _bare_minute_sleeps(cured) == [], "вызов через дверь принят за голый сон"


def test_every_pause_of_unknown_length_hears_the_cancel():
    """Перепись по имени: ни одной минутной паузы мимо двери, ждущей отмену."""
    with open(SRC, encoding="utf-8") as fh:
        found = _bare_minute_sleeps(fh.read())
    # Пауза, уже признанную целой переписью по длине, второй записи не требует:
    # причина у неё одна (gp_channel «pause» стоит вплотную перед выдачей
    # username), и дублировать её — значит заводить две правды об одном месте.
    excused = _UNKNOWN_LENGTH_WHOLE | _MUST_STAY_WHOLE
    deaf = [f for f in found if (f[0], f[1]) not in excused]
    assert not deaf, (
        "эти паузы неизвестной длины глухи к отмене — «Стоп» владельца не "
        "доходит до исполнителя всё это время:\n"
        + "\n".join(f"  {fn} стр.{line}: {expr}" for fn, expr, line in deaf)
        + "\nПаузу Telegram ждут через `bounded_flood_sleep(..., pool=pool, "
        "op_id=op_id)`, свой пейсинг — через `cancellable_pause(pool, op_id, "
        "...)`. Если сразу за паузой идёт реальное действие — оставьте её "
        "целой и внесите в _UNKNOWN_LENGTH_WHOLE с причиной.")


def test_the_unknown_length_whole_pauses_are_still_there():
    """Самопроверка списка: мёртвая запись маскирует новую находку."""
    with open(SRC, encoding="utf-8") as fh:
        keys = {(fn, expr) for fn, expr, _ in _bare_minute_sleeps(fh.read())}
    stale = sorted(_UNKNOWN_LENGTH_WHOLE - keys)
    assert not (_UNKNOWN_LENGTH_WHOLE & _MUST_STAY_WHOLE), (
        "одна и та же пауза записана в оба реестра — причина должна быть одна")
    assert not stale, (
        f"этих пауз в коде больше нет: {stale} — уберите их из "
        "_UNKNOWN_LENGTH_WHOLE")


def _lower_bound(node: ast.AST, known: dict[str, float]) -> float | None:
    """Наименьшая длина сна, если её видно из исходника (иначе None).

    Считаем только то, что действительно видно: литерал, `random.uniform`
    (берём нижнюю границу), сумму, `max`/`min`, губернатор темпа (его
    множитель ≥1 по построению) и умножение на вызов-джиттер
    (`chaos_factor()` тоже ≥1). Всё остальное — неизвестность, а не ноль:
    такой сон храповик просто не судит.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return float(node.value)
    if isinstance(node, ast.Name):
        return known.get(node.id)
    if isinstance(node, ast.Await):
        return _lower_bound(node.value, known)
    if isinstance(node, ast.Call):
        name = ast.unparse(node.func)
        if name.endswith("random.uniform") and len(node.args) == 2:
            return _lower_bound(node.args[0], known)
        if name.endswith("_governed_delay") and node.args:
            return _lower_bound(node.args[-1], known)
        if name == "max":
            seen = [v for v in (_lower_bound(a, known) for a in node.args) if v is not None]
            return max(seen) if seen else None
        if name == "min" and node.args:
            seen = [v for v in (_lower_bound(a, known) for a in node.args) if v is not None]
            return min(seen) if seen else None
        return None
    if isinstance(node, ast.BinOp):
        left = _lower_bound(node.left, known)
        right = _lower_bound(node.right, known)
        if isinstance(node.op, ast.Add) and left is not None and right is not None:
            return left + right
        if isinstance(node.op, ast.Mult):
            if left is not None and isinstance(node.right, ast.Call):
                return left
            if right is not None and isinstance(node.left, ast.Call):
                return right
    return None


def _long_raw_sleeps(src: str) -> list[tuple[str, str, int, float]]:
    """Сырые asyncio.sleep длиной от _LONG_S: (функция, выражение, строка, сек)."""
    tree = ast.parse(src)
    found: list[tuple[str, str, int, float]] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        known: dict[str, float] = {}
        for node in ast.walk(fn):
            if (isinstance(node, ast.Assign) and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)):
                value = _lower_bound(node.value, known)
                if value is not None:
                    known[node.targets[0].id] = value
        for node in ast.walk(fn):
            if not (isinstance(node, ast.Call)
                    and ast.unparse(node.func).endswith("asyncio.sleep")
                    and node.args):
                continue
            low = _lower_bound(node.args[0], known)
            if low is not None and low >= _LONG_S:
                found.append((fn.name, ast.unparse(node.args[0]), node.lineno, low))
    return found


# ── Перепись длинных пауз ───────────────────────────────────────────────────

def test_every_long_pause_hears_the_cancel():
    with open(SRC, encoding="utf-8") as fh:
        src = fh.read()
    found = _long_raw_sleeps(src)
    deaf = [f for f in found if (f[0], f[1]) not in _MUST_STAY_WHOLE]
    assert not deaf, (
        "эти паузы глухи к отмене — операция держит слот и арендованные "
        "аккаунты до их конца:\n"
        + "\n".join(f"  {fn} стр.{line}: {expr} (от {low:.0f}с)"
                    for fn, expr, line, low in deaf)
        + "\nПропустите сон через _pause_watching_for_cancel(pool, op_id, …), "
        "если следом идёт виток цикла с проверкой отмены или такая проверка "
        "на месте. Если следом сразу реальное действие — оставьте паузу целой "
        "и внесите её в _MUST_STAY_WHOLE с причиной."
    )


def test_the_whole_pauses_are_still_there():
    """Самопроверка списка: исчезнувшая пауза — повод обновить причину, а не
    держать в храповике мёртвую запись (она маскирует новую находку)."""
    with open(SRC, encoding="utf-8") as fh:
        keys = {(fn, expr) for fn, expr, _, _ in _long_raw_sleeps(fh.read())}
    stale = sorted(_MUST_STAY_WHOLE - keys)
    assert not stale, (
        f"этих пауз в коде больше нет: {stale} — уберите их из _MUST_STAY_WHOLE")


def test_the_detector_bites():
    """Пауза в десять минут без отмены обязана быть находкой."""
    sick = (
        "async def _exec_demo(pool, op_id):\n"
        "    between = await _governed_delay(pool, 1, random.uniform(600, 1200))\n"
        "    await asyncio.sleep(between)\n"
    )
    assert _long_raw_sleeps(sick) == [("_exec_demo", "between", 3, 600.0)]


def test_the_invite_window_pause_ends_the_round_on_cancel():
    """У инвайта ранний выход обязан ОСТАНОВИТЬ круг, а не пропустить паузу.

    Длину этой паузы назначает governor чата (окно частоты приёма). Уже
    набранный чанк уходит в рассылку сразу после фазы набора, поэтому «вышли
    из паузы и пошли дальше» означало бы приглашения раньше, чем чат готов
    принимать — то есть новый флуд вместо экономии времени. Инвайт — самая
    баноопасная операция продукта, и здесь это не формальность.
    """
    with open(SRC, encoding="utf-8") as fh:
        src = fh.read()
    tree = ast.parse(src)
    target = None
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and ast.unparse(node.func).endswith("_pause_watching_for_cancel")
                and node.args and isinstance(node.args[-1], ast.Constant)
                and node.args[-1].value == "mass_invite_chat_window"):
            target = node
    assert target is not None, (
        "пауза окна чата у инвайта не найдена — ориентир сменился, поправьте тест")
    # Границы берём по AST (у оператора есть end_lineno), а не окном в символах:
    # окно фиксированной длины врёт при любой правке рядом.
    owner = None
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if fn.lineno <= target.lineno <= fn.end_lineno:
            if owner is None or fn.end_lineno - fn.lineno < owner.end_lineno - owner.lineno:
                owner = fn
    assert owner is not None and owner.name == "_iter_round", (
        f"пауза переехала в {owner.name if owner else '?'} — проверьте, что круг "
        "по-прежнему обязан завершаться по отмене")
    after = [n for n in ast.walk(owner)
             if isinstance(n, ast.If) and n.lineno > target.lineno
             and "_is_cancelled" in ast.unparse(n.test)]
    assert after, "после паузы окна чата нет проверки отмены"
    nearest = min(after, key=lambda n: n.lineno)
    assert nearest.lineno - target.end_lineno <= 8, (
        "проверка отмены стоит далеко от паузы — между ними успели появиться "
        "операторы, и смысл «вышли по отмене — круг закончен» потерялся")
    assert any(isinstance(x, ast.Return) for x in ast.walk(nearest)), (
        "по отмене круг обязан закончиться (return), иначе набранный чанк всё "
        "равно уйдёт в рассылку раньше окна чата")


def test_the_detector_spares_short_pauses():
    """Иначе храповик потребовал бы дробить секундные паузы натуральности."""
    healthy = (
        "async def _exec_demo(pool, op_id):\n"
        "    await asyncio.sleep(2)\n"
        "    await asyncio.sleep(random.uniform(1.5, 3.5))\n"
        "    await _pause_watching_for_cancel(pool, op_id, 600, 'demo')\n"
    )
    assert _long_raw_sleeps(healthy) == []


# ── Поведение самой паузы ───────────────────────────────────────────────────

class _Pool:
    def __init__(self, status: str | None = "running"):
        self._status = status

    async def fetchrow(self, sql, *a):
        return None if self._status is None else {"status": self._status}


@pytest.fixture
def ow(monkeypatch):
    from services import op_worker

    op_worker._cancel_cache.clear()
    monkeypatch.setattr(op_worker, "_CANCEL_PAUSE_STEP_S", 0.01)
    yield op_worker
    op_worker._cancel_cache.clear()


def test_a_cancelled_operation_leaves_a_long_pacing_pause_early(ow):
    slept = asyncio.run(ow._pause_watching_for_cancel(
        _Pool("cancelled"), 42, 1.0, "проверка"))
    assert slept < 1.0, (
        "пауза темпа не прервалась: отменённая операция держит флот до её конца")


def test_a_live_operation_keeps_the_whole_pacing_pause(ow):
    """Темп — анти-бан, а не вежливость: без отмены он выдерживается целиком."""
    slept = asyncio.run(ow._pause_watching_for_cancel(
        _Pool("running"), 42, 0.05, "проверка"))
    assert slept == pytest.approx(0.05, abs=1e-6)


# ── ТРЕТЬЯ ПЕРЕПИСЬ: пауза, собранная из многих коротких снов ─────────────────
#
# Две переписи выше спрашивают про ОДИН сон: длинный по значению или длинный по
# имени. Но минуты набираются и иначе: `for _ in range(30): await
# asyncio.sleep(5)` — каждый сон короткий, ни одна из двух переписей его не
# выберет, а операция глуха к отмене две с половиной минуты. Это второй СПОСОБ
# сделать то же самое, и выборка, названная через «длинный сон», его не видит
# (см. CLAUDE.md «У переписи два вопроса»).
#
# На сегодня таких циклов в слое НЕТ ни одного — и это проверенный ноль, а не
# молчание сломанного измерителя: ниже стоят самопроверки на заведомо больном,
# заведомо здоровом и заведомо коротком примере. Храповик держит ноль.
_LOOP_TOTAL_S = 60.0
_CANCEL_AWARE = ("_is_cancelled", "stop_requested", "cancellable_pause",
                 "is_cancelled", "_pause_watching_for_cancel",
                 "bounded_flood_sleep")


def _countable(node: ast.AST) -> float | None:
    """Длина, если её видно по тексту. Непосчитанное — не находка."""
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return float(node.value)
    if isinstance(node, ast.Call) and ast.unparse(node.func) in (
            "random.uniform", "random.randint"):
        vals = [_countable(a) for a in node.args]
        if vals and all(v is not None for v in vals):
            return max(vals)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult):
        a, b = _countable(node.left), _countable(node.right)
        if a is not None and b is not None:
            return a * b
    return None


def _sleepy_loops(src: str) -> list[tuple[int, float]]:
    """Циклы с СЧИТАЕМЫМ числом итераций, спящие суммарно долго без отмены.

    Число итераций берём только из литерального `range(...)`: `while True`
    фонового демона — не находка, его сон законен и прерывается отменой задачи.
    """
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    out: list[tuple[int, float]] = []
    for loop in ast.walk(tree):
        if not isinstance(loop, (ast.For, ast.AsyncFor, ast.While)):
            continue
        whole = ast.unparse(loop)
        if "sleep" not in whole or any(k in whole for k in _CANCEL_AWARE):
            continue
        iters = None
        if isinstance(loop, (ast.For, ast.AsyncFor)) and isinstance(loop.iter, ast.Call) \
                and ast.unparse(loop.iter.func) == "range":
            vals = [_countable(a) for a in loop.iter.args]
            if len(vals) == 1 and vals[0] is not None:
                iters = vals[0]
            elif len(vals) == 2 and all(v is not None for v in vals):
                iters = vals[1] - vals[0]
        if iters is None:
            continue
        total = 0.0
        for n in ast.walk(loop):
            if isinstance(n, ast.Call) and ast.unparse(n.func).endswith("sleep") \
                    and n.args:
                total += _countable(n.args[0]) or 0.0
        if total * iters >= _LOOP_TOTAL_S:
            out.append((loop.lineno, total * iters))
    return out


_SICK_LOOP = '''
async def f():
    for _ in range(30):
        await asyncio.sleep(5.0)
'''
_LOOP_WITH_A_CANCEL_CHECK = '''
async def f(pool, op_id):
    for _ in range(30):
        if await _is_cancelled(pool, op_id):
            break
        await asyncio.sleep(5.0)
'''
_SHORT_LOOP = '''
async def f():
    for _ in range(3):
        await asyncio.sleep(2.0)
'''


def test_the_loop_census_bites():
    """Измеритель, молчащий на заведомо больном примере, бесполезен."""
    assert _sleepy_loops(_SICK_LOOP), (
        "тридцать снов по пять секунд — две с половиной минуты глухоты, "
        "а перепись их не видит")


def test_the_loop_census_spares_a_loop_that_checks_the_cancel():
    assert not _sleepy_loops(_LOOP_WITH_A_CANCEL_CHECK)


def test_the_loop_census_spares_a_short_loop():
    assert not _sleepy_loops(_SHORT_LOOP)


def test_no_loop_waits_out_the_cancel_in_small_steps():
    import glob
    offenders = {}
    for path in sorted(glob.glob(os.path.join(ROOT, "services", "*.py"))):
        with open(path, encoding="utf-8") as f:
            found = _sleepy_loops(f.read())
        if found:
            offenders[os.path.basename(path)] = found
    assert not offenders, (
        "цикл копит минуты короткими снами и ни разу не смотрит на отмену: "
        f"{offenders}. Остановка операции владельцем будет ждать его до конца. "
        "Поставьте проверку отмены в голову цикла или cancellable_pause")
