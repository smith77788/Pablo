"""Каждый запрос, берущий id из адреса, обязан брать и владельца.

НАХОДКА, из которой вырос этот тест (23.10.2026). У ленты карточки
(`/uch/contacts/{id}/timeline`) и графа идентичности (`/identity`) ЧАСТЬ
запросов владельца брала, а часть — нет: `contact_history` и
`organism_events` с owner_id, а `contact_sources WHERE contact_id=$1` и
`contact_crm WHERE contact_id=$1` — без него вообще. По чужому uuid отдавались
аккаунты-источники, стадия CRM и «Был сегодня».

Грубая проверка «uid встречается где-нибудь в ручке» такую дыру НЕ ловит —
именно поэтому она и прожила: uid в ручке был, просто не во всех запросах.
Поэтому здесь проверяется КАЖДЫЙ вызов отдельно.

ЧТО СЧИТАЕТСЯ ЗАЩИТОЙ. Два законных способа:
  1. сам запрос берёт и id, и uid (`WHERE id=$1 AND owner_id=$2`);
  2. ВЫШЕ в той же ручке стоит «калитка»: запрос с id и uid, результат которого
     проверяется и при пустом ответе ручка СРАЗУ возвращает отказ
     (`if not row: return _err(...)`). После такой калитки чужой id до
     остальных запросов не доходит — так устроены `repository.get_contact` и
     `ecosystem_detail`.
Проверка в обработчике БЕЗ раннего возврата защитой не считается: именно так
выглядела лента карточки, где непустой ответ по чужому контакту собирался из
запросов, которые владельца не спрашивали.

ТРЕТИЙ ЗАКОННЫЙ СПОСОБ — сравнение: запрос ДОСТАЁТ владельца по id
(`owner = await ...WHERE id=$1`), и ручка сверяет его с собой
(`if owner != uid: return 403`). Так закрыты телеметрия аккаунта и
семантическая память бота. Сам запрос калитки владельца не берёт — он его
ищет, поэтому из проверки исключается по имени, в которое пишет.

ЧЕТВЁРТЫЙ — ручка владельца платформы: у `admin_user_detail` своего скоупа нет
по замыслу (ранний 403 не-админу), а межтенантный срез аккаунтов включается
только явным `?scope=platform` через `_wants_platform_scope`, который сам
проверяет админство и по умолчанию отвечает False.

ПРОБНИК. Измеритель обязан видеть заведомо плохой пример и не трогать
заведомо хорошие — иначе однажды он начнёт «проходить» просто потому, что
сломался. Пробники ниже проверяются тем же кодом, что и продукт.

КАК ЭТОТ ИЗМЕРИТЕЛЬ ПИСАЛСЯ (и почему это важнее его текста). Первая версия
дала 0 находок: она спрашивала «встречается ли uid где-нибудь в ручке» — то
есть пропустила бы обе настоящие дыры. Вторая, построже, дала 28 — и ни одна
не была дырой: калитки стояли, но в формах, которых измеритель не знал
(`_safe_fetchrow` вместо `pool.fetchrow`, помощник `_user_can_use_bot`,
`if not await _own_contact(...)` без присваивания, сравнение владельца, ручка
админа). Каждую форму пришлось прочитать глазами и добавить в измеритель, а не
«починить» по его списку. Детектор, дающий десятки находок в зрелом коде,
почти всегда сломан — и ровно поэтому к нему приложены пробники.

ОБРАТНЫЙ КОНТРОЛЬ (прогонялся): убрал калитку `_own_contact` из uch_timeline
-> красный test_no_query_reads_by_an_url_id_without_the_owner с обеими
строками (contact_sources и contact_crm).
"""
from __future__ import annotations

import ast
import os
import re

# Запросы идут и напрямую (`pool.fetch(...)`), и через помощников
# (`_safe_fetchrow(pool, sql, ...)`), которые глотают сбой схемы. Измеритель,
# знающий только первый вид, объявил «без владельца» 28 запросов, из которых
# НИ ОДИН таким не был: калитки стояли, просто в другой форме. Детектор,
# дающий десятки находок в зрелом коде, почти всегда сломан — поэтому формы
# ниже перечислены обе, а правильность проверяется пробниками.
CALLS = {"fetch", "fetchrow", "fetchval", "execute"}
HELPERS = {"_safe_fetch", "_safe_fetchrow", "_safe_fetchval", "_safe_count"}


def _is_query(call: ast.Call) -> bool:
    if isinstance(call.func, ast.Attribute) and call.func.attr in CALLS:
        return True
    return isinstance(call.func, ast.Name) and call.func.id in HELPERS


def _id_vars(seg: str) -> set[str]:
    return {m.group(1) for m in
            re.finditer(r"(\w+)\s*=\s*(?:int\()?\s*request\.match_info(?:\.get)?[\[\(]", seg)}


