"""Потолок списка может стоять не в запросе, а в чужой подписи.

Храповик `test_a_capped_list_says_it_is_capped.py` читает текст обработчиков и
находит `LIMIT 100` в SQL. Но обработчик может не содержать ни числа, ни SQL:

    items = await _lnk.list_for_owner(pool, uid)      # а там limit: int = 100

Так прошли мимо правила три места. Сто первая короткая ссылка не открывалась
ничем: ни выключить, ни удалить, ни посмотреть переходы — а переходы и есть то,
зачем ссылку сокращали. Вставка на восемьсот прокси добавляла пятьсот, экран
говорил «Добавлено: 500» и ОЧИЩАЛ поле — триста исчезали вместе с текстом, и
копии не оставалось нигде. Сто первый заказ менеджера не видел никто, включая
бухгалтерию: кнопка «Экспорт CSV» строила файл из той же первой страницы.

Это правило смотрит на ПОДПИСИ вызываемых функций. Находка — вызов из слоя
мини-аппа функции, у которой потолок стоит в значении по умолчанию, а свой
обработчик не передаёт. Каждое оставленное место названо в `_ALLOWED` с
причиной: «потолок» сам по себе не порок, порок — потолок, о котором владельцу
не говорят.

РАЗРЕШЕНИЕ ИМЁН. Собственная функция файла и чужая того же имени — разные
функции. Этот же скан, пока разрешал имена по совпадению, отнёс
`_str_list(data.get("usernames"))` из `dm_adhoc_send` к `channel_admin._str_list`
(потолок в ВОСЕМЬ) и объявил, что из любой разовой рассылки уходят первые восемь
адресатов. В `mini_app_api.py` есть свой `_str_list(value)` — без потолка вообще.
Поэтому имя разрешается сначала в своём файле, потом среди импортов, и только
так.

ПРОВЕРЯЕМЫЕ ПРИЧИНЫ. Первая версия `_ALLOWED` оправдывала `tg_cloud.list_files`
тем, что «с фронта /api/miniapp/cloud/list не зовут». Такого роута нет вовсе —
настоящий `/api/miniapp/cloud/files`, и экран облака его зовёт. То есть
разрешение держалось на угаданном пути, а проверка «двери нет» молчала бы
всегда. Теперь путь сверяется со снимком маршрутов, а «двери нет» — с
источником фронта: двести первый файл облака нашёлся именно так, уже после
того, как я записал его в безвредные.
"""
from __future__ import annotations

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]

_CAP_ARGS = ("limit", "max_rows", "page_size", "max_items", "top")

# Вызовы, у которых потолок по умолчанию оставлен сознательно. Ключ —
# «модуль.функция», значение — почему владелец от этого не страдает.
_ALLOWED: dict[str, str] = {
    "secret_masking.redact_secrets":
        "режет длину ТЕКСТА сообщения, а не число строк списка",
    "operation_bus.resubmit_unfinished":
        "действие (перезапуск незавершённых), а не список на экране",
    "dm_engine.parse_import_list":
        "разбор вставки; потолок 5000 выше, чем влезает в тело запроса",
    "content_memory.recent_pillars_for_owner":
        "служебный выбор следующей темы, на экран не попадает",
    "virtual_layer.entity_history":
        "лента виртуального админа — её ведёт другой тред",
    "virtual_layer.segment":
        "выборка виртуального слоя — её ведёт другой тред",
    "host_server.list_market":
        "витрина хост-сервера: экрана нет, решение о ней за владельцем",
    "provider_marketplace.list_services":
        "внешний API провайдеров по ключу, не экран владельца",
    "provider_marketplace.list_providers":
        "внешний API провайдеров по ключу, не экран владельца",
    "geo_va_link.retry_missing":
        "действие (добить непроставленное), а не список",
    "mini_app_api._collect_valid_proxies":
        "размер ПАЧКИ: ответ отдаёт overflow, экран шлёт вставку по частям "
        "(tests/test_the_pasted_proxies_do_not_vanish.py)",
    "contact_invite_link.invite_history_for_contact":
        "история приглашений ОДНОМУ контакту: пятидесяти там не бывает",
    "account_warmer.normalize_warmup_channels":
        "список каналов прогрева: пятьдесят — осмысленный предел самой затеи",
    "workflow_engine.recent_runs":
        "карточка показывает «последние 5 запусков» по смыслу",
    "mass_inviter_engine.parse_user_refs":
        "разбор вставки, потолок 200000 — выше, чем влезает в тело запроса",
    "mass_inviter_engine.parse_phones":
        "разбор вставки, потолок 200000 — выше, чем влезает в тело запроса",
    "vault_service.export_data":
        "экспорт берёт limit+1 строк, считает total и пишет «Экспорт неполный: "
        "сохранены N из M» прямо в файл — потолок назван владельцу",
}

