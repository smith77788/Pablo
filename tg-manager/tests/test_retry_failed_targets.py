"""Точечный повтор упавших целей массовой операции.

Зачем механизм: частичный провал (400 каналов, 27 упали по FloodWait) без него
означает перезапуск ВСЕЙ операции — лишний расход лимитов аккаунтов и повторная
обработка успешных целей, то есть дубли постов и повторные вступления.

Почему механизм намеренно узкий: `operation_log.target` у большинства операций
— человекочитаемая метка («acc#123», заголовок канала), из которой цель не
восстановить, либо вообще ИСПОЛНИТЕЛЬ, а не цель. Поэтому кнопка появляется
только у op_type, объявивших `retry_targets`. Молча угадывать нельзя: повтор
ушёл бы не по тем объектам, а операция отчиталась бы об успехе.

Здесь защищается: корректность разбора, отсутствие ложных повторов и
согласованность деклараций реестра с реальным кодом исполнителей.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from services import operation_bus as ob

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "services" / "op_worker.py"
MASS_OPS = ROOT / "bot" / "handlers" / "mass_ops.py"


class _FakePool:
    """Минимальный пул: отдаёт заданные строки operation_log."""

    def __init__(self, rows, op_row=None):
        self.rows = rows
        self.op_row = op_row
        self.submitted = []

    async def fetch(self, query, *args):
        return self.rows

    async def fetchrow(self, query, *args):
        return self.op_row


# ── Разбор operation_log.target ─────────────────────────────────────────────

_INT_META = {"param": "channel_ids", "prefix": "ch#", "kind": "int"}
_STR_META = {"param": "channels", "kind": "str"}


@pytest.mark.parametrize(
    "raw,meta,expected",
    [
        ("ch#123", _INT_META, 123),
        ("ch#-100500", _INT_META, -100500),   # channel_id бывает отрицательным
        ("t.me/joinchat/AAA", _STR_META, "t.me/joinchat/AAA"),
        ("  ch#7  ", _INT_META, 7),
    ],
)
def test_parse_valid_targets(raw, meta, expected):
    assert ob.parse_log_target(raw, meta) == expected


@pytest.mark.parametrize(
    "raw,meta",
    [
        ("acc#7", _INT_META),          # исполнитель, а не цель — чужой префикс
        ("ch#abc", _INT_META),         # нечисловой хвост
        ("123", _INT_META),            # префикса нет
        ("Новости Москвы", _INT_META), # человекочитаемая метка
        ("", _INT_META),
        (None, _STR_META),
    ],
)
def test_parse_rejects_unparseable(raw, meta):
    """Не разобралось — отбрасываем, а не угадываем.

    Мусор в списке целей хуже короткого списка: исполнитель либо молча ничего
    не сделает, либо ударит не по тому объекту.
    """
    assert ob.parse_log_target(raw, meta) is None


# ── Сбор упавших целей ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_target_succeeded_elsewhere_is_not_retried():
    """Главная защита от дублей.

    Один канал может упасть на одном аккаунте и пройти на другом. Если
    повторить его, пользователь получит вторую публикацию в уже успешный
    канал — именно то, ради чего затевался точечный повтор.
    """
    rows = [
        {"target": "ch#1", "status": "error"},
        {"target": "ch#2", "status": "error"},
        {"target": "ch#2", "status": "ok"},
        {"target": "ch#3", "status": "ok"},
    ]
    got = await ob.collect_failed_targets(_FakePool(rows), 1, "bulk_seo_apply")
    assert got == [1]


@pytest.mark.asyncio
async def test_duplicate_errors_collapse():
    rows = [
        {"target": "ch#5", "status": "error"},
        {"target": "ch#5", "status": "error"},
        {"target": "ch#6", "status": "error"},
    ]
    got = await ob.collect_failed_targets(_FakePool(rows), 1, "bulk_seo_apply")
    assert got == [5, 6]


@pytest.mark.asyncio
async def test_unparseable_rows_do_not_break_collection():
    rows = [
        {"target": "мусор", "status": "error"},
        {"target": None, "status": "error"},
        {"target": "ch#9", "status": "error"},
    ]
    got = await ob.collect_failed_targets(_FakePool(rows), 1, "bulk_seo_apply")
    assert got == [9]


@pytest.mark.asyncio
async def test_unsupported_op_type_returns_nothing():
    rows = [{"target": "acc#1", "status": "error"}]
    assert await ob.collect_failed_targets(_FakePool(rows), 1, "strike") == []


@pytest.mark.asyncio
async def test_collection_is_fail_open_on_db_error():
    class Broken:
        async def fetch(self, *a):
            raise RuntimeError("БД недоступна")

    # Сбой вспомогательного запроса не должен ронять экран операции.
    assert await ob.collect_failed_targets(Broken(), 1, "bulk_seo_apply") == []


# ── Постановка повтора ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_retry_replaces_only_target_field(monkeypatch):
    """Повтор обязан повторять ТУ ЖЕ операцию: меняется только список целей.

    Текст, задержки, выбранные аккаунты переносятся как есть — иначе повтор
    будет другой операцией, а пользователь ждёт ту же.
    """
    captured = {}

    async def fake_submit(pool, owner_id, op_type, params, **kw):
        captured.update(params=params, op_type=op_type, kw=kw)
        return 555

    monkeypatch.setattr(ob, "submit", fake_submit)
    rows = [{"target": "ch#1", "status": "error"}, {"target": "ch#2", "status": "error"}]
    pool = _FakePool(
        rows,
        op_row={
            "owner_id": 42,
            "op_type": "bulk_seo_apply",
            "params": {"channel_ids": [1, 2, 3, 4], "seo_profile": "x", "delay": 7},
        },
    )
    res = await ob.submit_retry_failed(pool, 42, 10)

    assert res["ok"] and res["op_id"] == 555 and res["count"] == 2
    assert captured["params"]["channel_ids"] == [1, 2]      # только упавшие
    assert captured["params"]["seo_profile"] == "x"          # прочее сохранено
    assert captured["params"]["delay"] == 7
    assert captured["params"]["retry_of_op"] == 10           # след происхождения
    assert captured["kw"]["total_items"] == 2


@pytest.mark.asyncio
async def test_alt_param_cleared_so_retry_is_not_full_run(monkeypatch):
    """Регресс-ловушка: bulk_join читает `links` ИЛИ `targets`.

    Если оставить старое значение альтернативного поля, исполнитель прочитает
    его первым и повтор пойдёт по ПОЛНОМУ списку целей — то есть повторно
    вступит в каналы, где уже состоит.
    """
    captured = {}

    async def fake_submit(pool, owner_id, op_type, params, **kw):
        captured.update(params=params)
        return 1

    monkeypatch.setattr(ob, "submit", fake_submit)
    rows = [{"target": "t.me/a", "status": "error"}]
    pool = _FakePool(
        rows,
        op_row={
            "owner_id": 1,
            "op_type": "bulk_join",
            "params": {"targets": ["t.me/a", "t.me/b", "t.me/c"]},
        },
    )
    await ob.submit_retry_failed(pool, 1, 3)
    assert captured["params"]["links"] == ["t.me/a"]
    assert "targets" not in captured["params"]


@pytest.mark.asyncio
async def test_retry_refuses_foreign_operation():
    pool = _FakePool([], op_row={"owner_id": 999, "op_type": "bulk_join", "params": {}})
    res = await ob.submit_retry_failed(pool, 1, 5)
    assert not res["ok"] and "не найдена" in res["reason"]


@pytest.mark.asyncio
async def test_retry_reports_distinct_refusal_reasons():
    """«Повторять нечего» и «тип не поддержан» — разные ответы.

    Слив их в одно сообщение оставил бы пользователя в догадках, что именно
    произошло с его операцией.
    """
    nothing = await ob.submit_retry_failed(
        _FakePool([], op_row={"owner_id": 1, "op_type": "bulk_join", "params": {}}), 1, 5
    )
    unsupported = await ob.submit_retry_failed(
        _FakePool([], op_row={"owner_id": 1, "op_type": "strike", "params": {}}), 1, 5
    )
    assert not nothing["ok"] and not unsupported["ok"]
    assert nothing["reason"] != unsupported["reason"]


# ── Гейт: декларации не должны разойтись с кодом исполнителей ───────────────

def _executor_src(name: str) -> str:
    src = WORKER.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    return ""


# op_type → имя исполнителя в op_worker. Держится вручную: автоматический
# вывод по dispatch дал бы ложную уверенность.
_EXECUTORS = {
    "bulk_seo_apply": "_exec_bulk_seo_apply",
    "bulk_join": "_exec_bulk_join_inner",
    "bulk_leave": "_exec_bulk_leave",
}


def test_every_declared_op_has_known_executor():
    declared = {op for op in ob.OP_REGISTRY if ob.supports_retry_failed(op)}
    assert declared, "ни одна операция не объявила retry_targets — механизм мёртв"
    missing = declared - set(_EXECUTORS)
    assert not missing, (
        f"объявлен retry_targets без сверки с исполнителем: {sorted(missing)}. "
        "Добавьте исполнителя в _EXECUTORS и проверьте формат operation_log.target — "
        "иначе повтор пойдёт не по тем целям."
    )


@pytest.mark.parametrize("op_type", sorted(_EXECUTORS))
def test_declared_param_is_actually_read_by_executor(op_type):
    """Поле params из декларации обязано читаться исполнителем.

    Иначе повтор поставит операцию с целями в поле, которое никто не смотрит:
    исполнитель возьмёт пустой/старый список и отчитается об успехе.
    """
    meta = ob.retry_targets_meta(op_type)
    src = _executor_src(_EXECUTORS[op_type])
    assert src, f"исполнитель {_EXECUTORS[op_type]} не найден"
    names = {meta["param"], *(meta.get("alt_params") or ())}
    assert any(f'params.get("{n}"' in src for n in names), (
        f"{op_type}: ни одно из полей {names} не читается в {_EXECUTORS[op_type]}"
    )


@pytest.mark.parametrize("op_type", sorted(_EXECUTORS))
def test_declared_prefix_matches_executor_log_format(op_type):
    """Префикс из декларации обязан встречаться в коде записи лога.

    Это гейт от тихого протухания: кто-то меняет формат target в исполнителе,
    декларация остаётся прежней — и повтор начинает отбрасывать все цели
    (в лучшем случае) или брать чужие (в худшем).
    """
    meta = ob.retry_targets_meta(op_type)
    prefix = meta.get("prefix")
    if not prefix:
        pytest.skip("формат без префикса — проверяется тестами разбора")
    src = _executor_src(_EXECUTORS[op_type])
    assert prefix in src, (
        f"{op_type}: префикс {prefix!r} не найден в {_EXECUTORS[op_type]} — "
        "формат operation_log.target изменился, декларация протухла"
    )


def test_executor_writes_error_rows_for_declared_ops():
    """У объявленной операции должен быть per-target лог ошибок.

    Без записей status='error' повтор всегда будет пустым, а кнопка —
    обещанием, которое нечем выполнить.
    """
    for op_type, fn in _EXECUTORS.items():
        src = _executor_src(fn)
        assert "INSERT INTO operation_log" in src, f"{op_type}: нет записи в operation_log"
        assert "'error'" in src, f"{op_type}: не пишет ошибки по целям"


# ── Проводка в UI ───────────────────────────────────────────────────────────

def test_handler_exists_for_retry_targets():
    src = MASS_OPS.read_text(encoding="utf-8")
    assert 'F.action == "retry_targets"' in src
    assert "submit_retry_failed" in src


def test_worker_offers_button_only_for_supported_types():
    src = WORKER.read_text(encoding="utf-8")
    assert "operation_bus.supports_retry_failed(op_type)" in src, (
        "кнопка повтора обязана зависеть от декларации, а не от списка "
        "op_type в условии — иначе она появится там, где повтор неверен"
    )


def test_op_detail_screen_offers_retry():
    # Экран деталей — единственное место, где видны сами упавшие цели.
    src = MASS_OPS.read_text(encoding="utf-8")
    detail = src[src.find('F.action == "op_detail"') :]
    assert "collect_failed_targets" in detail
    assert 'action="retry_targets"' in detail or "retry_targets" in detail
