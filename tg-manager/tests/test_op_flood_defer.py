"""Длинную флуд-паузу Telegram нельзя пересиживать внутри прогона.

Разрыв. Поймав FloodWait, `_exec_mass_publish` спал ровно столько, сколько
попросил Telegram — без какого-либо предела. Telegram отдаёт FloodWait и на
десятки секунд, и на часы. Всё это время прогон держал слот параллельности
(один из восьми) и арендованные аккаунты, не делая ничего: флот простаивал, а
очередь владельца не двигалась. С появлением потолка прогона такой сон ещё и
съедал его целиком, обрывая всю операцию.

Правильное поведение: короткую паузу переждать на месте (дёшево), длинную —
отложить операцию целиком, освободив слот и аккаунты, и возобновить её, когда
пауза истечёт. Возобновление безопасно: исполнитель пропускает уже
опубликованные каналы по operation_log, так что дублей нет.

Отложить обязан ОТДЕЛЬНЫЙ путь: `_requeue_op_no_accounts` проваливает операцию,
если та ждёт дольше `_ACCT_WAIT_MAX_MIN` от создания. Для ожидания свободных
аккаунтов это верно, а провалить операцию за то, что Telegram попросил
подождать час, — значит наказать за соблюдение правил платформы.

Защитные механизмы Telegram при этом не обходятся: пауза выдерживается
полностью, меняется только то, чем занят воркер, пока она идёт.

op_worker импортирует telethon и в тестовой среде не поднимается — связку
проверяем по исходнику, как и соседние тесты очереди.
"""
from __future__ import annotations

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _fn(src: str, name: str) -> str:
    start = src.index(f"async def {name}")
    m = re.search(r"\n(?:async )?def ", src[start + 10:])
    return src[start:start + 10 + m.start()] if m else src[start:]


def _code(src: str, name: str) -> str:
    """Тело функции БЕЗ докстроки — иначе объяснение в прозе читается как код."""
    body = _fn(src, name)
    m = re.search(r'"""', body)
    if not m:
        return body
    end = body.index('"""', m.end())
    return body[:m.start()] + body[end + 3:]


# ── Порог ────────────────────────────────────────────────────────────────────

def test_inline_flood_threshold_is_configurable_and_bounded():
    ow = _read("services/op_worker.py")
    m = re.search(
        r'_FLOOD_INLINE_MAX_S = _int_env\("OP_FLOOD_INLINE_MAX_SEC", ([^,]+), ([^,]+), ([^)]+)\)',
        ow)
    assert m, "порог пересиживания флуда не найден"
    default, lo, hi = (eval(g.strip()) for g in m.groups())  # noqa: S307 - свои же литералы
    assert lo <= default <= hi
    assert default <= 3600, (
        "пересиживать больше часа бессмысленно — слот и аккаунты простаивают"
    )
    assert lo >= 30, (
        "слишком низкий порог отложит операцию на каждой короткой паузе, "
        "а перезапуск дороже, чем переждать полминуты"
    )


def test_threshold_is_below_the_run_ceiling():
    """Иначе сон успеет съесть потолок прогона и оборвать операцию целиком."""
    ow = _read("services/op_worker.py")
    inline = eval(re.search(  # noqa: S307
        r'_FLOOD_INLINE_MAX_S = _int_env\("OP_FLOOD_INLINE_MAX_SEC", ([^,]+),', ow).group(1))
    ceiling = eval(re.search(  # noqa: S307
        r'_OP_TIMEOUT_DEFAULT_S = _int_env\("OP_TIMEOUT_SEC", ([^,]+),', ow).group(1))
    assert inline < ceiling, (inline, ceiling)


# ── Отложенный перезапуск ────────────────────────────────────────────────────

def test_defer_releases_the_slot_by_returning_to_pending():
    body = _fn(_read("services/op_worker.py"), "_defer_op_for_flood")
    assert "status='pending'" in body
    assert "started_at=NULL" in body
    assert "scheduled_for = now() + make_interval(secs => $2)" in body


def test_defer_resets_progress_counter():
    """Иначе счётчик копится поверх прошлого прогона — класс «done > total»."""
    body = _fn(_read("services/op_worker.py"), "_defer_op_for_flood")
    assert "done_items=0" in body