# Разрешения, которые держатся на том, что ручку с фронта не зовут. Записка от
# руки тут живёт дольше причины: экран появится — потолок вернётся молча.
# Поэтому каждая такая причина проверяема, а не прочитана.
_ALLOWED_NO_FRONT = {
    "host_server.list_market": "/api/miniapp/host_server/market",
}


def _module_ints(tree: ast.Module) -> dict[str, int]:
    """Целые константы модуля: потолок часто стоит именованным
    (`limit: int = PROXY_IMPORT_BATCH`), и по литералам его не видно."""
    out: dict[str, int] = {}
    for n in tree.body:
        if isinstance(n, ast.Assign) and isinstance(n.value, ast.Constant) \
                and isinstance(n.value.value, int):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    out[t.id] = n.value.value
        if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) \
                and isinstance(n.value, ast.Constant) \
                and isinstance(n.value.value, int):
            out[n.target.id] = n.value.value
    return out


def _cap_arg(fn: ast.AST, consts: dict[str, int] | None = None):
    """(имя параметра-потолка, его значение, позиция среди позиционных)."""
    consts = consts or {}
    a = fn.args
    pos = a.args[len(a.args) - len(a.defaults):] if a.defaults else []
    pairs = list(zip([x.arg for x in pos], a.defaults))
    pairs += [(x.arg, d) for x, d in zip(a.kwonlyargs, a.kw_defaults)
              if d is not None]
    for name, d in pairs:
        if isinstance(d, ast.Name) and d.id in consts:
            d = ast.Constant(value=consts[d.id])
        if (name in _CAP_ARGS and isinstance(d, ast.Constant)
                and isinstance(d.value, int) and d.value > 0):
            try:
                idx = [x.arg for x in a.args].index(name)
            except ValueError:
                idx = None
            return name, d.value, idx
    return None


def _capped_functions() -> dict[tuple[str, str], tuple[str, int, int | None]]:
    out: dict[tuple[str, str], tuple[str, int, int | None]] = {}
    for path in sorted((ROOT / "services").glob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        consts = _module_ints(tree)
        for fn in tree.body:          # только модульного уровня
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            cap = _cap_arg(fn, consts)
            if cap:
                out[(path.stem, fn.name)] = cap
    return out


def _findings(files=None) -> list[str]:
    caps = _capped_functions()
    bad: list[str] = []
    paths = files if files is not None else sorted(
        (ROOT / "services").glob("mini_app*.py"))
    for path in paths:
        src = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        modalias: dict[str, str] = {}
        funcsrc: dict[str, str] = {}
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                for a in n.names:
                    if a.name.startswith("services."):
                        modalias[a.asname or a.name.split(".")[-1]] = \
                            a.name.split(".")[-1]
            if isinstance(n, ast.ImportFrom) and n.module:
                if n.module == "services":
                    for a in n.names:
                        modalias[a.asname or a.name] = a.name
                elif n.module.startswith("services."):
                    stem = n.module.split(".")[-1]
                    for a in n.names:
                        funcsrc[a.asname or a.name] = stem
        own = {f.name for f in ast.walk(tree)
               if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))}
        for n in ast.walk(tree):
            if not isinstance(n, ast.Call):
                continue
            f = n.func
            if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
                stem = modalias.get(f.value.id)
                if not stem:
                    continue
                key = (stem, f.attr)
            elif isinstance(f, ast.Name):
                if f.id in own:        # своя функция файла — не чужая подпись
                    continue
                stem = funcsrc.get(f.id)
                if not stem:
                    continue
                key = (stem, f.id)
            else:
                continue
            if key not in caps:
                continue
            argname, default, idx = caps[key]
            if any(k.arg == argname for k in n.keywords):
                continue
            if idx is not None and len(n.args) > idx:
                continue
            if f"{key[0]}.{key[1]}" in _ALLOWED:
                continue
            bad.append(f"{path.name}:{n.lineno} → {key[0]}.{key[1]}"
                       f"({argname}={default}) · {ast.unparse(n)[:70]}")
    return bad


