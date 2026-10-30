"""Прерванная на полпути работа не закрывается как «выполнено».

Исход операции считает `op_status.classify_final`, и считает ПО СЧЁТЧИКАМ: так
и задумано — слово исполнителя легко соврёт, ok/failed врут хуже. Но у правила
был провал ровно в опасную сторону: прямое `failed` исполнителя классификатор
слышал даже с пустыми счётчиками, а прямое `partial` — нет, и `partial` с
ok=0/failed=0 превращался в `done`.

Беда не умозрительная. `_exec_contacts_sync` снимается таймаутом на 20-й
минуте; `asyncio.wait_for` отменяет `sync_all_accounts`, и её результат теряется
ЦЕЛИКОМ, хотя контакты коммитились по одному и часть флота уже сохранена.
Счётчиков у этого возврата нет и быть не может — собирать их было некому.
Собственный комментарий ветки объясняет, почему здесь `partial`, а не `done`:
иначе синхронизация стоит в списке операций успешной, а всё, что считает
неудачи по статусу (сводки, здоровье, разбор «что не доехало»), срыва не видит
вовсе. Ровно это и происходило: слово терялось на классификаторе.

Существующий храповик (`test_an_operation_outcome_is_a_word_the_product_knows`)
эту ветку пропускал, потому что требовал НАЛИЧИЯ ключей `ok`/`failed` в
возврате, а они здесь есть — оба нулевые. Перепись читалась сильнее, чем
работала.
"""

import ast
import asyncio
import inspect
import textwrap

import pytest

from services import op_status
from services import op_worker as W


# ── Сам классификатор ────────────────────────────────────────────────────────

def test_an_explicit_partial_survives_empty_counters():
    """Та самая тройка из ветки таймаута."""
    assert op_status.classify_final("partial", ok=0, failed=0) == op_status.PARTIAL


def test_an_explicit_partial_survives_clean_counters():
    """Форма оборванной рассылки: доставлено часть, провалов не записано.

    `"partial" if sent else "failed"` с ok=sent и failed=0 — и прогресс в
    очереди при этом может выглядеть добранным (его дописывает не исполнитель).
    """
    assert op_status.classify_final(
        "partial", ok=5, failed=0, done_items=5, total_items=5,
    ) == op_status.PARTIAL


def test_counters_can_still_lower_the_outcome():
    """Счётчики сильнее слова — но только ВНИЗ."""
    assert op_status.classify_final("partial", ok=0, failed=3) == op_status.FAILED


@pytest.mark.parametrize("said", [None, "done", "skipped", ""])
def test_silence_about_trouble_is_still_a_success(said):
    """Обслуживающая операция без целей по-прежнему честно «выполнена»."""
    assert op_status.classify_final(said, ok=0, failed=0) == op_status.DONE


# ── Ветка, из которой беда пришла ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_timed_out_contacts_sync_closes_as_partial(monkeypatch):
    """Исполнитель отдаёт «частично», и это слово доходит до очереди.

    Беду создаёт `asyncio.wait_for`, бросая TimeoutError; здесь её бросает сама
    `sync_all_accounts` — для проверяемой ветки это одна и та же дверь (её тело
    и есть `except asyncio.TimeoutError`), а ждать 20 минут настоящего таймаута
    в проверке нечем.
    """
    import services.contacts_hub.sync_service as _sync

    async def times_out(pool, owner_id):
        raise asyncio.TimeoutError

    monkeypatch.setattr(_sync, "sync_all_accounts", times_out)
    monkeypatch.setattr(W, "_reliability_metric", lambda *a, **k: None)

    res = await W._exec_contacts_sync(None, 4242, 777, {})

    assert res["status"] == "partial", (
        "синхронизация оборвалась на полпути, а исход говорит иначе")
    assert op_status.classify_final(
        res["status"], ok=res.get("ok", 0), failed=res.get("failed", 0),
    ) == op_status.PARTIAL, (
        "слово исполнителя не доходит до очереди: классификатор превращает "
        "оборванную синхронизацию в «выполнено»")


def test_the_timeout_branch_really_has_no_counters_to_give():
    """Фикс нельзя подменить «пусть исполнитель заполнит счётчики».

    Заполнять нечем: результат `sync_all_accounts` потерян вместе с отменённой
    задачей. Если однажды в этой ветке появятся НЕнулевые ok/failed — значит
    счётчики откуда-то взялись, и эту проверку надо перечитать, а не поправить.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(W._exec_contacts_sync)))
    zeroed = []
    for n in ast.walk(tree):
        if not isinstance(n, ast.Dict):
            continue
        keys = {k.value: v for k, v in zip(n.keys, n.values)
                if isinstance(k, ast.Constant)}
        said = keys.get("status")
        if not (isinstance(said, ast.Constant) and said.value == "partial"):
            continue
        zeroed.append(tuple(
            keys[c].value if isinstance(keys.get(c), ast.Constant) else "?"
            for c in ("ok", "failed")))
    assert zeroed == [(0, 0)], (
        f"ветка таймаута вернула иные счётчики: {zeroed}")
