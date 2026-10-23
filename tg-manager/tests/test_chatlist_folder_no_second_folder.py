"""Повтор операции не должен создавать в Telegram вторую такую же папку.

Механика дефекта. Общая папка собирается в два шага по сети: сначала папка
создаётся у аккаунта (`UpdateDialogFilterRequest`), затем к ней экспортируется
chatlist-ссылка (`ExportChatlistInviteRequest`). Самый частый отказ продукта
приходится ровно на второй шаг: Telegram требует Premium именно для ШАРИНГА
папки.

Повтор операции здесь штатный: `_maybe_requeue` после сетевой ошибки, сброс
зависшей операции сторожем, перезапуск контейнера (рабочая ветка едет на
Railway, перезапуск — на каждом деплое). Исполнитель шёл с начала и брал
СЛЕДУЮЩИЙ свободный номер папки, то есть создавал у аккаунта вторую папку с тем
же названием. Потом третью. Папок у аккаунта конечное число, в базе хранится
ссылка на одну, и осиротевшие владелец не видит и отозвать не может.

Схема это предусматривала с самого начала: комментарий к `filter_id` в
`schema_v199_chatlist_folders.sql` прямо говорит, что номер нужен «для
повторного экспорта/отзыва ссылки той же папки, а не создания новой при каждом
клике», и параметр `existing_filter_id` у `create_shared_folder_link` для этого
есть. Его просто никто не передавал, а при сбое номер и вовсе не сохранялся.

Тест держит обе половины: номер доезжает до базы даже при сбое, и исполнитель
его читает — и ссылку, если она уже выдана.
"""
from __future__ import annotations

import ast
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _func(path: str, name: str) -> str:
    with open(path, encoding="utf-8") as f:
        src = f.read()
    node = next(n for n in ast.walk(ast.parse(src))
                if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
                and n.name == name)
    return "\n".join(src.split("\n")[node.lineno - 1:node.end_lineno])


_OW = os.path.join(ROOT, "services", "op_worker.py")
_AM = os.path.join(ROOT, "services", "account_manager.py")
_DB = os.path.join(ROOT, "database", "db.py")


# ── Готовая ссылка не пересоздаётся ──────────────────────────────────────────

def test_executor_returns_the_existing_link_without_touching_telegram():
    body = _func(_OW, "_exec_create_chatlist_folder")
    assert "FROM chatlist_folders" in body, (
        "исполнитель не читает строку папки — повтор пойдёт создавать новую")
    assert '"ready"' in body and "invite_link" in body, (
        "готовая ссылка не распознаётся как уже сделанная работа")
    # Чтение обязано быть ДО создания, иначе оно ничего не предотвращает.
    # Сравниваем с самим ВЫЗОВОМ: имя хелпера встречается ещё и в пояснении
    # выше, и по нему позиция получилась бы раньше чтения — измеритель соврал бы.
    assert body.index("FROM chatlist_folders") < body.index(
        "await account_manager.create_shared_folder_link("), (
        "строка папки читается после создания — проверка бесполезна")


def test_executor_reuses_the_folder_when_only_the_link_failed():
    body = _func(_OW, "_exec_create_chatlist_folder")
    assert "existing_filter_id=" in body, (
        "номер уже созданной папки не передаётся — рядом встанет вторая")


# ── Номер папки доезжает до базы даже при сбое ───────────────────────────────

def test_failure_result_carries_the_filter_id():
    body = _func(_AM, "create_shared_folder_link")
    # Номер запоминается сразу после создания папки, то есть ДО экспорта ссылки.
    assert '_created["filter_id"] = fid' in body
    assert body.index('_created["filter_id"] = fid') < body.index(
        "ExportChatlistInviteRequest"), (
        "номер запоминается после экспорта — при отказе Premium он снова "
        "потеряется, а это и есть самый частый отказ")
    # И отдаётся наружу в ветках отказа, а не только при успехе.
    tail = body[body.index("except FloodWaitError"):]
    assert tail.count('"filter_id"') >= 2, (
        "ветки отказа не возвращают номер папки")


def test_db_persists_filter_id_on_failure():
    body = _func(_DB, "set_chatlist_folder_result")
    failed = body[body.index("status='failed'"):]
    assert "filter_id" in failed, (
        "при сбое номер папки не сохраняется — повтор создаст вторую")
    assert "COALESCE" in failed, (
        "отказ ДО создания папки затрёт уже известный номер")


# ── Сторож самой проверки ────────────────────────────────────────────────────

def test_the_reuse_parameter_really_exists():
    """Передавать `existing_filter_id` бессмысленно, если хелпер его не знает."""
    body = _func(_AM, "create_shared_folder_link")
    assert "existing_filter_id" in body.split("\n")[0:6][-1] or \
           "existing_filter_id" in body[:body.index('"""')], (
        "параметр переиспользования исчез из подписи хелпера")
    assert "int(existing_filter_id) if existing_filter_id else 0" in body, (
        "параметр есть в подписи, но на выбор номера не влияет")


def test_schema_still_documents_the_intent():
    """Обоснование живёт в схеме — если его убрали, фикс выглядит произвольным."""
    path = os.path.join(ROOT, "schema_v199_chatlist_folders.sql")
    with open(path, encoding="utf-8") as f:
        sql = f.read()
    assert "filter_id" in sql and "chatlist_folders" in sql
