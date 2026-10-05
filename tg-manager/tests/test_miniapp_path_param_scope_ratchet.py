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

# Признанные проверки доступа по id из ПУТИ. Хендлер, который зовёт такую
# проверку и отказывает по её результату, безопасен даже когда в его собственном
# SQL колонки владения нет: owner-скоуп живёт внутри проверки.
#
# Так выглядит `bot_auto_replies`: `_user_can_use_bot(pool, uid, bot_id)` →
# 404, а дальше запрос по bot_id. До свода четырёх дословных копий в один
# помощник скоуп стоял в самом SQL, и храповик его видел; после свода — покраснел
# на правильной правке. Признаём проверку, а не требуем копировать условие в
# каждый запрос.
#
# Список короткий НАМЕРЕННО, каждая запись разобрана поимённо. Добавлять сюда
# можно только fail-closed проверку: ошибка базы обязана ЗАКРЫВАТЬ доступ
# (у `_user_can_use_bot` это `_safe_count`, отдающий 0). Проверка, которая при
# сбое пускает, превратила бы храповик в разрешение на дыру.
#
# Разобранные поимённо (каждая — fail-closed, проверено в
# test_every_gate_really_scopes ниже):
#   _own_funnel      — SELECT ... FROM auto_funnels WHERE id=$1 AND owner_id=$2
#                      через _safe_fetchrow: ошибка базы даёт None → 404.
#   _own_mesh        — то же по content_meshes.
#   _own_experiment  — JOIN managed_bots b ON b.bot_id=e.bot_id AND b.added_by=$2
#                      (у experiments своей колонки владельца нет).
#   get_workspace_role — роль участника из workspace_members; при сбое базы
#                      бросает, хендлер ловит и отдаёт 500, то есть не пускает.
_ACCESS_GATES = ("_user_can_use_bot", "_own_funnel", "_own_mesh",
                 "_own_experiment", "get_workspace_role")

# Чем проверка признаётся настоящей: условием по владельцу или по участию.
_GATE_PROOF = {
    # Условие видимости вынесено в константу _BOTS_VISIBLE_SQL; её содержимое
    # проверяется отдельно, ниже в том же тесте.
    "_user_can_use_bot": ("_BOTS_VISIBLE_SQL",),
    "_own_funnel": ("owner_id",),
    "_own_mesh": ("owner_id",),
    "_own_experiment": ("added_by",),
    "get_workspace_role": ("workspace_members",),
}


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


def _calls_access_gate(fn: ast.AST) -> bool:
    """Хендлер проверяет доступ к id из пути признанной проверкой."""
    for n in ast.walk(fn):
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        name = f.attr if isinstance(f, ast.Attribute) else (
            f.id if isinstance(f, ast.Name) else "")
        if name in _ACCESS_GATES:
            return True
    return False


def _unscoped(src: str) -> list[str]:
    tree = ast.parse(src)
    gated = {fn.name for fn in ast.walk(tree)
             if isinstance(fn, (ast.AsyncFunctionDef, ast.FunctionDef))
             and _calls_access_gate(fn)}
    return [name for name, stmts in _handlers_with_path_param(src)
            if name not in gated and not any(_OWNER.search(s) for s in stmts)]


# ── Сам храповик ───────────────────────────────────────────────────────────

_DB_SRC = (pathlib.Path(__file__).resolve().parent.parent
           / "database" / "db.py").read_text(encoding="utf-8")


def _calls_outside_try(gate: str, src: str) -> set[str]:
    """Хендлеры, зовущие проверку не под try (исключение уйдёт мимо отказа)."""
    tree = ast.parse(src)
    bad: set[str] = set()
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        guarded = set()
        for node in ast.walk(fn):
            if isinstance(node, ast.Try):
                for inner in ast.walk(node):
                    guarded.add(id(inner))
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else (
                f.id if isinstance(f, ast.Name) else "")
            if name == gate and id(node) not in guarded:
                bad.add(fn.name)
    return bad


