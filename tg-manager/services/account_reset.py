"""Ручной сброс кулдауна и риска аккаунта — ОДНА реализация на все кнопки.

Кнопка «Сбросить кулдаун» есть в трёх местах (Mini App, бот — один аккаунт, бот
— все сразу), и все три были написаны отдельно, каждая со своим набором шагов.
В итоге результат зависел от того, откуда владелец нажал:

  * Mini App не чистил in-memory кулдаун flood_engine, а его читают отдельно от
    БД: `strike_engine` отсеивает такой аккаунт в preflight, экран страйка в
    боте отвечает «аккаунт на остывании, попробуйте позже», а пульс флота
    показывает «остывает». Кулдаун в памяти живёт до перезапуска процесса, и
    сброс в БД сам по себе его не трогает;
  * бот не ставил `risk_cleared_at` — гейт операций `is_account_quarantined` и
    риск-пульс продолжали считать старые ограничения, и аккаунт не брался в
    работу;
  * бот снимал `acc_status='cooldown'` даже при устойчивом конфликте сессии, где
    `account_monitor._heal_expired_cooldowns` статус намеренно НЕ трогает (иначе
    самолечение и проверка здоровья перебрасывают статус туда-сюда, и оператор
    не видит, что сессию надо перезалить);
  * ни бот, ни массовый сброс не снимали process-local запрет
    `account_health.suitability`, из-за которого `get_sorted_accounts` молча
    выкидывал аккаунт из подбора до следующего часового цикла здоровья. Сам
    сброс потом снимал два флага из пяти (`dm`, `invite`) и не трогал
    `health_score`, хотя бан в памяти гасит и все пять флагов, и счётчик: для
    `create`, `post` и `join` аккаунт оставался выключенным, а риск-пульс
    продолжал считать его выбывшим.

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

    Оба живут в памяти процесса и переживают очистку БД: кулдаун читает
    strike_engine.preflight и экран страйка в боте, запрет suitability —
    account_health.get_sorted_accounts. main.py держит бот, Mini App API и
    op_worker в ОДНОМ процессе, поэтому правка видна сразу. Обе части
    вспомогательные: их сбой не должен ронять сброс.
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
            # Снимаем ВСЁ, что держит память процесса: и флаги suitability (все
            # пять, а не два — бан в памяти гасит их все, и аккаунт оставался
            # выключенным для create/post/join), и упавший до нуля health_score,
            # по которому риск-пульс продолжал светить «на паузе».
            _ah.clear_local_blocks(acc_id)
    except Exception:
        log.warning("account_reset: процесс-локальные тормоза не сняты для %d "
                    "аккаунтов", len(account_ids), exc_info=True)


async def _записать_в_журнал(pool, owner_id: int, действие: str, цель: str) -> None:
    """Отметить снятие риска в operation_audit.

    Снятие риска — не косметика: `risk_cleared_at` выключает предохранитель
    `is_account_quarantined`, и аккаунт с недавним баном снова берётся в
    работу. Нажать кнопку может не только хозяин — доступ к аккаунтам даётся
    и через workspace, и через экосистему. Без записи на вопрос «почему
    забаненный аккаунт снова пошёл в инвайты» ответа в системе нет.

    Запись никогда не роняет сам сброс: он уже применён.
    """
    try:
        from database.db import record_manual_action

        await record_manual_action(pool, owner_id, действие, target=цель)
    except Exception:
        log.debug("account_reset: журнал не записан owner=%s", owner_id, exc_info=True)


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
        изменено = int(str(res).rsplit(" ", 1)[-1]) > 0
    except ValueError:
        изменено = True
    if изменено:
        await _записать_в_журнал(pool, owner_id, "account_risk_cleared", str(acc_id))
    return изменено


async def cooled_account_ids(pool, owner_id: int) -> list[int]:
    """Аккаунты владельца, реально стоящие на паузе, — ОДИН список на всех.

    Пауза бывает ТРЁХ видов, и два из них в `tg_accounts` не видны:

      1. `cooldown_until` в `tg_accounts` — переживает перезапуск;
      2. кулдаун flood_engine в памяти процесса — его читают
         `strike_engine.preflight` и экран страйка в боте. Меню «Сбросить
         кулдауны» смотрело только в базу и на таком аккаунте писало «✅ Нет
         активных кулдаунов — все аккаунты доступны», пока страйк отвечал
         «аккаунт на остывании»;
      3. карантин риск-пульса по `restriction_events` — его читает гейт всех
         операций `is_account_quarantined`. Окна кулдауна у такого аккаунта
         нет вовсе, поэтому на экране сброса он не появлялся НИКОГДА: пульс
         считал его в «Карантин», операции обходили, а единственная кнопка,
         которая его освобождает (`risk_cleared_at`), показывала владельцу
         «все аккаунты доступны». Та же жалоба «кулдаун не сбрасывается»,
         только у неё не было даже кнопки;
      4. процесс-локальные тормоза `account_health` — обнулённый `health_score`
         и снятые флаги `suitability`. Их не видно ни в базе, ни в кулдауне
         flood_engine: риск-пульс по первому светит «на паузе», подбор под
         действие по вторым молча выбрасывает аккаунт, а экран снятия пауз о
         них не спрашивал. Четвёртый способ получить ту же жалобу.

    Мёртвый по статусу аккаунт (`account_status.is_dead`) в список НЕ идёт:
    снятие риска его не воскрешает (`banned` остаётся `banned`, и дверь
    выбора его всё равно не возьмёт), а кнопка, которая ничего не меняет, —
    это третий способ получить ту же жалобу. Про такой аккаунт честно
    говорит экран флота: «забанен — не восстановить».
    """
    if not pool or not owner_id:
        return []
    try:
        rows = await pool.fetch(
            "SELECT id, COALESCE(acc_status,'active') AS acc_status, "
            "(cooldown_until IS NOT NULL AND cooldown_until > NOW()) AS cd_db "
            "FROM tg_accounts WHERE owner_id=$1 AND is_active=TRUE",
            owner_id)
    except Exception:
        log.warning("account_reset: список аккаунтов не получен owner=%s",
                    owner_id, exc_info=True)
        return []
    try:
        from services.flood_engine import is_account_cooling
    except Exception:
        is_account_cooling = None  # нет сигнала памяти → считаем только базу

    # Третий вид паузы — карантин риск-пульса. Спрашиваем ОДНИМ запросом на
    # весь флот (та же дверь, что у гейта операций), и только про аккаунты,
    # которые снятие риска реально вернёт в строй.
    from services import account_status as _acc_status

    revivable = [int(r["id"]) for r in rows
                 if not _acc_status.is_dead(r.get("acc_status"))]
    quarantined: set[int] = set()
    try:
        from services.infra_memory import quarantined_accounts
        quarantined = await quarantined_accounts(pool, revivable)
    except Exception:
        # Обогащение не критично: без него экран покажет хотя бы кулдауны,
        # как показывал раньше. Ронять из-за него единственный ручной путь
        # вернуть аккаунт в строй нельзя.
        log.warning("account_reset: карантин риск-пульса не получен owner=%s",
                    owner_id, exc_info=True)

    # Четвёртый вид паузы — память процесса (health_score/suitability). Порог
    # и предикат живут в account_health, чтобы экран и пульс не разъехались.
    try:
        from services.account_health import local_block_reason
    except Exception:
        local_block_reason = None

    cooled: list[int] = []
    for r in rows:
        acc_id = int(r["id"])
        if r["cd_db"] or acc_id in quarantined:
            cooled.append(acc_id)
            continue
        if local_block_reason is not None and acc_id in revivable:
            try:
                if local_block_reason(acc_id):
                    cooled.append(acc_id)
                    continue
            except Exception:
                log.debug("account_reset: локальный тормоз acc=%s не прочитан",
                          acc_id, exc_info=True)
        if is_account_cooling is None:
            continue
        try:
            if is_account_cooling(acc_id):
                cooled.append(acc_id)
        except Exception:
            log.debug("account_reset: проверка памяти не удалась acc=%s", acc_id,
                      exc_info=True)
    return cooled


async def reset_all_cooled(pool, owner_id: int) -> int:
    """Сброс всех стоящих на паузе аккаунтов владельца. Возвращает счёт.

    Набор — ровно тот, что владелец видит в меню (см. cooled_account_ids), и не
    шире: массовое снятие риска не должно захватывать аккаунты, которых в
    списке остывающих нет.
    """
    if not pool or not owner_id:
        return 0
    cooled_ids = await cooled_account_ids(pool, owner_id)
    if not cooled_ids:
        return 0
    try:
        res = await pool.execute(
            f"UPDATE tg_accounts SET {_CLEAR_STATUS_SQL} "
            "WHERE owner_id=$1 AND id = ANY($2::bigint[])",
            owner_id, cooled_ids)
    except Exception:
        log.warning("account_reset: массовый сброс не удался owner=%s",
                    owner_id, exc_info=True)
        raise
    _clear_in_memory(cooled_ids)
    await _записать_в_журнал(
        pool, owner_id, "accounts_risk_cleared_bulk",
        ",".join(str(i) for i in cooled_ids[:50]))
    try:
        return int(str(res).rsplit(" ", 1)[-1])
    except ValueError:
        return len(cooled_ids)
