"""Ручной сброс кулдауна и риска аккаунта — ОДНА реализация на все кнопки.

Кнопка «Сбросить кулдаун» есть в трёх местах (Mini App, бот — один аккаунт, бот
— все сразу), и все три были написаны отдельно, каждая со своим набором шагов.
В итоге результат зависел от того, откуда владелец нажал:

  * Mini App не чистил in-memory кулдаун flood_engine — `wait_if_cooling` и
    подбор аккаунтов продолжали держать аккаунт в паузе до перезапуска процесса;
  * бот не ставил `risk_cleared_at` — гейт операций `is_account_quarantined` и
    риск-пульс продолжали считать старые ограничения, и аккаунт не брался в
    работу;
  * бот снимал `acc_status='cooldown'` даже при устойчивом конфликте сессии, где
    `account_monitor._heal_expired_cooldowns` статус намеренно НЕ трогает (иначе
    самолечение и проверка здоровья перебрасывают статус туда-сюда, и оператор
    не видит, что сессию надо перезалить);
  * ни бот, ни массовый сброс не снимали process-local запрет
    `account_health.suitability` (dm/invite), из-за которого
    `get_sorted_accounts` молча выкидывал аккаунт из подбора до следующего
    часового цикла здоровья.

Полный сброс — это пять шагов, и пропуск любого возвращает жалобу владельца
«кулдаун не сбрасывается». Держим их здесь, а кнопки только зовут эту функцию.

Историю `restriction_events` не удаляем никогда: её читают здоровье, дрейф,
анти-шторм и мониторинг. `risk_cleared_at` — отметка «владелец осознанно снял
риск в этот момент»; ограничение ПОЗЖЕ отметки снова уводит аккаунт в карантин.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# Снимаем ТОЛЬКО транзиентный 'cooldown' (его ставит op_worker при сетевом/
# прокси-сбое). 'banned'/'warming'/'session_expired' — не наше дело. И не
# трогаем статус при устойчивом конфликте сессии: её надо перезалить, отметку
# снимет первая же чистая проверка. Те же предохранители, что у
# account_monitor._heal_expired_cooldowns — расходиться им нельзя.
_CLEAR_STATUS_SQL = """
    cooldown_until = NULL,
    risk_cleared_at = NOW(),
    acc_status = CASE
        WHEN COALESCE(acc_status,'active') = 'cooldown'
             AND session_conflict_at IS NULL
        THEN 'active' ELSE COALESCE(acc_status,'active') END,
    status_reason = CASE
        WHEN COALESCE(acc_status,'active') = 'cooldown'
             AND session_conflict_at IS NULL
        THEN NULL ELSE status_reason END
"""


def _clear_in_memory(account_ids: list[int]) -> None:
    """Снять process-local тормоза: кулдаун flood_engine и запрет suitability.

    Оба живут в памяти процесса и переживают очистку БД. main.py держит бот,
    Mini App API и op_worker в ОДНОМ процессе, поэтому правка видна сразу.
    Обе части — вспомогательные: их сбой не должен ронять сброс.
    """
    if not account_ids:
        return
    try:
        from services.flood_engine import clear_account_cooldown
        for acc_id in account_ids:
            clear_account_cooldown(acc_id)
    except Exception:
        log.warning("account_reset: in-memory кулдаун flood_engine не снят "
                    "для %d аккаунтов", len(account_ids), exc_info=True)
    try:
        from services import account_health as _ah
        for acc_id in account_ids:
            health = _ah.get_health(acc_id)
            for key in ("dm", "invite"):
                if key in health.suitability:
                    health.suitability[key] = True
    except Exception:
        log.warning("account_reset: запрет suitability не снят для %d аккаунтов",
                    len(account_ids), exc_info=True)


async def reset_account(pool, acc_id: int, owner_id: int) -> bool:
    """Полный сброс одного аккаунта владельца. True — строка в БД обновлена.

    owner-scoped: чужой аккаунт не трогаем и возвращаем False.
    """
    if not pool or not acc_id or not owner_id:
        return False
    try:
        res = await pool.execute(
            f"UPDATE tg_accounts SET {_CLEAR_STATUS_SQL} WHERE id=$1 AND owner_id=$2",
            acc_id, owner_id)
    except Exception:
        # НЕ глушим: это единственный ручной путь вернуть аккаунт в строй.
        log.warning("account_reset: сброс не удался acc=%s owner=%s",
                    acc_id, owner_id, exc_info=True)
        raise
    _clear_in_memory([acc_id])
    try:
        return int(str(res).rsplit(" ", 1)[-1]) > 0
    except ValueError:
        return True


async def reset_all_cooled(pool, owner_id: int) -> int:
    """Сброс всех аккаунтов владельца с ЖИВЫМ окном кулдауна. Возвращает счёт.

    Набор намеренно узкий (`cooldown_until > NOW()`), как и в кнопке бота до
    сведения: массовое снятие риска не должно захватывать аккаунты, которые
    владелец в этот момент не видит в списке остывающих.
    """
    if not pool or not owner_id:
        return 0
    cooled_ids: list[int] = []
    try:
        rows = await pool.fetch(
            "SELECT id FROM tg_accounts WHERE owner_id=$1 AND cooldown_until > NOW()",
            owner_id)
        cooled_ids = [int(r["id"]) for r in rows]
    except Exception:
        # Без списка id почистить память нечем, но сброс в БД всё равно делаем.
        log.warning("account_reset: список остывающих не получен owner=%s",
                    owner_id, exc_info=True)
    try:
        res = await pool.execute(
            f"UPDATE tg_accounts SET {_CLEAR_STATUS_SQL} "
            "WHERE owner_id=$1 AND cooldown_until > NOW()",
            owner_id)
    except Exception:
        log.warning("account_reset: массовый сброс не удался owner=%s",
                    owner_id, exc_info=True)
        raise
    _clear_in_memory(cooled_ids)
    try:
        return int(str(res).rsplit(" ", 1)[-1])
    except ValueError:
        return len(cooled_ids)
