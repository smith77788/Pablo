"""Раскладка аккаунтов по прокси: один аккаунт — один выход в сеть.

Разрыв, который это закрывает. При импорте сессий поле прокси одно на всю
партию: выбранный прокси записывался КАЖДОМУ импортированному аккаунту, а если
прокси не выбран — все получали NULL и уходили в сеть с одного и того же
реального IP хоста. То есть двадцать аккаунтов, добавленных одним действием,
оказывались за одним выходом.

Это противоречит собственному стандарту продукта: `proxy_selector`
.audit_proxy_isolation считает нормой `max_per_ip = 1` и показывает общий IP как
риск бана. Продукт создавал этот риск сам, на первом же шаге, а потом честно
сообщал о нём в аудите — то есть жаловался на то, что сделал.

Общий IP — сильнейший корреляционный признак когорты: Telegram связывает
аккаунты по нему надёжнее, чем по отпечатку устройства (устройства и api_id
импорт уже рандомизирует по аккаунту). Когда банят один аккаунт когорты,
следом уезжает вся когорта.

Здесь только решение о раскладке — без базы и без сети.
"""
from __future__ import annotations


def plan_distribution(proxy_loads: list[tuple], count: int) -> list:
    """Кому какой прокси. Возвращает список длиной `count`.

    `proxy_loads` — [(proxy_id, сколько аккаунтов уже на нём), ...] по ЖИВЫМ
    прокси владельца. Каждый следующий аккаунт уходит на наименее загруженный
    прокси, поэтому свободные (0 аккаунтов) разбираются первыми и достигается
    строгая изоляция 1:1, пока прокси хватает.

    Когда прокси кончились, остаток получает None — это честный реальный IP
    хоста, а не выдуманное назначение. Пустой список прокси → все None: режим
    «без прокси» законный, и ломать импорт из-за его отсутствия нельзя.
    """
    n = max(0, int(count or 0))
    if n == 0:
        return []
    loads = []
    for item in (proxy_loads or []):
        try:
            pid, used = item[0], item[1]
            if pid is None:
                continue
            loads.append([int(pid), int(used or 0)])
        except (TypeError, ValueError, IndexError, KeyError):
            # Битая строка не должна ронять импорт: пропускаем её и
            # раскладываем по тому, что разобралось.
            continue
    if not loads:
        return [None] * n

    out = []
    for _ in range(n):
        # Наименее загруженный; при равенстве — меньший id, чтобы раскладка была
        # воспроизводимой (и тесты не зависели от порядка строк из базы).
        loads.sort(key=lambda x: (x[1], x[0]))
        pid = loads[0][0]
        loads[0][1] += 1
        out.append(pid)
    return out


def plan_evacuation(stranded_ids: list, live_loads: list[tuple]) -> dict:
    """Куда переселить аккаунты с мёртвых прокси.

    Разрыв. Сторож прокси честно говорит «замените прокси или переназначьте
    аккаунты на рабочий», но единственное автоматическое переназначение
    (`proxy_selector.failover_dead_proxies`) берёт ТОЛЬКО прокси, заранее
    помеченные как резервные. Пользователь, у которого просто есть живые
    прокси со свободным местом, не помечал ничего резервным — и failover для
    него не делал ничего, оставляя аккаунты стоять намертво.

    Аккаунт на мёртвом прокси не делает вообще ничего, поэтому переезд на
    живой прокси — улучшение даже когда изоляция при этом становится не 1:1.
    Но молчать об ухудшении нельзя: об этом говорит `isolation_note`.

    Возвращает {"moves": [(account_id, proxy_id), ...], "stranded": [ids...]}
    — застрявшие остаются, если живых прокси нет вовсе.
    """
    ids = [i for i in (stranded_ids or []) if i is not None]
    if not ids:
        return {"moves": [], "stranded": []}
    plan = plan_distribution(live_loads, len(ids))
    moves, stranded = [], []
    for acc_id, pid in zip(ids, plan):
        if pid is None:
            stranded.append(acc_id)
        else:
            moves.append((acc_id, pid))
    return {"moves": moves, "stranded": stranded}


def isolation_summary(assignments: list, prior_loads: dict | None = None) -> dict:
    """Что получилось: сколько аккаунтов делят один выход.

    `prior_loads` — {proxy_id: сколько было ДО}, чтобы считать итоговую
    населённость прокси, а не только новичков. Аккаунты без прокси (None)
    считаются отдельно: они делят реальный IP хоста, и это тоже общий выход.
    """
    prior = dict(prior_loads or {})
    per: dict = {}
    naked = 0
    for pid in (assignments or []):
        if pid is None:
            naked += 1
            continue
        per[pid] = per.get(pid, 0) + 1
    totals = {pid: per[pid] + int(prior.get(pid, 0) or 0) for pid in per}
    crowded = sorted((n for n in totals.values() if n > 1), reverse=True)
    return {
        "assigned": sum(per.values()),
        "without_proxy": naked,
        "proxies_used": len(per),
        "max_per_proxy": max(totals.values()) if totals else 0,
        "crowded": crowded,
    }


def isolation_note(summary: dict) -> str | None:
    """Предупреждение человеку, если изоляция не 1:1. None — всё хорошо.

    Пишем до того, как аккаунты пойдут в работу: после массового бана когорты
    объяснять уже поздно.
    """
    if not summary:
        return None
    naked = int(summary.get("without_proxy") or 0)
    worst = int(summary.get("max_per_proxy") or 0)
    parts = []
    if worst > 1:
        parts.append(
            f"на один прокси пришлось до {worst} аккаунтов — Telegram связывает "
            f"аккаунты по общему IP, и бан одного тянет за собой остальных")
    if naked > 1:
        parts.append(
            f"{naked} аккаунтов без прокси выходят с общего IP сервера")
    elif naked == 1:
        parts.append("1 аккаунт без прокси выходит с общего IP сервера")
    if not parts:
        return None
    return "⚠️ Изоляция неполная: " + "; ".join(parts) + ". Добавьте прокси и распределите аккаунты."
