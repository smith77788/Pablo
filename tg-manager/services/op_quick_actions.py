"""Быстрые действия по ИТОГУ операции — вместо советов словами.

Разрыв, который это закрывает. Сводки операций давно умеют объяснять, что пошло
не так, но объясняют ТЕКСТОМ, а действия рядом нет. На живом отчёте инвайта это
выглядело так:

  «⚠️ Без прокси (прямой выход с IP хоста, риск блокировок): 28 — назначьте
   прокси для изоляции»
  «🚫 3 аккаунтов без прав админа выведены из круга — проверьте, что автовыдача
   админки прошла»
  «🛑 Флот перегрет — дайте им отдохнуть и повторите позже»

Продукт знал и диагноз, и лечение — и предлагал пользователю пойти сделать это
самому, руками, найдя нужный экран. Плюс статичная карта «тип операции → одна
кнопка» покрывала 4 типа из семидесяти и не смотрела на исход вообще.

Здесь решения строятся по СТРУКТУРНОМУ результату исполнителя (не по разбору
текста сводки — он меняется и локализуется). Чистые функции, проверяются без
базы и сети.

Формат действия: {"id", "label", "fn", "arg"?, "reason"}. `fn` — имя функции
мини-аппа; `arg` передаётся ей, если задан.
"""
from __future__ import annotations


def _int(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


# Куда вести после успешной операции, когда особых проблем в исходе нет.
# Раньше такая карта покрывала 4 типа из ~70 — большинство операций
# заканчивались тупиком «готово» без единого следующего шага.
_AFTER_DONE = {
    "mass_invite":              ("📊 Открыть здоровье флота", "openHealth"),
    "auto_register":            ("🔥 Прогреть аккаунты", "openWarmup"),
    "reg_check":                ("🔥 Прогреть аккаунты", "openWarmup"),
    "bot_factory":              ("🤖 Открыть ботов", "loadBots"),
    "scan_owned_resources":     ("📡 Открыть каналы", "openChannels"),
    "scan_owned_bots":          ("🔌 Подключить найденных", "connectDiscoveredBots"),
    "connect_discovered_bots":  ("🤖 Открыть ботов", "loadBots"),
    "account_warmup":           ("📊 Открыть здоровье флота", "openHealth"),
    "check_accounts_health":    ("🩹 Открыть восстановление", "openRehab"),
    "import_sessions":          ("🌐 Назначить прокси", "openProxies"),
    "bulk_dm_adhoc":            ("📊 Открыть здоровье флота", "openHealth"),
    "dm_campaign":              ("📊 Открыть здоровье флота", "openHealth"),
    "create_chatlist_folder":   ("📁 Открыть общие папки", "openFolders"),
    "parse_audience":           ("📨 Запустить инвайт", "openMassInvite"),
}


def suggest(op_type: str, status: str, result: dict | None,
            op_id: int | None = None) -> list[dict]:
    """Быстрые действия по итогу операции. Порядок = приоритет показа.

    Сначала то, что чинит НАЙДЕННУЮ проблему (прокси, права, остаток целей), и
    только потом обычный следующий шаг — иначе полезное действие тонет.
    """
    res = result if isinstance(result, dict) else {}
    st = (status or "").strip().lower()
    out: list[dict] = []
    seen: set[str] = set()

    def _add(aid, label, fn, reason, arg=None):
        if aid in seen or not fn:
            return
        seen.add(aid)
        item = {"id": aid, "label": label, "fn": fn, "reason": reason}
        if arg is not None:
            item["arg"] = arg
        out.append(item)

    # 1. Проблемы флота, названные самим исполнителем.
    if _int(res.get("no_proxy")) > 0:
        n = _int(res.get("no_proxy"))
        _add("assign_proxy", f"🌐 Назначить прокси ({n})", "openProxies",
             f"{n} аккаунтов работали с прямого IP хоста — это риск блокировок")
    if _int(res.get("no_rights_retired")) > 0:
        n = _int(res.get("no_rights_retired"))
        _add("check_rights", f"🛡 Проверить права ({n})", "openChannels",
             f"{n} аккаунтов выведены из круга без прав админа")
    if res.get("flood_storm"):
        _add("fleet_health", "📊 Здоровье флота", "openHealth",
             "флот перегрет — посмотрите, каким аккаунтам нужен отдых")

    # 2. Недоделанная цель. Если продолжение уже запланировано — не зовём
    #    человека делать то, что система сделает сама.
    left = _int(res.get("left"))
    if left > 0 and not res.get("next_op_id") and op_id:
        _add("retry_left", f"↻ Доработать остаток ({left})", "retryOp",
             f"осталось {left} целей, продолжение не запланировано", arg=op_id)

    # 3. Провалившаяся операция — повтор.
    if st == "failed" and op_id:
        _add("retry", "↻ Повторить", "retryOp", "операция провалилась", arg=op_id)

    # 4. Мёртвые сессии — чинить аккаунты, а не повторять.
    if res.get("all_failed_connect"):
        _add("fix_accounts", "📱 Открыть аккаунты", "goTab",
             "ни один аккаунт не подключился — нужны живые сессии",
             arg="accounts")

    # 5. Обычный следующий шаг по типу операции.
    nxt = _AFTER_DONE.get(op_type or "")
    if nxt and st != "failed":
        _add(f"next_{op_type}", nxt[0], nxt[1], "следующий шаг")

    return out
