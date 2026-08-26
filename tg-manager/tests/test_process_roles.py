"""Роли процесса и снятый потолок операций (находка аудита №2).

БЫЛО: бот, HTTP-API на 500+ маршрутов и ~49 фоновых циклов в ОДНОМ процессе
(`Procfile: web: python main.py`), а `_MAX_PARALLEL = 8` было зашито в код и
означало ёмкость ВСЕЙ платформы — все клиенты делили восемь слотов, и добавить
мощности было нечем.

СТАЛО: `ROLE=all|web|worker` разводит тот же образ по процессам, а потолок —
настройка воркера. Оба изменения СОВМЕСТИМЫ НАЗАД: без переменных окружения
поведение ровно прежнее, иначе включение ролей стало бы сюрпризом на деплое.
"""
from __future__ import annotations

import ast
import importlib
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(rel: str) -> str:
    return open(os.path.join(ROOT, rel), encoding="utf-8").read()


def _func_src(rel: str, name: str) -> str:
    src = _src(rel)
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return "\n".join(src.split("\n")[node.lineno - 1:node.end_lineno])
    raise AssertionError(f"{name} не найдена в {rel}")


# ── потолок операций ──────────────────────────────────────────────────────────

def _reload_worker(**env):
    """Перечитать op_worker с заданным окружением (константы читаются на импорте)."""
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update({k: str(v) for k, v in env.items()})
    try:
        from services import op_worker
        return importlib.reload(op_worker)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_parallel_cap_defaults_to_previous_behaviour():
    ow = _reload_worker(OP_MAX_PARALLEL="", OP_MAX_PARALLEL_PER_OWNER="")
    assert ow._MAX_PARALLEL == 8, "дефолт изменился — деплой без настроек поведёт себя иначе"
    assert ow._MAX_PARALLEL_PER_OWNER == 3


def test_parallel_cap_is_configurable():
    ow = _reload_worker(OP_MAX_PARALLEL="32", OP_MAX_PARALLEL_PER_OWNER="8")
    assert ow._MAX_PARALLEL == 32, "потолок платформы всё ещё зашит в код"
    assert ow._MAX_PARALLEL_PER_OWNER == 8
    _reload_worker(OP_MAX_PARALLEL="", OP_MAX_PARALLEL_PER_OWNER="")   # вернуть дефолт


def test_parallel_cap_survives_garbage_and_extremes():
    """Опечатка в переменной не должна ни ронять старт, ни снимать все ограничения."""
    assert _reload_worker(OP_MAX_PARALLEL="не-число")._MAX_PARALLEL == 8
    assert _reload_worker(OP_MAX_PARALLEL="0")._MAX_PARALLEL == 1        # зажим снизу
    assert _reload_worker(OP_MAX_PARALLEL="999999")._MAX_PARALLEL == 256  # зажим сверху
    _reload_worker(OP_MAX_PARALLEL="")


# ── роли процесса ─────────────────────────────────────────────────────────────

def test_role_env_var_is_namespaced():
    """Имя переменной обязано быть с префиксом.

    Короткое «ROLE» — слишком общее: если оно уже задано в окружении под что-то
    другое, ВСЕ фоновые задачи молча выключаются, и продукт выглядит зависшим.
    Ровно эта ошибка была допущена и стоила аварии.
    """
    src = _src("main.py")
    assert 'os.getenv("ROLE")' not in src, (
        "вернулось общее имя ROLE — риск коллизии с чужой переменной окружения")


def test_role_default_is_all_and_unknown_falls_back():
    src = _src("main.py")
    assert '_ROLE = (os.getenv("INFRAGRAM_ROLE") or "all")' in src, "роль по умолчанию не 'all'"
    assert '_VALID_ROLES = ("all", "web", "worker")' in src
    # неизвестная роль не роняет процесс, а честно предупреждает и работает как all
    i = src.index("_VALID_ROLES")
    head = src[i:i + 600]
    assert "log.warning" in head and '_ROLE = "all"' in head


def test_web_role_starts_no_background_loops():
    """ROLE=web обязан пропускать фоновые сервисы — иначе разделение бессмысленно."""
    body = _func_src("main.py", "_resilient")
    assert '_ROLE == "web"' in body, "фоновые циклы не отключаются в роли web"
    # выход ДО запуска сервиса, а не после
    assert body.index('_ROLE == "web"') < body.index("while True"), \
        "проверка роли стоит после запуска цикла — сервис всё равно стартует"


def test_worker_role_does_not_poll_telegram():
    """Два процесса на одном getUpdates отбирают апдейты друг у друга: Telegram
    отдаёт апдейт ровно одному, и бот начал бы отвечать через раз.

    Проверяем ПОРЯДОК ветвей, а не окно фиксированной длины: окно уже один раз
    промахнулось, когда поллинг обернули в цикл перезапуска.
    """
    src = _src("main.py")
    i_worker = src.index('_ROLE == "worker"')
    i_poll = src.index("await dp.start_polling(")
    assert i_worker < i_poll, (
        "ветка воркера обязана стоять ДО поллинга — иначе воркер тоже начнёт "
        "забирать апдейты и бот будет отвечать через раз")
    between = src[i_worker:i_poll]
    assert "else:" in between, (
        "поллинг должен быть в ветке else относительно роли worker")


def test_role_gate_is_in_one_place_not_scattered():
    """Гейт роли — в одной точке (_resilient), а не размазан по 49 создателям задач.

    Размазанная проверка гарантированно разъедется: новый сервис добавят без неё.
    """
    src = _src("main.py")
    # в самом файле роль упоминается считаное число раз: объявление, гейт, поллинг
    hits = [ln for ln in src.split("\n")
            if "_ROLE" in ln and not ln.strip().startswith("#")]
    assert len(hits) <= 8, f"проверка роли расползлась по файлу ({len(hits)} мест)"
    assert sum(1 for ln in hits if '_ROLE == "web"' in ln) == 1
