"""Генератор подключён к мастеру и исполнителю — а не лежит мёртвым кодом.

Класс багов «параметр принят, но не доходит до эффекта» — самый дорогой в этом
модуле: интерфейс обещает уникальные названия, описания и аватары, операция
рапортует «готово», а в Telegram уезжают одинаковые объекты без фото. Юнит-тест
генератора такое не поймает: он проверяет функцию, а не то, что её кто-то
вызывает.

Поэтому здесь статические проверки цепочки:
  мастер → сборка целей → план в БД → исполнитель → Telegram.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HANDLER = ROOT / "bot" / "handlers" / "global_presence.py"
WORKER = ROOT / "services" / "op_worker.py"
DB = ROOT / "database" / "db.py"
SCHEMA = ROOT / "schema_v159.sql"


def _src(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _func_src(path: Path, name: str) -> str:
    """Исходник функции верхнего уровня по имени."""
    tree = ast.parse(_src(path))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(_src(path), node) or ""
    raise AssertionError(f"функция {name} не найдена в {path.name}")


# ── Мастер: генератор реально вызывается ────────────────────────────────────

def test_preview_builds_real_targets():
    # Предпросмотр обязан показывать РЕАЛЬНЫЕ цели плана: пользователь
    # подтверждает то, что увидел. Выдуманные примеры-образцы этого не дают.
    src = _func_src(HANDLER, "_show_preview")
    assert "_generate_targets" in src
    assert "summarize_targets" in src


def test_launch_uses_generated_targets():
    src = _func_src(HANDLER, "cb_gp_launch")
    assert "_generate_targets" in src, "запуск обязан собирать цели генератором"
    assert "build_targets(" not in src, "старый плоский планировщик не должен вернуться"


def test_confirm_screen_counts_targets_not_cities():
    # Регресс: экран считал объекты по числу ГОРОДОВ. На структуре из 4 тематик
    # он обещал вчетверо меньше, чем создавалось на самом деле.
    src = _func_src(HANDLER, "_cb_gp_confirm_preview_impl")
    assert "_generate_targets" in src
    assert 'summary["total"]' in src or "n_targets" in src


def test_generator_fields_persisted_on_launch():
    # Без сохранения seed/ролей/уровней повторная сборка (ретрай, отчёт) дала бы
    # другой набор объектов, чем показал предпросмотр.
    src = _func_src(HANDLER, "_submit_plan")
    for field in ("roles=", "levels=", "plan_seed=", "avatar_style="):
        assert field in src, f"поле {field} не передаётся в план"


def test_taken_usernames_consulted_before_allocation():
    src = _func_src(HANDLER, "_generate_targets")
    assert "get_taken_usernames" in src, (
        "аллокатор обязан знать уже занятые имена владельца, иначе коллизия "
        "всплывёт только отказом Telegram в бою"
    )
    assert "taken_usernames=" in src


def test_bot_targets_routed_to_botfather_executor():
    # Боты создаются через BotFather другим исполнителем: если бот-цели уедут
    # в канальную операцию, они молча не создадутся.
    src = _func_src(HANDLER, "cb_gp_launch")
    assert "global_presence_bot" in src
    assert 'asset_type") == "bot"' in src or "'bot'" in src


def test_launch_reports_queue_failure_honestly():
    # «План сохранён, но в очередь не встал» обязано доходить до экрана:
    # иначе пользователь ждёт выполнения, которого не начнётся.
    src = _func_src(HANDLER, "cb_gp_launch")
    assert "не встал в очередь" in src


# ── Мёртвые кнопки ──────────────────────────────────────────────────────────

def test_every_callback_action_has_handler():
    """Каждое действие GeoPresenceCb(action=...) имеет обработчик.

    Гейт класса «мёртвая кнопка»: кнопка отрисована, а фильтра под неё нет —
    нажатие уходит в никуда, и заметно это только вручную.
    """
    src = _src(HANDLER)
    emitted = set(re.findall(r'GeoPresenceCb\(\s*action="([a-z_]+)"', src))
    handled = set(re.findall(r'F\.action\s*==\s*"([a-z_]+)"', src))
    missing = emitted - handled
    assert not missing, f"кнопки без обработчика: {sorted(missing)}"


def test_new_states_declared():
    states = _src(ROOT / "bot" / "states.py")
    block = states.split("class GlobalPresenceFSM")[1].split("class ")[0]
    for st in ("choosing_structure", "choosing_levels", "choosing_avatar"):
        assert st in block, f"состояние {st} не объявлено"


# ── Исполнитель: сгенерированное доходит до Telegram ────────────────────────

def _gp_executor() -> str:
    return _func_src(WORKER, "_exec_global_presence_channel")


def test_worker_uses_generated_description():
    src = _gp_executor()
    assert 'target.get("planned_about")' in src, (
        "описание генерируется на этапе плана; исполнитель обязан его применять, "
        "а не подставлять одинаковую строку на всю сеть"
    )


def test_worker_applies_avatar():
    src = _gp_executor()
    assert "avatar_factory" in src and "set_channel_photo" in src


def test_avatar_failure_does_not_fail_the_target():
    # Аватар — оформление. Его сбой не должен уводить уже созданный канал в
    # 'failed': объект существует, и повторное создание сделало бы дубль.
    src = _gp_executor()
    idx = src.find("avatar_factory")
    assert idx > 0
    window = src[idx - 400 : idx + 1600]
    assert "except Exception" in window


def test_worker_records_applied_username_not_planned():
    """Регресс: в каталог писался planned_username независимо от исхода.

    При занятом имени канал уезжал в managed_channels с username, которого у
    него нет, — ссылки из отчёта вели в никуда.
    """
    src = _gp_executor()
    assert "applied_username" in src
    m = re.search(r"INSERT INTO managed_channels.*?\n(.*?)\n\s*\)", src, re.DOTALL)
    assert m, "INSERT в managed_channels не найден"
    args = m.group(1)
    assert "applied_username" in args
    assert 'target.get("planned_username")' not in args


def test_worker_records_group_type_for_groups():
    """Регресс: группы писались как type='channel'.

    Групповые фильтры смотрят type IN ('megagroup','supergroup','group','chat'),
    поэтому созданные через Global Presence группы не попадали ни в один из них.
    """
    src = _gp_executor()
    m = re.search(r"INSERT INTO managed_channels.*?\n(.*?)\n\s*\)", src, re.DOTALL)
    assert m
    assert '"group" if is_group else "channel"' in m.group(1)


def test_worker_resolves_asset_type_per_target():
    """Смешанная структура: в одном плане и каналы, и чаты.

    Общий флаг плана превратил бы половину структуры не в тот тип актива.
    """
    src = _gp_executor()
    assert 'target.get("asset_type")' in src
    # Флаг должен вычисляться внутри цикла по целям, а не один раз на план.
    loop_pos = src.find("for i, target in enumerate(targets)")
    flag_pos = src.find('is_group = (target.get("asset_type")')
    assert loop_pos > 0 and flag_pos > loop_pos


def test_worker_summary_not_hardcoded_to_channels():
    src = _gp_executor()
    assert "Создано каналов:" not in src, (
        "итог операции обязан совпадать с тем, что реально создано "
        "(в плане могут быть группы)"
    )


# ── Схема и слой БД ─────────────────────────────────────────────────────────

def test_schema_adds_generator_columns():
    sql = _src(SCHEMA)
    for col in (
        "roles", "levels", "name_pool", "username_pool", "about_pool",
        "avatar_style", "plan_seed", "planned_about", "avatar_seed",
        "final_username", "avatar_applied", "role", "level",
    ):
        assert col in sql, f"колонка {col} отсутствует в schema_v159"


def test_schema_columns_are_additive():
    # Новые колонки обязаны быть NULLable/с DEFAULT: планы, созданные до
    # генератора, продолжают исполняться прежним путём.
    sql = _src(SCHEMA)
    add_lines = [ln for ln in sql.splitlines() if "ADD COLUMN" in ln]
    assert add_lines, "в файле нет ни одного ADD COLUMN"
    for ln in add_lines:
        assert "IF NOT EXISTS" in ln, ln
        # NOT NULL без DEFAULT упадёт на непустой таблице.
        assert "NOT NULL" not in ln or "DEFAULT" in ln, ln


def test_db_persists_target_generator_fields():
    src = _func_src(DB, "create_global_presence_targets")
    for field in ("role", "level", "planned_about", "avatar_seed", "avatar_style"):
        assert f't.get("{field}")' in src, f"поле {field} не сохраняется в цель"


def test_taken_usernames_is_fail_open():
    # Потерять подсказку об именах — терпимо; не дать запустить проект из-за
    # сбоя вспомогательного запроса — нет.
    src = _func_src(DB, "get_taken_usernames")
    assert "except Exception" in src
    assert "return taken" in src


def test_no_username_flag_reaches_generator():
    """Кнопка «Без username» обязана доходить до генератора отдельным флагом.

    Регресс: пустой username_pattern означает «возьми шаблоны из библиотеки»,
    поэтому без явного флага отказ пользователя молча превращался бы в выдачу
    библиотечных имён.
    """
    gen = _func_src(HANDLER, "_generate_targets")
    assert "assign_usernames" in gen and "no_username" in gen
    skip = _func_src(HANDLER, "cb_gp_skip_uname")
    assert "no_username=True" in skip
    accept = _func_src(HANDLER, "cb_gp_accept_uname")
    assert "no_username=False" in accept
