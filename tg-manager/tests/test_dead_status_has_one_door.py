"""Выборка аккаунтов под действие не имеет права судить о смерти сама.

ЧТО БЫЛО. Набор «мёртвых» значений `acc_status` был выписан литералами в
двадцати трёх запросах по всему продукту, и почти ни один не совпадал с
остальными. Массовая дверь (`resource_selector.select_all_active`) исключала
четыре статуса, одиночные пути — три, экран виртуального админа — свои шесть,
из которых половины (`restricted`, `flood`) никто никогда не пишет.

Из этого выходила настоящая дыра, а не только разнобой в числах: аккаунт со
статусом `spamblock` массовая дверь не брала, а одиночная операция брала и
шла работать уже ограниченной сессией — то есть добивала аккаунт, который
продукт специально отложил. То же для `deleted` и `frozen`: их добавили в
словарь, массовая дверь стала их отсекать, а двадцать с лишним запросов
продолжали считать такой аккаунт рабочим.

ЧТО ТЕПЕРЬ. Условие собирает одна дверь — `account_status.sql_not_dead()`, и
набор у неё один на продукт. Этот тест держит инвариант: ни один запрос не
выписывает набор сам.

ЧЕГО ЭТОТ ТЕСТ НЕ ТРЕБУЕТ. Не всякий список статусов в коде — это тот же
набор, и сводить их все было бы ошибкой. Отдельно и ОСОЗНАННО оставлены:

  * `account_health`/`account_monitor` — условие «сессия подтверждённо мертва»
    (`banned`/`deactivated`/`session_expired` ПЛЮС auth_error от Telegram).
    `spamblock` сессию не убивает, и деактивировать по нему аккаунт нельзя;
  * `account_rehab._DEAD_STATUSES` — кого реабилитация не берёт. Она как раз
    лечит `spamblock`, поэтому мёртвым он здесь быть не может;
  * `budget_radar.DEAD_STATUSES` — «деньги на прокси уже не окупятся», то есть
    безвозвратно; временные `spamblock`/`cooldown` намеренно не входят;
  * `warmup_status.DEAD_STATUSES`, `account_warmer` — чего прогрев не греет;
  * фильтр массового УДАЛЕНИЯ в мини-аппе — невоскрешаемые; спам-блок туда не
    входит, иначе кнопка «удалить мёртвых» удаляла бы лечащиеся аккаунты
    (держит `test_dead_status_is_one_vocabulary`).

Поэтому храповик проверяет ровно гейты, а любой список со СВОИМ смыслом
назван ниже в `_ALLOWED` с причиной. Три формы, которые он держит:

  1. SQL-условие `NOT IN` по `acc_status` (в `WHERE` и внутри
     `COUNT(*) FILTER` — внутри FILTER копия так же опасна, просто врёт
     счётчиком, а не выборкой);
  2. положительное `IN` на стороне ЗАПИСИ (`UPDATE ... SET acc_status = CASE
     WHEN ... IN (...)`): там набор решает, какой статус аккаунту присвоить,
     и короткий список молча оставляет аккаунт рабочим;
  3. питоновский литерал-множество (tuple/set/list) из трёх и более статусов
     словаря — ровно в этой форме гейты и расходились после сведения SQL:
     `op_worker._INVITE_UNSAFE_STATUS` (инвайт — самая баноопасная операция
     продукта) брал в работу аккаунт со статусом `deleted` или `frozen`.
"""
from __future__ import annotations

import ast
import pathlib

from services import account_status as acc_status

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEAD = set(acc_status.DEAD_STATUSES)


def _where(path: pathlib.Path) -> str:
    """Путь от корня репозитория; пробник проверяют и на файле вне репо."""
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _sources() -> list[pathlib.Path]:
    out: list[pathlib.Path] = []
    for folder in ("services", "bot"):
        out += sorted((ROOT / folder).rglob("*.py"))
    return out


