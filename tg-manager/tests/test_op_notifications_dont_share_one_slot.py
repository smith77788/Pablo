"""Итог операции не должен теряться из-за уведомления о прогрессе.

ЧТО БЫЛО. `db.notify_if_enabled` ограничивает частоту по тройке
(user_id, pref, dedup_key): один и тот же ключ срабатывает не чаще раза в
минуту, остальное МОЛЧА отбрасывается. Её докстринг прямо предупреждает:
логически разным событиям нужен свой dedup_key, иначе поток однотипных
уведомлений съедает слот и «большинство уведомлений теряется».

op_worker не передавал dedup_key НИ РАЗУ. Все его сообщения шли под одним
pref='op_complete' и с ключом None, то есть делили ОДИН слот на владельца:

  * «операция запущена»,
  * прогресс — каждые 30 секунд, по каждой из трёх параллельных операций,
  * вехи 25/50/75%,
  * объяснения пауз и блокировок,
  * и, самое важное, ИТОГ операции и сообщение о её падении.

Следствие видно на самых обычных сценариях. Короткая операция: сообщение о
старте занимает слот, операция заканчивается через 20 секунд — итог молча
выброшен, владелец не узнаёт, что она вообще завершилась. Долгая операция:
прогресс тикает каждые 30 секунд и держит слот занятым постоянно, поэтому
итог, падение и объяснение флуд-паузы попадают в эфир только случайно.

ФИКС: у каждого рода события свой ключ. Прогресс теперь вытесняет только
прогресс своей же операции, а итог, падение и блокировки имеют собственные
слоты и доходят всегда.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]
# Слой операций: движок очереди и движок DM-кампаний. Кампания — тоже операция
# (op_type=dm_campaign), её сообщения идут под тем же pref='op_complete' и без
# ключа делили бы ровно тот же слот.
_SRCS = (_ROOT / "services" / "op_worker.py", _ROOT / "services" / "dm_engine.py")
_SRC = _SRCS[0]


def _calls():
    out = []
    for path in _SRCS:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for n in ast.walk(tree):
            if not isinstance(n, ast.Call):
                continue
            fname = n.func.attr if isinstance(n.func, ast.Attribute) else ""
            if fname != "notify_if_enabled":
                continue
            key = next((kw.value for kw in n.keywords if kw.arg == "dedup_key"), None)
            out.append((f"{path.name}:{n.lineno}", key))
    return out


def test_every_notification_carries_its_own_dedup_key():
    missing = [ln for ln, key in _calls() if key is None]
    assert not missing, (
        "уведомление без dedup_key делит один слот в минуту со ВСЕМИ остальными "
        "сообщениями владельца — прогресс вытесняет итог операции: строки "
        + ", ".join(str(m) for m in missing)
    )


def test_notifications_are_found_at_all():
    """Пробник, который ничего не нашёл, ничего и не проверил."""
    assert len(_calls()) >= 14


def _src_text() -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in _SRCS)


def test_result_and_progress_use_different_slots():
    src = _src_text()
    assert 'dedup_key=f"done:{op_id}"' in src, "у итога операции обязан быть свой слот"
    assert 'dedup_key=f"fail:{op_id}"' in src, "у падения операции обязан быть свой слот"
    assert 'dedup_key=f"progress:{op_id}"' in src, "прогресс обязан вытеснять только прогресс"


def test_milestones_do_not_evict_each_other():
    """25, 50 и 75% — три разных события, а не одно повторяющееся."""
    src = _src_text()
    assert 'dedup_key=f"milestone:{op_id}:{milestone}"' in src


def test_keys_are_scoped_to_the_operation():
    """Общий ключ на все операции владельца вернул бы ту же болезнь в другом виде:
    три параллельные операции снова делили бы один слот."""
    src = _src_text()
    keys = re.findall(r'dedup_key=f"([^"]+)"', src)
    assert keys, "ключи не найдены"
    for k in keys:
        assert "{op_id}" in k or "{owner_id}" in k or "{campaign_id}" in k, (
            f"ключ {k!r} не привязан ни к операции, ни к владельцу"
        )


def test_dm_campaign_result_has_its_own_slot():
    """Кампания шлёт старт-отказ, вехи и итог — тоже под одним pref."""
    src = _src_text()
    assert 'dedup_key=f"dm-done:{campaign_id}"' in src
    assert 'dedup_key=f"dm-milestone:{campaign_id}:{_milestone}"' in src


def test_blocking_explanations_have_their_own_slots():
    """Объяснения пауз — это то, ради чего владелец НЕ пойдёт перезапускать
    операцию. Они не должны конкурировать ни с прогрессом, ни друг с другом."""
    src = _src_text()
    for key in ('flood-defer:{op_id}', 'no-accounts:{op_id}',
                'circuit:{owner_id}', 'retry:{op_id}:{retry_count}'):
        assert f'dedup_key=f"{key}"' in src, f"нет отдельного слота для {key}"