def test_defer_never_fails_the_operation():
    """Провалить операцию за назначенную Telegram паузу — наказание за правила."""
    code = _code(_read("services/op_worker.py"), "_defer_op_for_flood")
    assert "status='failed'" not in code
    assert "_ACCT_WAIT_MAX_MIN" not in code, (
        "отсчёт «ждём свободные аккаунты» к флуд-паузе неприменим"
    )


def test_defer_is_a_separate_path_from_fleet_wait():
    """Путь ожидания флота обязан сохранить свой предел — его не смешиваем."""
    fleet = _fn(_read("services/op_worker.py"), "_requeue_op_no_accounts")
    assert "_ACCT_WAIT_MAX_MIN" in fleet
    assert "status='failed'" in fleet


def test_defer_explains_itself_to_the_owner():
    body = _fn(_read("services/op_worker.py"), "_defer_op_for_flood")
    assert "last_error=$3" in body, "причина отсрочки должна быть видна владельцу"


def test_worker_honours_defer_seconds():
    body = _fn(_read("services/op_worker.py"), "_run_op_task")
    seg = body[body.index('if result.get("status") == "requeue":'):]
    seg = seg[:seg.index("return") + 20]
    assert '_defer_s = result.get("defer_s")' in seg
    assert "_defer_op_for_flood(" in seg
    assert "_requeue_op_no_accounts(pool, op_id)" in seg, (
        "возврат без defer_s обязан идти прежним путём"
    )


# ── mass_publish ─────────────────────────────────────────────────────────────

def test_short_flood_is_still_slept_through():
    """Перезапуск дороже, чем переждать короткую паузу на месте."""
    body = _fn(_read("services/op_worker.py"), "_exec_mass_publish")
    assert "await asyncio.sleep(flood_wait + random.uniform(2, 8))" in body


def test_long_flood_defers_instead_of_sleeping():
    body = _fn(_read("services/op_worker.py"), "_exec_mass_publish")
    assert "if flood_wait > _FLOOD_INLINE_MAX_S:" in body
    seg = body[body.index("if flood_wait > _FLOOD_INLINE_MAX_S:"):]
    seg = seg[:seg.index("await asyncio.sleep(flood_wait")]
    assert '"status": "requeue"' in seg
    assert '"defer_s": flood_wait + 60' in seg, (
        "ждать надо всю паузу плюс запас — иначе вернёмся в тот же флуд"
    )
    assert "release_accounts(mp_used_acc_ids)" in seg, (
        "смысл отсрочки — отпустить флот; без этого слот освободится, а аккаунты нет"
    )


def test_defer_waits_at_least_as_long_as_telegram_asked():
    """Защитные механизмы платформы не обходим: пауза выдерживается полностью."""
    body = _fn(_read("services/op_worker.py"), "_exec_mass_publish")
    seg = body[body.index("if flood_wait > _FLOOD_INLINE_MAX_S:"):]
    seg = seg[:seg.index("await asyncio.sleep(flood_wait")]
    m = re.search(r'"defer_s": flood_wait \+ (\d+)', seg)
    assert m and int(m.group(1)) > 0, "отсрочка обязана быть НЕ короче назначенной паузы"

    defer_body = _fn(_read("services/op_worker.py"), "_defer_op_for_flood")
    assert "max(1, int(wait_s))" in defer_body, "отсрочка не должна схлопываться в ноль"


def test_flood_is_still_recorded_before_deferring():
    """Штраф аккаунту записывается в любом случае — иначе пейсинг не учится."""
    body = _fn(_read("services/op_worker.py"), "_exec_mass_publish")
    idx_record = body.index("record_flood(")
    idx_defer = body.index("if flood_wait > _FLOOD_INLINE_MAX_S:")
    assert idx_record < idx_defer, "запись флуда обязана идти до отсрочки"


def test_resume_after_defer_does_not_duplicate_posts():
    """Отсрочка возобновляет операцию — без пропуска уже взятых целей это дубли."""
    body = _fn(_read("services/op_worker.py"), "_exec_mass_publish")
    assert "_already_published = await completed_targets(pool, op_id)" in body