def _hand_written_gates(path: pathlib.Path) -> list[str]:
    """Строки-запросы, где набор мёртвых статусов выписан вручную.

    Ищем по AST в строковых константах: `NOT IN (...)` по `acc_status` с двумя
    и более именами из словаря. Два — уже копия набора; одно имя это обычная
    точечная проверка («не забанен»), и запрещать её незачем.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return []
    found: list[str] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        text = node.value
        if "acc_status" not in text or "NOT IN" not in text:
            continue
        # Берём только то, что внутри скобок NOT IN (...) — иначе статус,
        # упомянутый в комментарии к запросу, читался бы как условие.
        for chunk in text.split("NOT IN")[1:]:
            inside = chunk[chunk.find("(") + 1:chunk.find(")")] if "(" in chunk else ""
            hits = {s for s in DEAD if f"'{s}'" in inside}
            if len(hits) >= 2:
                found.append(f"{_where(path)}:{node.lineno} {sorted(hits)}")
    return found


def test_no_query_writes_the_dead_set_by_hand():
    offenders: list[str] = []
    for path in _sources():
        if path.name == "account_status.py":
            continue          # сам словарь и живёт здесь
        offenders += _hand_written_gates(path)
    assert not offenders, (
        "эти выборки судят о смерти аккаунта по своему списку статусов:\n  "
        + "\n  ".join(offenders)
        + "\n\nСобирайте условие дверью account_status.sql_not_dead(): из-за "
          "расхождения этих списков аккаунт, который массовая дверь уже не "
          "берёт, одиночная операция брала и работала мёртвой сессией.")


def test_the_probe_catches_a_hand_written_gate(tmp_path):
    """Измеритель проверяем на заведомо больном примере, а не только на коде."""
    sick = tmp_path / "sick.py"
    sick.write_text(
        'SQL = "SELECT id FROM tg_accounts WHERE '
        "COALESCE(acc_status,'active') NOT IN ('banned','spamblock')\"\n",
        encoding="utf-8")
    assert _hand_written_gates(sick), "пробник не видит выписанный вручную набор"

    healthy = tmp_path / "healthy.py"
    healthy.write_text(
        'from services import account_status as _acc_status\n'
        'SQL = "SELECT id FROM tg_accounts WHERE " + _acc_status.sql_not_dead()\n',
        encoding="utf-8")
    assert not _hand_written_gates(healthy), "пробник ругается на правильный код"

    single = tmp_path / "single.py"
    single.write_text(
        'SQL = "SELECT id FROM tg_accounts WHERE acc_status NOT IN (\'banned\')"\n',
        encoding="utf-8")
    assert not _hand_written_gates(single), (
        "точечная проверка одного статуса — не копия набора")


def _write_side_sets(path: pathlib.Path) -> list[str]:
    """Положительное `IN` по `acc_status` в запросе, который ПИШЕТ статус.

    На чтении положительное `IN` законно («покажи мёртвых»), а на записи это
    тот же набор с обратным знаком: `SET acc_status = CASE WHEN acc_status IN
    (...)`. Короткий список здесь не отсекает лишних, а наоборот — оставляет
    аккаунту рабочий статус, и дверь выбора потом берёт его в работу.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return []
    found: list[str] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        text = node.value
        upper = text.upper()
        if "ACC_STATUS" not in upper or "UPDATE" not in upper:
            continue
        for chunk in text.split(" IN ")[1:]:
            inside = chunk[chunk.find("(") + 1:chunk.find(")")] if "(" in chunk else ""
            hits = {s for s in DEAD if f"'{s}'" in inside}
            if len(hits) >= 2:
                found.append(f"{_where(path)}:{node.lineno} {sorted(hits)}")
    return found


