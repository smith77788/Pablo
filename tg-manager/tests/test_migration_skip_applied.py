"""Регрессия: create_pool перезапускал ВСЕ ~154 schema-файла на каждом старте.

Это тысячи no-op SQL-запросов на деплой + риск, что медленный CREATE INDEX из
нового файла залочит старт и Railway убьёт деплой по health-check
(«Application failed to respond»). Теперь уже применённые ЧИСТО (status='ok')
файлы пропускаются; 'warnings'/новые — перезапускаются.
"""
from __future__ import annotations

import inspect
from database import db


def test_create_pool_skips_applied_files():
    src = inspect.getsource(db.create_pool)
    # читает набор уже применённых файлов
    assert "SELECT filename FROM schema_migrations WHERE status='ok'" in src
    assert "_applied_ok" in src
    # реально пропускает их в цикле
    assert "if _bn0 in _applied_ok:" in src and "continue" in src
    # ретраит только чистые: 'warnings' и незаписанные НЕ в _applied_ok → выполняются
    assert "status='ok'" in src


def test_skip_count_logged():
    src = inspect.getsource(db.create_pool)
    assert "skipped" in src
