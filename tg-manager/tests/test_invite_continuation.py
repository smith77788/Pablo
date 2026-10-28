"""Остаток аудитории не бросается: инвайт продолжает сам себя, пока не закончит.

ЖАЛОБА ПОЛЬЗОВАТЕЛЯ: «всё ещё не инвайтит».

ЧТО ПРОИСХОДИЛО. Суточный лимит на аккаунт консервативен по умолчанию (холодный
старт — 15). У пользователя с одним-двумя аккаунтами и аудиторией в сотни целей
первый прогон закрывал 15–30 штук, писал «⏸ Осталось в очереди: N» — и всё.
Остаток исчезал вместе с операцией; чтобы продолжить, надо было вспомнить и
запустить заново. Для человека, ожидавшего «пригласить всех», это выглядит как
«инвайт не работает».

Проверено на настоящей базе (postgres 16, полная схема, заглушен только Telethon):
50 целей и один аккаунт разошлись за 4 дня — 15/15/15/5, 50 уникальных, ноль
дублей, ноль потерь.

Границы намеренные: не продолжаем при перегреве флота и при закрытой группе —
там проблема не в лимитах, и повтор только навредит.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKER = (ROOT / "services" / "op_worker.py").read_text(encoding="utf-8")


def _fn(name: str) -> str:
    m = re.search(rf"async def {name}\(.*?(?=\nasync def |\n# ──)", WORKER, re.DOTALL)
    assert m, f"{name} не найдена"
    return m.group(0)


def _exec_body() -> str:
    m = re.search(r"async def _exec_mass_invite\(.*?(?=\nasync def )", WORKER, re.DOTALL)
    assert m
    return m.group(0)


def test_leftover_schedules_a_continuation():
    body = _exec_body()
    assert "_schedule_invite_continuation" in body, (
        "остаток очереди обязан уезжать в следующую операцию, а не исчезать"
    )
    assert "_next_op" in body and "продолжим завтра" in body, (
        "пользователь должен видеть, что работа не брошена"
    )


def test_continuation_carries_explicit_targets():
    """Повторное чтение источника захватило бы и уже приглашённых."""
    fn = _fn("_schedule_invite_continuation")
    assert '"user_refs"' in fn and '"phones"' in fn
    assert '"source"] = "import_list"' in fn or "'source'] = 'import_list'" in fn, (
        "источник должен быть зафиксирован списком"
    )
    assert 'pop("parse_run_id"' in fn, (
        "привязка к запуску парсера в продолжении неуместна — цели уже отобраны"
    )


def test_continuation_goes_through_the_bus():
    fn = _fn("_schedule_invite_continuation")
    assert "operation_bus" in fn and "submit(" in fn, (
        "продолжение — обычная операция, а не прямой INSERT в очередь"
    )
    assert "INSERT INTO operation_queue" not in fn


def test_continuation_is_not_re_gated_by_plan():
    """Цели здесь — остаток УЖЕ принятой операции. С включённым гейтом хвост
    аудитории молча переставал приглашаться после первого дня: проверено на живой
    базе, submit падал с PlanRequiredError и ошибка глоталась."""
    fn = _fn("_schedule_invite_continuation")
    assert "bypass_plan_check=True" in fn
    assert "не новая покупка" in fn, "решение должно быть объяснено в коде"


def test_chain_is_bounded():
    from services import invite_recovery, op_worker
    assert op_worker._MAX_INVITE_CHAIN >= 2, "одного продолжения мало для больших аудиторий"
    assert op_worker._MAX_INVITE_CHAIN <= 60, "забытая операция не должна жить месяцами"
    body = _exec_body()
    # Решение переехало в чистую функцию — предел передаётся ей явно.
    assert "max_chain=_MAX_INVITE_CHAIN" in body, "цепочка обязана иметь предел"
    assert invite_recovery.should_schedule_continuation(
        left=10, group_broken=False, all_failed_connect=False,
        chain=5, max_chain=5)[0] is False, "предел цепочки обязан срабатывать"
    assert '"invite_chain"' in _fn("_schedule_invite_continuation"), (
        "счётчик должен передаваться дальше, иначе предел не сработает"
    )


def test_no_continuation_when_the_problem_is_not_limits():
    """Границы сохранены, но правило про перегрев стало ТОЧНЕЕ.

    Раньше любой перегрев отменял продолжение — и остаток целей исчезал вместе с
    операцией (живой прогон: 203/380, остаток 177 потерян). Теперь различаем:
    перегрев ПОСЛЕ успехов = флот упёрся в потолок, лечится отдыхом, остаток
    уезжает на следующий запуск; перегрев БЕЗ единого успеха = флагнутый чат или
    аудитория, повтор жжёт аккаунты — продолжения нет.
    """
    from services import invite_recovery as ir

    def _c(**kw):
        base = dict(left=100, group_broken=False, all_failed_connect=False,
                    chain=0, max_chain=5)
        base.update(kw)
        return ir.should_schedule_continuation(**base)

    assert _c(group_broken=True)[0] is False, "закрытая группа: повтор бессмыслен"
    assert _c(flood_storm=True, ok_count=0)[0] is False, (
        "перегрев без единого успеха: повтор навредит")
    assert _c(flood_storm=True, ok_count=203)[0] is True, (
        "перегрев после успехов — временный лимит, цель нельзя бросать")


def test_no_continuation_after_cancel():
    body = _exec_body()
    seg = body[body.index("should_schedule_continuation"):][:500]
    assert "_is_cancelled" in seg, (
        "отменённая операция не имеет права воскресать продолжением"
    )


def test_failure_to_schedule_is_logged_not_swallowed():
    """Если продолжение не встало, остаток аудитории не будет приглашён — это
    должно быть видно в логах, а не только по косвенным признакам."""
    fn = _fn("_schedule_invite_continuation")
    assert "log.warning" in fn, "тихий проглот здесь стоит пользователю аудитории"
    assert "log_exc_swallow" not in fn


def test_continuation_starts_when_limits_reset():
    """Суточные счётчики считаются по CURRENT_DATE — раньше полуночи UTC лимит
    не обновится, и повтор просто снова упрётся в него."""
    fn = _fn("_schedule_invite_continuation")
    assert "timedelta(days=1)" in fn
    assert "hour=0" in fn and "CURRENT_DATE" in fn