# Списки статусов со СВОИМ смыслом — не копии набора. Ключ:
# «файл::имя функции или <module>», причина обязательна.
_ALLOWED: dict[str, str] = {
    "services/account_health.py::_run_spambot_check_cycle":
        "«сессия подтверждённо мертва» — статус ПЛЮС auth_error от Telegram; "
        "spamblock сессию не убивает, деактивировать по нему нельзя",
    "services/account_manager.py::<module>":
        "_VERIFIED_RESTRICTION_STATUSES — что умеет подтвердить ответ @SpamBot, "
        "а не кого не берут операции",
    "services/account_monitor.py::_check_dead_sessions":
        "та же проверка мёртвой сессии по auth_error",
    "services/account_rehab.py::<module>":
        "кого не берёт реабилитация: она как раз лечит spamblock, поэтому "
        "мёртвым он здесь быть не может",
    "services/budget_radar.py::<module>":
        "«деньги на прокси уже не окупятся» — безвозвратно; временные "
        "spamblock/cooldown намеренно не входят",
    "services/warmup_status.py::<module>":
        "_TERMINAL_SKIPS — причины ПРОПУСКА действия (no_session, not_found), "
        "это не значения acc_status",
    "bot/handlers/health_dashboard.py::cb_health_real_check":
        "по auth_error выключаем аккаунт — снова «сессия подтверждённо мертва»",
}


def _enclosing_names(tree: ast.AST) -> dict[int, str]:
    """Для каждой строки — имя ближайшей функции или класса (или <module>)."""
    out: dict[int, str] = {}

    def walk(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = f"{prefix}{child.name}"
                for line in range(child.lineno, (child.end_lineno or child.lineno) + 1):
                    out.setdefault(line, name)
                walk(child, name + ".")
            else:
                walk(child, prefix)

    walk(tree, "")
    return out


def _python_set_copies(path: pathlib.Path) -> list[tuple[str, str]]:
    """Питоновские литералы-наборы мёртвых статусов: (ключ, описание).

    Порог — три статуса словаря в одном литерале. Два встречаются в законной
    точечной логике («забанен или удалён — выключить»), три это уже попытка
    пересказать набор своими словами.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return []
    enclosing = _enclosing_names(tree)
    found: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Set, ast.Tuple, ast.List)):
            continue
        values = {e.value.strip().lower() for e in node.elts
                  if isinstance(e, ast.Constant) and isinstance(e.value, str)}
        hits = values & DEAD
        if len(hits) >= 3:
            key = f"{_where(path)}::{enclosing.get(node.lineno, '<module>')}"
            found.append((key, f"{_where(path)}:{node.lineno} {sorted(hits)}"))
    return found


def test_no_python_copy_of_the_dead_set():
    """Гейт на питоне — такая же копия набора, как и в SQL.

    Сведение SQL-условий оставило дыру ровно здесь: `op_worker`,
    `account_health`, `trust_engine`, `strike_engine`, `invite_overflow` судили
    о смерти своими множествами из трёх значений, и `deleted`/`frozen` для них
    были рабочими статусами.
    """
    offenders: list[str] = []
    for path in _sources():
        if path.name == "account_status.py":
            continue
        for key, where in _python_set_copies(path):
            if key not in _ALLOWED:
                offenders.append(f"{where}   [{key}]")
    assert not offenders, (
        "эти наборы пересказывают словарь мёртвых статусов своими словами:\n  "
        + "\n  ".join(offenders)
        + "\n\nБерите account_status.DEAD_STATUSES / EFFECTIVE_DEAD_STATUSES "
          "(или is_dead / is_effectively_dead). Если набор ОТЛИЧАЕТСЯ по смыслу "
          "— внесите его в _ALLOWED этого теста вместе с причиной: список "
          "исключений с объяснениями честнее, чем молча разошедшиеся копии.")


def test_the_allowlist_has_no_stale_entries():
    """Разрешение без кода за ним — забытая строка, которая прикрывает новое.

    Иначе исключение, оставленное после удаления своего набора, со временем
    начинает разрешать ЧУЖОЙ набор в той же функции.
    """
    live = {key for path in _sources() for key, _ in _python_set_copies(path)}
    stale = sorted(set(_ALLOWED) - live)
    assert not stale, (
        "эти разрешения больше не на что ссылаются — удалите их из _ALLOWED:\n  "
        + "\n  ".join(stale))


def test_no_write_builds_its_own_dead_set():
    offenders: list[str] = []
    for path in _sources():
        if path.name == "account_status.py":
            continue
        offenders += _write_side_sets(path)
    assert not offenders, (
        "эти запросы РЕШАЮТ статус аккаунта по своему списку:\n  "
        + "\n  ".join(offenders)
        + "\n\nНабор для записи собирайте тем же словарём "
          "(account_status.sql_dead_list()).")


def test_the_probe_catches_the_shapes_that_slipped_through(tmp_path):
    """Самопроверка на трёх формах, которые пробник проглядел бы."""
    # 1. NOT IN внутри COUNT(*) FILTER — не в WHERE.
    in_filter = tmp_path / "filtered.py"
    in_filter.write_text(
        """SQL = "SELECT COUNT(*) FILTER (WHERE COALESCE(acc_status,'active') """
        """NOT IN ('banned','spamblock')) AS live"\n""",
        encoding="utf-8")
    assert _hand_written_gates(in_filter), "копия внутри FILTER не видна пробнику"

    # 2. Положительное IN на стороне ЗАПИСИ.
    on_write = tmp_path / "written.py"
    on_write.write_text(
        """SQL = "UPDATE tg_accounts SET acc_status = CASE WHEN acc_status """
        """IN ('banned','deactivated') THEN 'dead' ELSE acc_status END"\n""",
        encoding="utf-8")
    assert _write_side_sets(on_write), "копия на стороне записи не видна пробнику"

    # Положительное IN на ЧТЕНИИ — законно («покажи мёртвых»), ругаться нельзя.
    on_read = tmp_path / "read.py"
    on_read.write_text(
        """SQL = "SELECT id FROM tg_accounts WHERE """
        """acc_status IN ('banned','deactivated')"\n""",
        encoding="utf-8")
    assert not _write_side_sets(on_read), "пробник ругается на законное чтение"

    # 3. Питоновский литерал-набор.
    py_set = tmp_path / "pyset.py"
    py_set.write_text(
        'BAD = {"banned", "deactivated", "spamblock"}\n'
        "def gate(s):\n    return s not in BAD\n",
        encoding="utf-8")
    assert _python_set_copies(py_set), "питоновская копия набора не видна пробнику"

    small = tmp_path / "small.py"
    small.write_text('PAIR = ("banned", "deactivated")\n', encoding="utf-8")
    assert not _python_set_copies(small), (
        "два статуса — точечная логика, а не копия набора")


