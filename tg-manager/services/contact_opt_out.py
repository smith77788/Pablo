"""Глобальный реестр «не приглашать» (do-not-invite / opt-out).

Первопричина: `invite_target_log` (op_worker.py) дедупит инвайты только В
ПРЕДЕЛАХ ОДНОЙ ГРУППЫ и не различает исход — человек, явно попросивший больше
не писать, при следующей кампании в ДРУГУЮ группу получит новый инвайт. Ни CRM
(`unified_contacts`), ни инвайт-движок не знают о таком отказе — единственный
способ учесть его сегодня был бы вручную отредактировать список аудитории
перед каждым запуском.

Осознанно НЕ автоматизируется по классификации ошибок инвайта:
`UserPrivacyRestrictedError` — это настройка приватности, а не отказ (ссылка в
ЛС всё ещё может сработать), автоматический opt-out по ней дал бы ложные
срабатывания. Реестр наполняется ЯВНЫМ действием оператора (карточка контакта
в CRM или результаты инвайта), а дальше подключён по-настоящему: фильтрует
аудиторию во ВСЕХ будущих кампаниях, а не только в текущей.

Формат `target` — та же нормализованная строка, что использует
mass_inviter_engine.parse_user_refs/parse_phones: '@username' (без учёта
регистра), голый numeric id, или телефон в E.164 ('+79991234567').
"""
from __future__ import annotations

import re
from typing import Optional

_DDL = (
    "CREATE TABLE IF NOT EXISTS contact_opt_out("
    "owner_id BIGINT NOT NULL, target TEXT NOT NULL, reason TEXT, "
    "source TEXT NOT NULL DEFAULT 'manual', created_at TIMESTAMPTZ NOT NULL DEFAULT now(), "
    "PRIMARY KEY(owner_id, target))"
)


# Тот же порог, что mass_inviter_engine._PHONE_MIN_DIGITS: Telegram user_id
# сейчас укладывается в 10 цифр, E.164-телефон со страновым кодом — 11–15.
_PHONE_MIN_DIGITS = 11


def normalize_target(raw: str) -> Optional[str]:
    """Привести ввод оператора к канонической форме target.

    Зеркалит mass_inviter_engine.parse_user_refs/parse_phones (тот же формат и
    та же граница id/телефон, иначе фильтр в дедупе не совпадёт со списком
    аудитории):
      '@Ivan', 'Ivan', 'ivan' → '@ivan' (username, регистронезависимо)
      '123456789' (<11 цифр) → '123456789' (numeric id, как есть)
      '+7 999 123-45-67', '79991234567' (11+ цифр) → '+79991234567' (E.164)
    None — если строка не похожа ни на что из перечисленного.
    """
    if not raw:
        return None
    s = raw.strip()
    if not s:
        return None
    only_digits = re.sub(r"\D", "", s)
    is_phone_shaped = bool(re.fullmatch(r"[\d\s+()\-]+", s))
    if is_phone_shaped and (
        (s.startswith("+") and len(only_digits) >= 10)
        or len(only_digits) >= _PHONE_MIN_DIGITS
    ):
        return "+" + only_digits
    token = s.lstrip("@").strip()
    if re.match(r"^\d+$", token):
        return token  # короче телефонного порога — Telegram user_id
    if re.match(r"^[A-Za-z0-9_]{3,}$", token):
        return f"@{token.lower()}"
    return None


async def add(
    pool, owner_id: int, raw_target: str, *,
    reason: Optional[str] = None, source: str = "manual",
) -> Optional[str]:
    """Добавить target в реестр. Возвращает нормализованную форму или None
    (нераспознанный ввод — вызывающий код сообщает оператору об ошибке)."""
    target = normalize_target(raw_target)
    if not target:
        return None
    await pool.execute(_DDL)
    await pool.execute(
        """INSERT INTO contact_opt_out(owner_id, target, reason, source)
           VALUES($1,$2,$3,$4)
           ON CONFLICT (owner_id, target) DO UPDATE
               SET reason = COALESCE(EXCLUDED.reason, contact_opt_out.reason)""",
        owner_id, target, reason, source,
    )
    return target


async def remove(pool, owner_id: int, raw_target: str) -> bool:
    target = normalize_target(raw_target)
    if not target:
        return False
    await pool.execute(_DDL)
    result = await pool.execute(
        "DELETE FROM contact_opt_out WHERE owner_id=$1 AND target=$2", owner_id, target
    )
    return result.endswith(" 1") if isinstance(result, str) else False


async def is_opted_out(pool, owner_id: int, raw_target: str) -> bool:
    target = normalize_target(raw_target)
    if not target:
        return False
    await pool.execute(_DDL)
    row = await pool.fetchval(
        "SELECT 1 FROM contact_opt_out WHERE owner_id=$1 AND target=$2", owner_id, target
    )
    return bool(row)


async def load_opted_out(pool, owner_id: int) -> set[str]:
    """Полный набор opted-out target'ов владельца (для фильтрации батча).

    Fail-open: при сбое — пустой набор (лучше не отфильтровать, чем сорвать
    инвайт целиком; тот же принцип, что у op_worker._load_invited_targets)."""
    from services.logger import log_exc_swallow
    import logging

    try:
        await pool.execute(_DDL)
        rows = await pool.fetch(
            "SELECT target FROM contact_opt_out WHERE owner_id=$1", owner_id
        )
        return {r["target"] for r in (rows or [])}
    except Exception:
        log_exc_swallow(logging.getLogger(__name__), "contact_opt_out: load failed")
        return set()


def compare_key(value: str) -> str:
    """Ключ сравнения целей: приводим к нижнему регистру.

    Username в Telegram регистронезависим, а mass_inviter_engine.parse_user_refs
    сохраняет регистр как есть ('@Ivan'). Без общего ключа сравнение 'ivan' (из
    normalize_target) с '@Ivan' (из аудитории) тихо не совпадало бы, и реестр
    «не приглашать» переставал работать ровно для тех, чей регистр в исходных
    данных отличается от введённого оператором.

    Регистр снимается со ВСЕЙ строки, а не только с '@…': числовой id и телефон
    в E.164 состоят из цифр, для них lower() — тождество, зато правило совпадает
    с SQL-выражением `lower(target)`, которым те же цели сравнивает пре-флайт.
    Разные правила на двух сторонах давали расхождение цифр между «уже
    приглашено» в пре-флайте и тем, что реально пропустит прогон.
    """
    return str(value).lower()


_compare_key = compare_key   # прежнее имя: на него ссылались тесты


def filter_targets(targets: list, opted_out: set[str]) -> tuple[list, int]:
    """Убрать opted-out значения из списка user_refs/phones. Чистая функция."""
    if not opted_out:
        return list(targets), 0
    blocked = {compare_key(t) for t in opted_out}
    remaining = [t for t in targets if compare_key(t) not in blocked]
    return remaining, len(targets) - len(remaining)


async def list_for_owner(pool, owner_id: int, limit: int = 200) -> list[dict]:
    await pool.execute(_DDL)
    rows = await pool.fetch(
        """SELECT target, reason, source, created_at FROM contact_opt_out
           WHERE owner_id=$1 ORDER BY created_at DESC LIMIT $2""",
        owner_id, limit,
    )
    return [dict(r) for r in (rows or [])]