def test_every_gate_really_scopes():
    """Список признанных проверок — не лазейка.

    Запись в _ACCESS_GATES снимает требование скоупа с ЦЕЛОГО хендлера, поэтому
    каждая обязана сама спрашивать владельца или участие. Если такую проверку
    однажды выпотрошат — здесь станет красным, а не тихо разрешит всё, что на
    неё сослалось.
    """
    db_src = _DB_SRC
    for gate in _ACCESS_GATES:
        body = _gate_source(gate, _SRC) or _gate_source(gate, db_src)
        assert body, f"проверка {gate} не найдена — список устарел"
        proof = _GATE_PROOF.get(gate, ())
        assert proof, f"для {gate} не записано, чем он скоупит"
        assert any(p in body for p in proof), (
            f"{gate} больше не спрашивает владельца или участие: {body[:160]}")
    # Условие видимости ботов — общее для списка и для проверки доступа.
    m = re.search(r"_BOTS_VISIBLE_SQL\s*=\s*(?:f?\"\"\"|f?[\"'])(.*?)(?:\"\"\"|[\"'])",
                  _SRC, re.DOTALL)
    assert m, "_BOTS_VISIBLE_SQL не найден"
    assert "added_by" in m.group(1), (
        "видимость ботов больше не привязана к владельцу: " + m.group(1)[:200])


def _gate_source(name: str, src: str) -> str:
    """Текст функции целиком — по разбору, а не по отступам.

    Отступы здесь обманывают: у проверки с многострочной сигнатурой первая
    строка тела начинается не там, где кажется, и срез по отступу обрывал
    функцию раньше её условий.
    """
    tree = ast.parse(src)
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.AsyncFunctionDef, ast.FunctionDef)) and fn.name == name:
            return ast.get_source_segment(src, fn) or ""
    return ""


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


# ── Сторожа признанных проверок доступа ───────────────────────────────────────

_GATED = """
async def gated(request):
    uid = _get_uid(request)
    bot_id = int(request.match_info["bot_id"])
    if not await _user_can_use_bot(pool, uid, bot_id):
        return _err("Бот не найден", 404)
    return await pool.fetch("SELECT id FROM auto_replies WHERE bot_id=$1", bot_id)
"""


def test_a_recognised_access_gate_counts_as_scope():
    assert _unscoped(_GATED) == [], (
        "проверка доступа не признана — пришлось бы копировать условие владения "
        "в каждый запрос, а это те самые четыре копии, которые уже разъезжались")


def test_an_unknown_gate_does_not_count():
    """Иначе достаточно было бы завести функцию с похожим именем."""
    fake = _GATED.replace("_user_can_use_bot", "_looks_like_a_check")
    assert _unscoped(fake) == ["gated"]


def test_every_recognised_gate_exists_and_is_fail_closed():
    """Проверка из списка обязана существовать и закрываться при сбое базы.

    `_safe_count` отдаёт 0 при ошибке, то есть доступ закрывается. Проверка,
    которая при сбое пускает, сделала бы запись в списке разрешением на дыру.
    """
    # Помощники _safe_* ловят ошибку базы и отдают 0 / None — хендлер по такому
    # ответу ОТКАЗЫВАЕТ. Это и есть fail-closed.
    _FAIL_CLOSED = ("_safe_count(", "_safe_fetchval(", "_safe_fetchrow(")
    for gate in _ACCESS_GATES:
        if f"async def {gate}(" in _SRC:
            # Границы функции — по разбору: срез фиксированной длины рвёт
            # длинную проверку и молча выключает утверждение ниже.
            body = _gate_source(gate, _SRC)
            assert body, f"{gate} в списке, но в коде нет"
            assert any(h in body for h in _FAIL_CLOSED), (
                f"{gate} не пользуется fail-closed помощником — при ошибке базы она "
                f"может ОТКРЫТЬ доступ, и признавать её нельзя")
            continue
        # Проверка из database/db.py (get_workspace_role): она ошибку базы НЕ
        # глотает, а бросает. Это тоже закрывает доступ, но только пока каждый
        # её вызов стоит внутри try — иначе исключение улетит наверх уже за
        # пределами хендлера. Поэтому проверяем оба условия.
        body = _gate_source(gate, _DB_SRC)
        assert body, f"{gate} в списке, но в коде нет"
        assert "except" not in body, (
            f"{gate} глотает ошибку базы — при сбое может ОТКРЫТЬ доступ")
        unguarded = _calls_outside_try(gate, _SRC)
        assert not unguarded, (
            f"{gate} вызывается вне try в: {sorted(unguarded)} — при сбое базы "
            f"ответ не превратится в отказ")