def test_the_door_is_actually_used():
    """Инвариант без потребителей ничего не значит."""
    users = [p.relative_to(ROOT) for p in _sources()
             if "sql_not_dead(" in p.read_text(encoding="utf-8")
             and p.name != "account_status.py"]
    assert len(users) >= 10, (
        f"дверь зовут всего из {len(users)} модулей — похоже, выборки опять "
        "судят сами")


def test_extra_statuses_only_make_the_door_stricter():
    """`extra` обязан РАСШИРЯТЬ набор: ослабить защиту им нельзя."""
    base = acc_status.sql_dead_list()
    wider = acc_status.sql_dead_list("warming", "restricted")
    for status in acc_status.DEAD_STATUSES:
        assert f"'{status}'" in wider, status
    assert len(wider) > len(base)
    # Повтор и регистр не ломают набор и не дублируют значения.
    assert acc_status.sql_dead_list("BANNED", "banned") == base


def test_virtual_admin_keeps_its_stricter_set():
    """Экран виртуального админа не трогает ещё и греющийся аккаунт."""
    import inspect

    from services import va_control

    src = inspect.getsource(va_control.eligible_accounts)
    assert "sql_not_dead" in src, "дверь виртуального админа судит сама"
    assert "warming" in src, (
        "греющийся аккаунт снова попал в выбор виртуального админа — прогрев "
        "нельзя перебивать действиями")
