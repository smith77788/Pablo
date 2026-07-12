"""Регрессия: доведение CF-Worker relay (уникальный IP на аккаунт) до рабочего.

4 блокера, найденные ревью, закрыты:
1) per-account cf_relay_url реально используется при подключении (_make_client),
   а не только глобальный env CF_RELAY_URL;
2) URL воркера строится с workers.dev-ПОДДОМЕНОМ (не account_id) + включается роут;
3) уникальный индекс cf_worker_pool(owner_id,worker_url) для ON CONFLICT;
4) восстановлена потерянная миграция bot_seo_suggestions (+auto_reoptimize).
"""
from __future__ import annotations

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def test_per_account_relay_used_in_make_client():
    am = _read("services/account_manager.py")
    seg = am[am.index("Выбор транспорта"):am.index("return TelegramClient")]
    # приоритет пер-аккаунтного relay над глобальным env
    assert 'device.get("cf_relay_url")' in seg
    assert "acc_relay or CF_RELAY_URL" in seg
    # колонка реально селектится в основных путях загрузки аккаунта
    assert "a.cf_relay_url" in _read("database/db.py")
    assert "a.cf_relay_url" in _read("services/op_worker.py")


def test_worker_url_uses_subdomain_and_enables_route():
    cf = _read("services/cf_pool_manager.py")
    # правильный формат URL: {name}.{subdomain}.workers.dev (не account_id)
    assert "{name}.{subdomain}.workers.dev" in cf
    # старый баг (URL по account_id) убран из КОДА (в комментарии-объяснении ок)
    assert 'f"https://{name}.{account_id}.workers.dev"' not in cf
    # поддомен берётся из env или CF API, роут workers.dev включается отдельно
    assert "async def get_workers_subdomain" in cf
    assert "CF_WORKERS_SUBDOMAIN" in cf
    assert "async def _enable_workers_dev" in cf and "/subdomain" in cf


def test_schema_v154_restores_and_indexes():
    s = _read("schema_v154.sql")
    # восстановлена потерянная таблица + opt-in колонка
    assert "CREATE TABLE IF NOT EXISTS bot_seo_suggestions" in s
    assert "PRIMARY KEY (owner_id, bot_id)" in s   # под ON CONFLICT(owner_id,bot_id)
    assert "auto_reoptimize" in s
    # уникальный индекс под ON CONFLICT(owner_id, worker_url)
    assert "UNIQUE INDEX" in s and "cf_worker_pool(owner_id, worker_url)" in s
