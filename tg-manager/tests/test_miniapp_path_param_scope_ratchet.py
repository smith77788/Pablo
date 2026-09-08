"""Храповик: хендлер, берущий id из ПУТИ, обязан скоупить запрос по владельцу.

Самый дорогой класс бага мультиарендного продукта. `/api/miniapp/account/{id}`
без `owner_id` — это не «неточность выборки», а выдача чужого аккаунта любому
авторизованному пользователю, который подставит соседний id. Репозиторий это
уже переживал дважды: `/team/members` отдавал весь `platform_users`, а `/audit` —
операции всех владельцев (см. `tests/test_miniapp_tenant_scope.py`). Те две дыры
закрыты точечными тестами; здесь замораживается ВЕСЬ класс — 139 хендлеров.

**Что проверяется.** Функция попадает под проверку, если она одновременно:
читает `request.match_info` (значит, id пришёл извне и не выведен из uid),
делает вызов в БД и содержит SQL-литерал. Такой SQL обязан упоминать колонку
владения (`owner_id`, `added_by`, `created_by`, `user_id`) или `uid`.

**Чего проверка НЕ ловит** (осознанно, чтобы не давать ложных срабатываний):
хендлеры, делегирующие выборку в сервис (`await _nb.get_instance_detail(pool,
uid, nid)`) — SQL в них нет, скоуп обеспечивает сервис. Расширять сюда нельзя
без отдельного разбора: измеритель, дающий десятки находок в зрелом коде, почти
всегда сломан (CLAUDE.md), а этот на момент написания даёт ровно ноль.

Проверка идёт по границам функций из AST, а не по окну фиксированной длины:
сдвинулся код — защита не должна выключаться молча.
"""
from __future__ import annotations

import ast
import pathlib
import re

_SRC = (pathlib.Path(__file__).resolve().parent.parent
        / "services" / "mini_app_api.py").read_text(encoding="utf-8")

_DB_CALL = re.compile(
    r"\b(?:fetch|fetchval|fetchrow|execute|executemany"
    r"|_safe_fetch|_safe_count|_safe_fetchrow|_safe_fetchval)\s*\(")
_SQL_VERB = re.compile(r"\b(SELECT|UPDATE|DELETE\s+FROM|INSERT\s+INTO)\b", re.I)
_OWNER = re.compile(r"\b(owner_id|added_by|created_by|user_id|uid)\b", re.I)


def _sql_literals(fn: ast.AST) -> list[str]:
    """Все SQL-строки функции — в любых кавычках, включая f-строки (у f-строк
    берём только постоянные куски: имена таблиц и условий живут там)."""
    out: list[str] = []
    for n in ast.walk(fn):
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            if _SQL_VERB.search(n.value):
                out.append(n.value)
        elif isinstance(n, ast.JoinedStr):
            parts = [v.value for v in n.values
                     if isinstance(v, ast.Constant) and isinstance(v.value, str)]
            joined = " ".join(parts)
            if _SQL_VERB.search(joined):
                out.append(joined)
    return out


def _reads_path_param(fn: ast.AST) -> bool:
    """`request.match_info[...]` где-то внутри функции — по узлам, а не по
    тексту: `ast.get_source_segment` пересобирает весь 18-тысячестрочный файл
    на каждую из ~700 функций и превращает проверку в минуты."""
    return any(isinstance(n, ast.Attribute) and n.attr == "match_info"
               for n in ast.walk(fn))


def _calls_db(fn: ast.AST) -> bool:
    for n in ast.walk(fn):
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        name = f.attr if isinstance(f, ast.Attribute) else (
            f.id if isinstance(f, ast.Name) else "")
        if name and _DB_CALL.match(name + "("):
            return True
    return False


def _handlers_with_path_param(src: str):
    """(имя, SQL-литералы) для функций «id из пути + поход в БД»."""
    tree = ast.parse(src)
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        if not _reads_path_param(fn) or not _calls_db(fn):
            continue
        stmts = _sql_literals(fn)
        if stmts:
            yield fn.name, stmts


def _unscoped(src: str) -> list[str]:
    return [name for name, stmts in _handlers_with_path_param(src)
            if not any(_OWNER.search(s) for s in stmts)]


# ── Сам храповик ───────────────────────────────────────────────────────────

def test_every_path_param_handler_is_owner_scoped():
    leaks = _unscoped(_SRC)
    assert not leaks, (
        "хендлеры берут id из пути и ходят в БД без скоупа по владельцу — "
        f"подстановка чужого id вернёт чужие данные: {sorted(leaks)}"
    )


def test_the_ratchet_actually_covers_the_surface():
    """Если фильтр однажды перестанет находить хендлеры, тест выше станет
    зелёным всегда и защита выключится молча."""
    n = sum(1 for _ in _handlers_with_path_param(_SRC))
    assert n >= 120, f"проверка охватывает лишь {n} хендлеров — фильтр сломался"


# ── Проверка самого измерителя ─────────────────────────────────────────────
# «Детектор, дающий десятки находок в зрелом коде, почти всегда сломан»
# (CLAUDE.md). Убеждаемся на заведомых примерах, что он видит дыру и не
# оговаривает здоровый код — иначе зелёный храповик ничего не значит.

_LEAKY = '''
async def leaky(request):
    aid = int(request.match_info["acc_id"])
    return await pool.fetchrow("SELECT phone, session_str FROM tg_accounts WHERE id=$1", aid)
'''

_SCOPED_DOUBLE = '''
async def ok1(request):
    uid = _get_uid(request)
    aid = int(request.match_info["acc_id"])
    return await pool.fetchrow(
        "SELECT phone FROM tg_accounts WHERE id=$1 AND owner_id=$2", aid, uid)
'''

_SCOPED_SINGLE = """
async def ok2(request):
    uid = _get_uid(request)
    cid = request.match_info['contact_id']
    return await pool.fetch(
        'SELECT * FROM contact_history WHERE contact_id=$1 AND owner_id=$2', cid, uid)
"""

_SCOPED_CREATED_BY = '''
async def ok3(request):
    uid = _get_uid(request)
    sid = int(request.match_info["sch_id"])
    return await pool.fetchrow(
        """UPDATE scheduled_broadcasts SET status='cancelled'
           WHERE id=$1 AND created_by=$2 RETURNING id""", sid, uid)
'''


def test_probe_catches_a_real_leak():
    assert _unscoped(_LEAKY) == ["leaky"]


def test_probe_does_not_slander_scoped_handlers():
    """Три формы записи, все встречаются в файле: тройные кавычки, одинарные и
    колонка владения с другим именем."""
    for src in (_SCOPED_DOUBLE, _SCOPED_SINGLE, _SCOPED_CREATED_BY):
        assert _unscoped(src) == [], src.strip().splitlines()[1]


def test_probe_ignores_handlers_that_take_no_id_from_the_path():
    """Выборка по одному uid безопасна и под проверку попадать не должна."""
    src = '''
async def mine(request):
    uid = _get_uid(request)
    return await pool.fetch("SELECT id FROM tg_accounts WHERE owner_id=$1", uid)
'''
    assert list(_handlers_with_path_param(src)) == []