def _uid_vars(seg: str) -> set[str]:
    out = {m.group(1) for m in re.finditer(r"(\w+)\s*=\s*_get_uid\(", seg)}
    return out or {"uid"}


def _gate_line(fn: ast.AST, ids: set[str], uids: set[str],
               owner_names: set[str] | None = None) -> int | None:
    """Строка «калитки»: ЛЮБОЙ вызов с id и uid, после которого ранний возврат.

    Калиткой может быть и запрос (`... WHERE id=$1 AND owner_id=$2`), и
    помощник (`_user_can_use_bot(pool, uid, bot_id)`, `get_contact(pool, cid,
    uid)`): важно не то, чем проверено владение, а то, что при чужом id ручка
    возвращает отказ ДО остальных запросов.
    """
    gates = []
    owner_names = owner_names if owner_names is not None else set()
    for node in ast.walk(fn):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        value = node.value
        if isinstance(value, ast.Await):
            value = value.value
        if not isinstance(value, ast.Call):
            continue
        names = {a.id for a in value.args if isinstance(a, ast.Name)}
        if not ((names & ids) and (names & uids)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        held = {t.id for t in targets if isinstance(t, ast.Name)}
        for check in ast.walk(fn):
            if not isinstance(check, ast.If):
                continue
            test = check.test
            tested = set()
            if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
                if isinstance(test.operand, ast.Name):
                    tested.add(test.operand.id)
            elif isinstance(test, ast.Name):
                tested.add(test.id)
            if not (tested & held):
                continue
            if any(isinstance(b, ast.Return) for b in ast.walk(check)):
                gates.append(node.lineno)
                break
    # Калитка без присваивания: `if not await _own_contact(cid, uid): return ...`
    # — самая короткая и самая частая форма, и без неё измеритель объявлял
    # находкой ровно ту ручку, где дыру и закрыли.
    for check in ast.walk(fn):
        if not isinstance(check, ast.If):
            continue
        if not any(isinstance(b, ast.Return) for b in check.body):
            continue
        for call in ast.walk(check.test):
            if not isinstance(call, ast.Call):
                continue
            names = {a.id for a in call.args if isinstance(a, ast.Name)}
            if (names & ids) and (names & uids):
                gates.append(check.lineno)
                break
    # Форма «сверить владельца с собой»: `owner = await ...(id)` и затем
    # `if owner != uid: return 403`. Сам запрос владельца не берёт — он его
    # ДОСТАЁТ, и калиткой служит сравнение. Так закрыты телеметрия аккаунта и
    # семантическая память бота.
    for check in ast.walk(fn):
        if not isinstance(check, ast.If) or not isinstance(check.test, ast.Compare):
            continue
        if not any(isinstance(b, ast.Return) for b in check.body):
            continue
        parts = {n.id for n in ast.walk(check.test) if isinstance(n, ast.Name)}
        if parts & uids and len(parts) > 1:
            gates.append(check.lineno)
            owner_names.update(parts - uids)
    return min(gates) if gates else None


def _admin_gated(fn: ast.AST, src_fn: str) -> bool:
    """Ручка платформенного владельца: своего скоупа у неё нет по замыслу.

    Два вида: ранний отказ не-админу (`if not _is_admin(uid): return 403`) и
    ЯВНЫЙ опт-ин в межтенантный срез (`_wants_platform_scope`), который сам
    проверяет админство и по умолчанию отдаёт False.
    """
    if "_wants_platform_scope" in src_fn:
        return True
    for check in ast.walk(fn):
        if not isinstance(check, ast.If):
            continue
        if not any(isinstance(b, ast.Return) for b in check.body):
            continue
        for call in ast.walk(check.test):
            if (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                    and call.func.id == "_is_admin"):
                return True
    return False


def unscoped_calls(path: str) -> list[tuple[str, int, str]]:
    src = open(path, encoding="utf-8").read()
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    bad = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        seg = ast.get_source_segment(src, fn) or ""
        if "match_info" not in seg or "_get_uid" not in seg:
            continue
        ids, uids = _id_vars(seg), _uid_vars(seg)
        if not ids:
            continue
        if _admin_gated(fn, seg):
            continue
        # Запрос самой калитки («достать владельца по id») владельца и не
        # берёт — он его ищет. Его исключаем по имени, в которое он пишет.
        owner_names: set[str] = set()
        gate = _gate_line(fn, ids, uids, owner_names)
        gate_lines = set()
        for node in ast.walk(fn):
            if isinstance(node, ast.Assign) and any(
                    isinstance(x, ast.Name) and x.id in owner_names for x in node.targets):
                value = node.value.value if isinstance(node.value, ast.Await) else node.value
                if isinstance(value, ast.Call):
                    gate_lines.add(value.lineno)
        for call in ast.walk(fn):
            if not (isinstance(call, ast.Call) and _is_query(call)):
                continue
            names = {a.id for a in call.args if isinstance(a, ast.Name)}
            if not (names & ids) or (names & uids):
                continue
            if call.lineno in gate_lines:
                continue
            if gate is not None and call.lineno > gate:
                continue
            bad.append((path, call.lineno, fn.name))
    return bad


_PROBE = '''
async def probe_part_of_the_queries(request):
    """Так выглядела лента карточки: проверка есть, раннего возврата нет."""
    uid = _get_uid(request)
    cid = request.match_info['contact_id']
    history = await pool.fetch('SELECT 1 FROM h WHERE id=$1 AND owner_id=$2', cid, uid)
    srcs = await pool.fetch('SELECT 1 FROM contact_sources WHERE contact_id=$1', cid)
    return history, srcs


async def probe_no_owner_at_all(request):
    uid = _get_uid(request)
    cid = request.match_info['contact_id']
    return await pool.fetchrow('SELECT 1 FROM t WHERE id=$1', cid)


async def probe_scoped_in_the_query(request):
    uid = _get_uid(request)
    cid = request.match_info['contact_id']
    return await pool.fetchrow('SELECT 1 FROM t WHERE id=$1 AND owner_id=$2', cid, uid)


async def probe_gate_by_comparing_the_owner(request):
    """Калитка сравнением: запрос достаёт владельца, ручка сверяет его с собой."""
    uid = _get_uid(request)
    account_id = int(request.match_info['account_id'])
    owner = await pool.fetchval('SELECT owner_id FROM tg_accounts WHERE id=$1', account_id)
    if owner != uid:
        return _err("Это не ваш ресурс", 403)
    return await pool.fetch('SELECT 1 FROM telemetry WHERE account_id=$1', account_id)


async def probe_admin_only(request):
    """Ручка владельца платформы: своего скоупа у неё нет по замыслу."""
    uid = _get_uid(request)
    if not uid or not _is_admin(uid):
        return _err("Раздел только для владельца платформы", 403)
    target_id = int(request.match_info['user_id'])
    return await pool.fetchrow('SELECT * FROM platform_users WHERE user_id=$1', target_id)


async def probe_gate_inline_in_the_if(request):
    """Калитка прямо в условии — так закрыта лента карточки."""
    uid = _get_uid(request)
    cid = request.match_info['contact_id']
    if not await _own_contact(cid, uid):
        return _err("Контакт не найден", 404)
    return await pool.fetch('SELECT 1 FROM contact_sources WHERE contact_id=$1', cid)


async def probe_gate_through_a_helper(request):
    """Калитка помощником: так устроены ручки ботов и воронок."""
    uid = _get_uid(request)
    bot_id = int(request.match_info['bot_id'])
    owns = await _user_can_use_bot(pool, uid, bot_id)
    if not owns:
        return _err("Бот не найден", 404)
    return await _safe_count(pool, 'SELECT COUNT(*) FROM t WHERE bot_id=$1', bot_id)


async def probe_helper_query_without_owner(request):
    """Тот же помощник, но калитки нет — это находка."""
    uid = _get_uid(request)
    bot_id = int(request.match_info['bot_id'])
    return await _safe_count(pool, 'SELECT COUNT(*) FROM t WHERE bot_id=$1', bot_id)


async def probe_gate_then_free_reads(request):
    """Законный способ: калитка с ранним возвратом, дальше чужой id не дойдёт."""
    uid = _get_uid(request)
    cid = request.match_info['contact_id']
    row = await pool.fetchrow('SELECT 1 FROM t WHERE id=$1 AND owner_id=$2', cid, uid)
    if not row:
        return _err("Не найдено", 404)
    members = await pool.fetch('SELECT 1 FROM members WHERE parent=$1', cid)
    return members
'''


def test_the_probe_sees_a_bad_handler_and_spares_the_good_ones(tmp_path):
    p = tmp_path / "probe.py"
    p.write_text(_PROBE, encoding="utf-8")
    got = {name for _, _, name in unscoped_calls(str(p))}
    assert got == {"probe_part_of_the_queries", "probe_no_owner_at_all",
                   "probe_helper_query_without_owner"}, (
        f"измеритель видит не то: {sorted(got)}")


def test_no_query_reads_by_an_url_id_without_the_owner():
    bad = []
    for f in sorted(os.listdir("services")):
        if f.endswith(".py"):
            bad += unscoped_calls(os.path.join("services", f))
    assert not bad, (
        "запрос берёт id из адреса и не берёт владельца:\n  "
        + "\n  ".join(f"{p}:{ln} ({fn})" for p, ln, fn in bad)
        + "\nЛибо допишите owner_id в сам запрос, либо поставьте выше калитку "
          "с ранним возвратом.")
