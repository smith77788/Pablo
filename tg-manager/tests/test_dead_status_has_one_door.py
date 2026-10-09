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

Поэтому храповик проверяет ровно выборки — SQL-условия `NOT IN` по
`acc_status`, — а не любое упоминание статусов в питоне.
"""
from __future__ import annotations

import ast
import pathlib

from services import account_status as acc_status

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEAD = set(acc_status.DEAD_STATUSES)


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
                try:
                    where = path.relative_to(ROOT)
                except ValueError:
                    where = path          # пробник проверяют и на файле вне репо
                found.append(f"{where}:{node.lineno} {sorted(hits)}")
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