# ── Пробы: правило видит то, на чём оно красное ────────────────────────────

def test_the_rule_is_red_on_a_default_ceiling(tmp_path):
    """Подложный движок с потолком и ручка, зовущая его без своего."""
    (tmp_path / "services").mkdir()
    (tmp_path / "services" / "probe_engine.py").write_text(
        "async def list_things(pool, owner_id, limit: int = 100):\n"
        "    return []\n", encoding="utf-8")
    handler = tmp_path / "services" / "mini_app_probe.py"
    handler.write_text(
        "from services import probe_engine as _pe\n\n\n"
        "async def things(request):\n"
        "    return _json_resp({'things': await _pe.list_things(pool, uid)})\n",
        encoding="utf-8")
    import tests.test_a_ceiling_hides_in_a_callees_signature as mod
    old = mod.ROOT
    mod.ROOT = tmp_path
    try:
        bad = mod._findings([handler])
    finally:
        mod.ROOT = old
    assert len(bad) == 1 and "probe_engine.list_things" in bad[0], bad


def test_passing_a_limit_closes_it(tmp_path):
    (tmp_path / "services").mkdir()
    (tmp_path / "services" / "probe_engine.py").write_text(
        "async def list_things(pool, owner_id, limit: int = 100):\n"
        "    return []\n", encoding="utf-8")
    handler = tmp_path / "services" / "mini_app_probe.py"
    handler.write_text(
        "from services import probe_engine as _pe\n\n\n"
        "async def things(request):\n"
        "    lim = _list_limit(request, 100, 500)\n"
        "    return _json_resp({'things': await _pe.list_things("
        "pool, uid, limit=lim)})\n", encoding="utf-8")
    import tests.test_a_ceiling_hides_in_a_callees_signature as mod
    old = mod.ROOT
    mod.ROOT = tmp_path
    try:
        assert mod._findings([handler]) == []
    finally:
        mod.ROOT = old


def test_a_named_constant_ceiling_is_seen_too(tmp_path):
    """`limit: int = BATCH` — такой же потолок, как `limit: int = 500`."""
    (tmp_path / "services").mkdir()
    (tmp_path / "services" / "probe_engine.py").write_text(
        "BATCH = 500\n\n\n"
        "async def list_things(pool, owner_id, limit: int = BATCH):\n"
        "    return []\n", encoding="utf-8")
    handler = tmp_path / "services" / "mini_app_probe.py"
    handler.write_text(
        "from services import probe_engine as _pe\n\n\n"
        "async def things(request):\n"
        "    return await _pe.list_things(pool, uid)\n", encoding="utf-8")
    import tests.test_a_ceiling_hides_in_a_callees_signature as mod
    old = mod.ROOT
    mod.ROOT = tmp_path
    try:
        bad = mod._findings([handler])
    finally:
        mod.ROOT = old
    assert len(bad) == 1 and "limit=500" in bad[0], bad


