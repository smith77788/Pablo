"""Повтор операции не должен публиковать второй раз туда, где пост уже есть.

Разрыв. Повтор запускает исполнителя ЗАНОВО с `done_items=0` — и автоматический
(`_maybe_requeue` после FloodWait или сетевого сбоя), и ручной. Исполнитель
проходит ВЕСЬ список целей сначала, включая те, куда пост уже ушёл на прошлом
прогоне. Для постинга это вторая публикация в канал: мусор у подписчиков,
сожжённый лимит аккаунта и лишний повод для флуда — то есть прямой удар по
анти-детекту, ради которого продукт и существует.

Второй разрыв, он же причина, по которой первый нельзя было заметить:
`_exec_mass_publish` писал в `operation_log.target` РАЗНЫЕ ключи — заголовок
канала при успехе и channel_id при провале. Сопоставить «уже опубликовано» с
«упало» было нечем, поэтому и ручной повтор («повторить неудавшиеся») брал
каналы вслепую: канал, упавший на первом аккаунте и прошедший на резервном,
попадал в повтор и получал вторую публикацию.

`operation_bus.collect_failed_targets` это правило соблюдает и объясняет прямым
текстом; у mass_publish собственный обработчик повтора, и там правила не было.

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
    start = src.index(f"async def {name}(")
    m = re.search(r"\n(?:async )?def ", src[start + 10:])
    return src[start:start + 10 + m.start()] if m else src[start:]


# ── Канонический ключ цели в журнале ─────────────────────────────────────────

def test_success_and_failure_log_the_same_target_key():
    """Иначе «уже опубликовано» нечем сопоставить с «упало»."""
    body = _fn(_read("services/op_worker.py"), "_exec_mass_publish")
    inserts = re.findall(
        r"INSERT INTO operation_log\(op_id, step_num, target, status[^)]*\)"
        r".*?\n(?:.*?\n)*?\s*op_id,\s*\n?\s*idx,\s*\n?\s*([^,\n]+),",
        body,
    )
    assert inserts, "записи в operation_log не найдены"
    keys = {k.strip() for k in inserts}
    assert keys == {'str(dialog["id"])'}, (
        f"target должен быть channel_id во ВСЕХ ветках, найдено: {keys}"
    )


def test_channel_title_is_kept_for_humans():
    """Читаемость не приносим в жертву: заголовок переезжает в message."""
    body = _fn(_read("services/op_worker.py"), "_exec_mass_publish")
    assert "VALUES($1,$2,$3,'ok',$4)" in body
    assert "_ch_title" in body


# ── Идемпотентность автоматического повтора ──────────────────────────────────

def test_helper_reads_only_successful_steps():
    ow = _read("services/op_worker.py")
    body = _fn(ow, "completed_targets")
    assert "status='ok'" in body, "уже-сделанными считаются только успешные шаги"
    # Скоуп сохраняется, но теперь он — ЦЕПОЧКА повторов: у операции,
    # поставленной кнопкой «Повторить», свой новый id, а журнал уже сделанной
    # работы лежит под id предка (op_worker.journal_op_ids). Неограниченное
    # чтение журнала по-прежнему запрещено.
    assert "op_id = ANY($1::bigint[])" in body, (
        "выборка обязана быть скоуплена операцией и её предками-повторами"
    )
    assert "journal_op_ids(pool, op_id)" in body, (
        "скоуп собран не по цепочке повторов"
    )
    assert "DISTINCT" in body


def test_mass_publish_skips_already_published_channels():
    body = _fn(_read("services/op_worker.py"), "_exec_mass_publish")
    assert "_already_published = await completed_targets(pool, op_id)" in body, (
        "повтор обязан знать, куда пост уже ушёл"
    )
    assert 'if str(dialog["id"]) in _already_published:' in body, (
        "пропуск должен сравниваться тем же ключом, что пишется в журнал"
    )
    # Пропуск обязан быть ВНУТРИ цикла по целям и до самой публикации.
    loop = body.index("for idx, target_entry in enumerate(targets, 1):")
    assert body.index("_already_published = await") < loop
    assert loop < body.index('if str(dialog["id"]) in _already_published:')


def test_skipped_channel_counts_as_success():
    """Иначе повтор отчитается «1 из 60» на уже выполненной работе."""
    body = _fn(_read("services/op_worker.py"), "_exec_mass_publish")
    skip = body[body.index('if str(dialog["id"]) in _already_published:'):]
    skip = skip[:skip.index("continue")]
    assert "ok_count += 1" in skip, "пропущенный канал — это достигнутая цель"
    assert "fail_count" not in skip, "пропуск не провал"
    assert "done_items=done_items+1" in skip, "прогресс обязан двигаться и на пропуске"


def test_first_run_behaviour_is_unchanged():
    """На первом прогоне журнал пуст — множество пустое, ничего не пропускается."""
    body = _fn(_read("services/op_worker.py"), "completed_targets")
    # Никаких фолбэков «считать сделанным при ошибке чтения»: ошибка выборки
    # обязана давать ПУСТОЕ множество, иначе сбой БД тихо отменит публикацию.
    assert "_safe_fetch(" in body
    assert "or []" in body, "пустой/сбойный результат обязан давать пустое множество"


# ── Ручной повтор «повторить неудавшиеся» ────────────────────────────────────

def test_manual_retry_excludes_channels_that_succeeded():
    """Канал, упавший на одном аккаунте и прошедший на резервном, — не «упавший»."""
    body = _fn(_read("bot/handlers/mass_publish.py"), "cb_mpub_retry_failed")
    assert "failed_ids = [c for c in failed_ids if c not in published]" in body, (
        "успешные каналы обязаны исключаться из повтора — иначе вторая публикация"
    )
    assert "status IN ('ok','error')" in body, (
        "чтобы исключить успешные, их надо сначала прочитать"
    )


def test_manual_retry_matches_bus_rule():
    """Правило обязано совпадать с operation_bus.collect_failed_targets."""
    bus = _fn(_read("services/operation_bus.py"), "collect_failed_targets")
    assert "if key not in succeeded" in bus, "опорное правило в шине изменилось"
    handler = _fn(_read("bot/handlers/mass_publish.py"), "cb_mpub_retry_failed")
    assert "published" in handler and "not in published" in handler


def test_manual_retry_still_parses_negative_channel_ids():
    """channel_id в Telegram отрицательный — фильтр не должен их терять."""
    body = _fn(_read("bot/handlers/mass_publish.py"), "cb_mpub_retry_failed")
    assert 'lstrip("-").isdigit()' in body

    # Поведение парсера проверяем напрямую, а не только по тексту.
    def _chan_id(raw):
        t = (raw or "").strip()
        return int(t) if t.lstrip("-").isdigit() else None

    assert _chan_id("-1001234567890") == -1001234567890
    assert _chan_id("1001234567890") == 1001234567890
    assert _chan_id("Мой канал") is None
    assert _chan_id("") is None
    assert _chan_id(None) is None


def test_manual_retry_keeps_order_and_dedupes():
    body = _fn(_read("bot/handlers/mass_publish.py"), "cb_mpub_retry_failed")
    assert "elif cid not in failed_ids:" in body, "повтор не должен дублировать цели"
