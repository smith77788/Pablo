"""Срез контактов для массового действия: один резолвер и ОДНА правда о размере.

ЧТО БЫЛО. Резолвер среза жил замыканием внутри `setup_routes` мини-аппа и
отдавал ТОЛЬКО список, обрезанный потолком `_SEG_MAX = 20000`. Размер среза
никто не считал, поэтому превью печатало длину обрезанного списка как размер
сегмента:

    🎯 В сегменте: 20000 · адресуемых в ЛС: 20000

— при пятидесяти тысячах подходящих контактов. Дальше запуск брал те же первые
двадцать тысяч (`ORDER BY id`), тост говорил «Запущено: 20000 получателей», и
ни одна строка нигде не сообщала, что тридцать тысяч человек в рассылку не
попали. Владелец считал сегмент разосланным.

Это тот самый класс, который репозиторий уже стережёт для списков
(`tests/test_miniapp_lists_admit_truncation.py`): урезанный список обязан
признаваться урезанным. Для массового действия цена ошибки выше — не «экран
соврал», а «треть аудитории не получила сообщение, и об этом не узнали».

ЧТО ТЕПЕРЬ. `resolve` возвращает пару (строки, настоящий размер среза):
размер берётся из `count_segment` по тому же WHERE, а не из длины выдачи. Кто
показывает число — показывает настоящее; кто действует — действует по выдаче и
обязан объявить разницу (`omitted`).

Второе: ошибка БД больше не выглядит пустым сегментом. Раньше запрос шёл через
fail-soft `_safe_fetch`, и сбой базы давал пустой список — то есть «в сегменте
нет адресуемых контактов» на полном сегменте. Теперь исключение уходит наверх,
а неверный идентификатор контакта отбивается до запроса (`ValueError`), чтобы
мусор в теле запроса не ронял ручку пятисоткой.
"""
from __future__ import annotations

import json as _json
import logging
import uuid as _uuid

log = logging.getLogger(__name__)

# Потолок одного запуска: защита от случайного «весь срез» на всей базе.
SEG_MAX = 20000

_FILTER_GENDERS = ("m", "f", "unknown")


def segment_filters(src: dict) -> dict:
    """Фильтры среза из тела запроса — те же оси, что у списка контактов."""
    def _b(k):
        return str(src.get(k)).lower() in ("1", "true", "yes", "on")

    g = src.get("gender")
    if g not in _FILTER_GENDERS:
        g = None

    def _int(k):
        v = src.get(k)
        return int(v) if str(v).isdigit() else None

    return {
        "search": (src.get("search") or "").strip() or None,
        "tag": (src.get("tag") or "").strip() or None,
        "group_id": _int("group_id"),
        "favorite_only": _b("favorite"), "premium_only": _b("premium"),
        "multi_only": _b("multi"), "mutual_only": _b("mutual"),
        "gender": g, "crm_stage": (src.get("crm_stage") or "").strip()[:40] or None,
        "account_id": _int("account_id"),
    }


def _valid_ids(raw) -> list[str]:
    """Идентификаторы контактов из тела запроса.

    `unified_contacts.id` — uuid, и `id = ANY($1::uuid[])` на мусоре роняет
    запрос ошибкой приведения типа. Мусор — ошибка запроса, а не пустой
    сегмент, поэтому отбиваем до обращения к базе.
    """
    out: list[str] = []
    seen: set[str] = set()
    for x in raw:
        s = str(x)
        try:
            key = str(_uuid.UUID(s))
        except (ValueError, AttributeError, TypeError):
            raise ValueError("Неверный идентификатор контакта")
        if key not in seen:
            seen.add(key)
            out.append(s)
    return out


def _parse_phones(d: dict) -> dict:
    if isinstance(d.get("phones"), str):
        try:
            d["phones"] = _json.loads(d["phones"])
        except Exception:
            d["phones"] = []
    return d


async def resolve(pool, owner_id: int, body: dict,
                  limit: int = SEG_MAX) -> tuple[list, int]:
    """Контакты сегмента и НАСТОЯЩИЙ размер среза.

    Вход — один из трёх: явный `contact_ids`, сохранённый сегмент
    (`saved_segment_id`) или фильтры в теле. Возврат — (строки с полями
    адресации, сколько контактов в срезе всего). Строк не больше `limit`;
    когда размер больше, разницу обязан объявить вызывающий.
    """
    cids = body.get("contact_ids")
    if isinstance(cids, list) and cids:
        ids = _valid_ids(cids)
        rows = await pool.fetch(
            "SELECT id, username, telegram_user_id, first_name, last_name, phones "
            "FROM unified_contacts WHERE owner_id=$1 AND id = ANY($2::uuid[]) "
            "ORDER BY id LIMIT $3",
            owner_id, ids, limit)
        out = [_parse_phones(dict(r)) for r in rows]
        if len(ids) <= limit:
            # выдача не обрезана: сколько нашлось — столько и есть
            return out, len(out)
        total = int(await pool.fetchval(
            "SELECT COUNT(*) FROM unified_contacts "
            "WHERE owner_id=$1 AND id = ANY($2::uuid[])",
            owner_id, ids) or 0)
        return out, total

    from services.contacts_hub.repository import (
        count_segment, get_segment_filters, resolve_segment,
    )

    filters = None
    sid = body.get("saved_segment_id")
    if str(sid).isdigit():
        filters = await get_segment_filters(pool, owner_id, int(sid))
    if filters is None:
        filters = segment_filters(body)
    rows = await resolve_segment(pool, owner_id, filters, limit=limit)
    if len(rows) < limit:
        # потолок не достигнут — срез кончился раньше, второй запрос ни к чему
        return rows, len(rows)
    total = await count_segment(pool, owner_id, filters)
    return rows, max(total, len(rows))


async def pressure_ok(pool, owner_id: int, op_name: str) -> tuple[bool, str]:
    """Гейт давления инфраструктуры (ban-safety) — как у остальных масс-опер."""
    try:
        from services import infra_orchestrator
        return await infra_orchestrator.is_ready_for_op(pool, owner_id, op_name)
    except Exception:
        log.warning("segment: pressure check failed", exc_info=True)
        return True, ""
