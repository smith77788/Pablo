"""Фильтры выборки parsed_audiences — ОДИН источник правды.

Раньше жил в mini_app_api (только парсер-вью фильтровал); инвайт и DM собирали
аудиторию БЕЗ фильтров, хотя богатые колонки (is_premium/is_bot/username/is_active/
phone) уже хранились. Вынесено сюда, чтобы парсер-вью, инвайт и DM применяли
ОДНИ И ТЕ ЖЕ условия. Чистая функция — тестируема, без БД.
"""
from __future__ import annotations

from typing import Any


def _truthy(v: Any) -> bool:
    return str(v).lower() in ("1", "true", "yes", "on")


def parsed_audience_filters(q, base_params_count: int = 1):
    """Доп. WHERE-условия + параметры для выборки parsed_audiences.

    ``q``: mapping с ключами source, premium, with_username, not_bot, active,
    with_phone (значения — truthy-строки/булевы), gender, last_seen и ``q`` —
    строка поиска по имени/@username/ID. Плейсхолдеры продолжаются с
    ``base_params_count`` (после уже занятых, напр. owner_id=$1). Булевы фильтры
    параметров НЕ добавляют (только ``source`` добавляет один ILIKE-параметр).

    Возвращает ``(sql, params)``: ``sql`` начинается с " AND …" или пустой.
    """
    conds: list[str] = []
    params: list = []
    idx = base_params_count
    get = q.get if hasattr(q, "get") else (lambda k, d=None: None)
    src = (get("source") or "").strip()
    if src:
        idx += 1
        conds.append(f"source_username ILIKE ${idx}")
        params.append(f"%{src}%")
    if _truthy(get("premium")):
        conds.append("is_premium=TRUE")
    if _truthy(get("with_username")):
        conds.append("username IS NOT NULL AND username<>''")
    if _truthy(get("not_bot")):
        conds.append("COALESCE(is_bot,FALSE)=FALSE")
    if _truthy(get("active")):
        conds.append("is_active=TRUE")
    if _truthy(get("with_phone")):
        conds.append("phone IS NOT NULL AND phone<>''")
    # Пол ('m'|'f') — таргетинг по полу (services/gender_classifier размечает
    # parsed_audiences.gender). Любое иное значение фильтр не добавляет.
    gender = str(get("gender") or "").strip().lower()
    if gender in ("m", "f"):
        idx += 1
        conds.append(f"gender=${idx}")
        params.append(gender)
    # Поиск по имени / @username / числовому ID. Живёт здесь, а не в обработчике
    # экрана, ровно по той же причине, что и остальные условия: список, счётчик
    # «найдено» и выгрузка обязаны отбирать ОДНО И ТО ЖЕ. Разъедься они — экран
    # покажет одно, а CSV отдаст другое, и заметить это можно только по факту.
    needle = str(get("q") or "").strip()[:64]
    if needle:
        bare = needle.lstrip("@").strip()
        if bare:
            idx += 1
            params.append(f"%{bare}%")
            parts = [
                f"username ILIKE ${idx}",
                f"first_name ILIKE ${idx}",
                f"last_name ILIKE ${idx}",
            ]
            # Числовую строку ищем и как точный tg_user_id: по ней человека и
            # находят, когда username у него нет.
            if bare.isdigit():
                try:
                    uid_exact = int(bare)
                except (TypeError, ValueError):
                    uid_exact = None
                if uid_exact is not None:
                    idx += 1
                    parts.append(f"tg_user_id=${idx}")
                    params.append(uid_exact)
            conds.append("(" + " OR ".join(parts) + ")")
    # Last Seen: оставить тех, кто был онлайн не позже N дней назад (0/мусор → без фильтра).
    ls_raw = str(get("last_seen") or "").strip()
    if ls_raw:
        try:
            ls_days = int(ls_raw)
        except (TypeError, ValueError):
            ls_days = 0
        if ls_days > 0:
            idx += 1
            conds.append(f"last_seen_days IS NOT NULL AND last_seen_days <= ${idx}")
            params.append(ls_days)
    sql = (" AND " + " AND ".join(conds)) if conds else ""
    return sql, params
