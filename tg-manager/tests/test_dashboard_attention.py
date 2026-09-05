"""Дашборд перестал быть витриной только для чтения.

Продукт копит подробную картину своего состояния — упавшие операции, мёртвые
прокси, карантин аккаунтов, молчащие боты, аккаунты без прокси, — но до сих пор
всё это либо лежало по разным экранам, либо не показывалось вовсе. Так,
`/api/miniapp/dashboard_realtime` с самого начала возвращал `account_health`,
`seo` и `geo`, а экран их не рисовал: данные приходили и выбрасывались.

Здесь проверяется, что:
  * сводка «что требует внимания» собирается из РЕАЛЬНЫХ таблиц;
  * у каждой карточки есть действие, и оно ведёт в СУЩЕСТВУЮЩИЙ эндпоинт —
    дашборд остаётся витриной с кнопками, а не вторым движком операций;
  * переходы на экраны идут по явному списку, а не по имени функции из ответа
    сервера (иначе переименование экрана даёт мёртвую кнопку);
  * чистая установка не показывает ложных тревог.
"""
from __future__ import annotations

import ast
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
API = (ROOT / "services" / "mini_app_api.py").read_text(encoding="utf-8")
DASH = (ROOT / "mini_app" / "screens" / "dashboard.js").read_text(encoding="utf-8")


def _attention_source() -> str:
    """Тело обработчика сводки."""
    tree = ast.parse(API)
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "dashboard_attention":
            return ast.unparse(node)
    raise AssertionError("обработчик dashboard_attention не найден")


def _registered_routes() -> set[str]:
    return set(re.findall(r'app\.router\.add_(?:get|post|put|delete|patch)\("([^"]+)"', API))


def test_endpoint_is_registered():
    assert "/api/miniapp/dashboard/attention" in _registered_routes()


def test_route_registered_after_handler_is_defined():
    """Регистрация раньше определения роняет сборку ВСЕГО сервера
    (UnboundLocalError) — так однажды лёг мини-апп целиком."""
    hdef = API.index("async def dashboard_attention")
    hreg = API.index('add_get("/api/miniapp/dashboard/attention"')
    assert hdef < hreg, "маршрут регистрируется раньше, чем определён обработчик"


def test_summary_reads_real_tables():
    """Сводка обязана считать по живым таблицам, а не по выдуманным числам."""
    src = _attention_source()
    for table in ("operation_queue", "user_proxies", "tg_accounts",
                  "managed_bots", "account_rehab_state", "managed_channels"):
        assert table in src, f"состояние {table} в сводке не учитывается"


def test_every_action_points_at_existing_endpoint():
    """Кнопка, ведущая в несуществующий маршрут, — та самая мёртвая кнопка."""
    src = _attention_source()
    routes = _registered_routes()
    used = set(re.findall(r"'(/api/miniapp/[a-z_/{}]+)'", src)) | \
           set(re.findall(r'"(/api/miniapp/[a-z_/{}]+)"', src))
    # f-строка с op_id разворачивается в шаблон маршрута
    normalised = {re.sub(r"\{[^}]+\}", "{op_id}", u) for u in used}
    missing = [u for u in normalised if u not in routes]
    assert not missing, f"действия ведут в несуществующие эндпоинты: {missing}"


def test_actions_do_not_invent_new_mass_operations():
    """Дашборд не должен сам ставить операции в очередь мимо operation_bus."""
    src = _attention_source()
    assert "INSERT INTO operation_queue" not in src, (
        "сводка ставит операцию напрямую — это обход тарифа, предохранителя и дедупа"
    )


def test_screen_jumps_use_allowlist():
    """Переходы — по ключам из явного списка на фронте, а не по имени функции."""
    src = _attention_source()
    # ast.unparse нормализует кавычки в одинарные — проверка не должна от них зависеть
    keys = set(re.findall(r"""['"]screen['"]:\s*['"]([a-z_]+)['"]""", src))
    assert keys, "переходов на экраны нет вовсе"
    allow = re.search(r"const _UD_SCREENS = \{(.*?)\n\};", DASH, re.S)
    assert allow, "на фронте нет списка допустимых переходов"
    known = set(re.findall(r"(\w+):\s*\(\)", allow.group(1)))
    assert keys <= known, f"переход без обработчика на фронте: {keys - known}"


def test_frontend_has_attention_tab_and_calls_endpoint():
    assert "'attention'" in DASH and "Требует внимания" in DASH
    assert "/api/miniapp/dashboard/attention" in DASH
    assert "async function _udAct" in DASH, "кнопки ничего не выполняют"


def test_dashboard_opens_on_problems_first():
    """Оператор должен видеть проблемы сразу, а не после переключения вкладки."""
    m = re.search(r"async function openUnifiedDashboard\(\)\s*\{(.*?)\n\}", DASH, re.S)
    assert m and "_udTabCur = 'attention'" in m.group(1)


def test_analytics_renders_data_it_already_receives():
    """account_health / seo / geo приходили в ответе и не рисовались."""
    m = re.search(r"function _udRenderAnalytics\(d\)\s*\{(.*?)\n\}", DASH, re.S)
    assert m, "рендер аналитики не найден"
    body = m.group(1)
    for key in ("account_health", "seo", "geo"):
        assert f"d.{key}" in body, f"{key} по-прежнему выбрасывается"


def test_clean_install_shows_nothing():
    """На пустой базе список обязан быть пустым: ложная тревога на чистой
    установке приучает оператора не смотреть на экран."""
    src = _attention_source()
    # каждый блок добавляет карточку только под условием непустого счётчика
    assert re.search(r"if n:\s", src) or "if dead:" in src
    assert "items.sort" in src and "len(items)" in src