def test_a_homonym_in_the_same_file_is_not_a_foreign_call(tmp_path):
    """`_str_list` своего файла — не `channel_admin._str_list` с потолком в 8."""
    (tmp_path / "services").mkdir()
    (tmp_path / "services" / "channel_admin.py").write_text(
        "def _str_list(v, n: int = 8, limit: int = 8):\n"
        "    return []\n", encoding="utf-8")
    handler = tmp_path / "services" / "mini_app_probe.py"
    handler.write_text(
        "def _str_list(value):\n"
        "    return []\n\n\n"
        "async def send(request):\n"
        "    return _str_list(data.get('usernames'))\n", encoding="utf-8")
    import tests.test_a_ceiling_hides_in_a_callees_signature as mod
    old = mod.ROOT
    mod.ROOT = tmp_path
    try:
        assert mod._findings([handler]) == []
    finally:
        mod.ROOT = old


def test_the_rule_looks_at_the_whole_layer():
    """Пустой список находок при пустом охвате ничего не значит."""
    caps = _capped_functions()
    assert len(caps) >= 80, f"функций с потолком по умолчанию найдено {len(caps)}"
    files = sorted((ROOT / "services").glob("mini_app*.py"))
    assert len(files) >= 25, f"файлов слоя найдено {len(files)}"


def test_every_allowance_has_a_reason():
    for key, why in _ALLOWED.items():
        assert "." in key, key
        assert len(why) > 20, (key, why)


def test_the_doorless_allowances_are_still_doorless():
    """Причина «ручку с фронта не зовут» — проверяемая, а не записка.

    Сосед закрыл похожий класс и оставил вывод: запись «это безвредно» надо
    сверять с определением вреда в том же файле. Здесь вред — молча обрезанный
    список на экране, значит достаточно убедиться, что экрана нет.
    """
    front = "\n".join(
        p.read_text(encoding="utf-8")
        for p in [ROOT / "mini_app" / "index.html",
                  *sorted((ROOT / "mini_app" / "screens").glob("*.js"))])
    # Опечатка в пути делала бы проверку пустой: сперва убедимся, что роут
    # вообще есть. Первая версия этой проверки называла «/cloud/list» —
    # такого роута нет, настоящий «/cloud/files», и проверка молчала бы всегда.
    snapshot = (ROOT / "tests" / "miniapp_routes_snapshot.txt").read_text(
        encoding="utf-8")
    for key, route in _ALLOWED_NO_FRONT.items():
        assert key in _ALLOWED, f"{key} нет в _ALLOWED"
        assert f"GET {route}\n" in snapshot, (
            f"роута {route} нет в снимке маршрутов: проверка «двери нет» "
            "ничего не проверяет")
        assert route not in front, (
            f"{key} разрешён тем, что {route} с фронта не зовут, — а теперь "
            "зовут: потолок снова обрежет список молча")


def test_an_allowance_that_no_longer_matches_is_removed():
    """Разрешение, под которое больше нет вызова, — мёртвая запись: следующий
    такой вызов пройдёт мимо правила молча."""
    caps = _capped_functions()
    unknown = [k for k in _ALLOWED
               if tuple(k.rsplit(".", 1)) not in caps]
    assert not unknown, (
        "в _ALLOWED записи, которым не соответствует ни одна функция с "
        "потолком: " + ", ".join(unknown))


# ── Собственно правило ─────────────────────────────────────────────────────

def test_no_handler_leaves_a_ceiling_in_a_callees_signature():
    bad = _findings()
    assert not bad, (
        "обработчик зовёт функцию, у которой потолок стоит в значении по "
        "умолчанию, и своего не передаёт: список молча обрежется, а в тексте "
        "обработчика числа не видно. Передайте свой limit и отдайте total, "
        "либо назовите место в _ALLOWED с причиной:\n  " + "\n  ".join(bad))
