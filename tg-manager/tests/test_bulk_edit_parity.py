"""Регресс: массовое редактирование каналов/ботов/профилей — паритет с ботом.

Три операции разом: bulk_edit_channels, bulk_bot_edit, bulk_update_profile.

Главный инвариант — БЕЛЫЙ СПИСОК ПОЛЕЙ, взятый из самих исполнителей. Принять
поле, которого исполнитель не знает, значит устроить «тихий успех»: операция
поставится, отработает и отчитается, а эффекта не будет — ровно тот класс, что
уже ловился в этой сессии (нулевые KPI, игнорируемые параметры).
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
API = ROOT / "services" / "mini_app_api.py"
WORKER = ROOT / "services" / "op_worker.py"
INDEX = ROOT / "mini_app" / "index.html"


def _handler() -> str:
    src = API.read_text(encoding="utf-8")
    m = re.search(r"    async def _bulk_edit_op\b.*?(?=\n    async def )", src, re.DOTALL)
    assert m, "_bulk_edit_op не найден"
    return m.group(0)


def _allowed_map() -> dict[str, set[str]]:
    src = API.read_text(encoding="utf-8")
    m = re.search(r"_BULK_EDIT_FIELDS\s*=\s*\{(.*?)\n    \}", src, re.DOTALL)
    assert m, "_BULK_EDIT_FIELDS не найден"
    out: dict[str, set[str]] = {}
    for km in re.finditer(r'"(\w+)":\s*\{([^}]*)\}', m.group(1)):
        out[km.group(1)] = set(re.findall(r'"(\w+)"', km.group(2)))
    return out


def test_all_three_ops_wired():
    h = _handler()
    for op in ("bulk_edit_channels", "bulk_bot_edit", "bulk_update_profile"):
        assert op in h, f"{op} не подключён"


def test_routes_registered():
    src = API.read_text(encoding="utf-8")
    for route in ("/api/miniapp/channels/bulk_edit",
                  "/api/miniapp/bots/bulk_edit",
                  "/api/miniapp/accounts/bulk_profile"):
        assert f'"{route}"' in src, f"роут {route} не зарегистрирован"


def test_field_whitelist_matches_executors():
    """Белый список не должен разъезжаться с тем, что реально умеют исполнители."""
    allowed = _allowed_map()
    worker = WORKER.read_text(encoding="utf-8")

    ch = re.search(r"async def _exec_bulk_edit_channels\(.*?(?=\nasync def )", worker, re.DOTALL)
    assert ch and set(re.findall(r'field\s*==\s*"(\w+)"', ch.group(0))) <= allowed["channels"], (
        "исполнитель каналов умеет поле, которого нет в белом списке"
    )
    bt = re.search(r"async def _exec_bulk_bot_edit\(.*?(?=\nasync def )", worker, re.DOTALL)
    assert bt and set(re.findall(r'field\s*==\s*"(\w+)"', bt.group(0))) <= allowed["bots"]


def test_unknown_field_rejected_with_400():
    h = _handler()
    assert "if field not in allowed" in h and "400" in h, (
        "поле вне белого списка обязано отклоняться, иначе «тихий успех»"
    )


def test_accounts_scoped_and_alive():
    h = _handler()
    assert "owner_id=$1 AND is_active" in h and "session_str IS NOT NULL" in h, (
        "аккаунты берём только свои и живые"
    )


def test_bots_not_enumerated_like_in_bot():
    h = _handler()
    assert "FROM managed_bots WHERE added_by=$1" in h, (
        "контракт бота: bulk_bot_edit берёт всех своих ботов, список не передаётся"
    )


def test_frontend_field_lists_match_backend():
    """UI не должен предлагать поле, которое бэкенд отвергнет."""
    html = INDEX.read_text(encoding="utf-8")
    m = re.search(r"const BE_FIELDS\s*=\s*\{(.*?)\n\};", html, re.DOTALL)
    assert m, "BE_FIELDS не найден"
    block = m.group(1)
    allowed = _allowed_map()
    lines = {ln.strip().split(":", 1)[0].strip(): ln
             for ln in block.splitlines() if ":" in ln}
    for target in ("channels", "bots", "profile"):
        assert target in lines, f"в BE_FIELDS нет {target}"
        ui_fields = set(re.findall(r"\['(\w+)'", lines[target]))
        extra = ui_fields - allowed[target]
        assert not extra, f"UI предлагает поля, которых бэкенд не примет: {extra}"


def test_username_coordination_warning_shown():
    """username автонумеруется у всех аккаунтов — это координационная сигнатура,
    пользователь должен знать об этом до запуска."""
    html = INDEX.read_text(encoding="utf-8")
    assert "'profile:username'" in html, "нет подсказки для username"
    m = re.search(r"'profile:username':\s*'([^']+)'", html)
    assert m and "сигнатура" in m.group(1), "предупреждение должно объяснять риск"


def test_confirm_before_applying():
    html = INDEX.read_text(encoding="utf-8")
    m = re.search(r"async function submitBulkEdit\(\)\s*\{.*?\n\}", html, re.DOTALL)
    assert m and "askConfirm" in m.group(0), "массовое изменение требует подтверждения"
    assert "pollOpResult" in m.group(0), "итог операции должен доводиться до пользователя"
