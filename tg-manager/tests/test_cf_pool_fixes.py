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


def test_cf_pool_ui_button_wired():
    """Кнопка деплоя пула должна существовать и звать реальный роут (не только curl)."""
    ui = _read("mini_app/index.html")
    assert "deployCfPool" in ui and "loadCfPoolStatus" in ui
    assert "/api/miniapp/cf/pool/deploy" in ui and "/api/miniapp/cf/pool/status" in ui
    api = _read("services/mini_app_api.py")
    assert 'add_post("/api/miniapp/cf/pool/deploy", cf_pool_deploy)' in api
    assert 'add_get("/api/miniapp/cf/pool/status", cf_pool_status)' in api
    # хендлеры делают реальную работу (не заглушки)
    seg = api[api.index("async def cf_pool_deploy"):api.index("async def cf_pool_deploy") + 1200]
    assert "deploy_pool" in seg and "assign_urls_to_accounts" in seg


def test_cf_credentials_in_app_no_railway():
    """Доступы CF можно вписать в приложении (шифр в БД), Railway не обязателен."""
    dbsrc = _read("database/db.py")
    assert "async def set_cf_credentials" in dbsrc and "async def get_cf_credentials" in dbsrc
    assert "encrypt_token" in dbsrc[dbsrc.index("async def set_cf_credentials"):dbsrc.index("async def get_cf_credentials")]
    api = _read("services/mini_app_api.py")
    assert "async def cf_credentials_save" in api
    assert 'add_post("/api/miniapp/cf/credentials", cf_credentials_save)' in api
    # deploy берёт доступы из БД-владельца, с фолбэком на env (не жёстко Railway)
    seg = api[api.index("async def _cf_resolve_creds"):api.index("async def cf_pool_status")]
    assert "db.get_cf_credentials" in seg and 'os.getenv("CF_API_TOKEN"' in seg
    assert "from database import db" in seg  # иначе NameError в проде
    # статус не отдаёт токен наружу (только флаги)
    st = api[api.index("async def cf_pool_status"):api.index("async def cf_credentials_save")]
    assert "has_token" in st and "api_token" not in st.split("credentials")[1][:200]
    ui = _read("mini_app/index.html")
    assert "saveCfCreds" in ui and "/api/miniapp/cf/credentials" in ui
    assert 'type="password" id="cfToken"' in ui  # токен вводится как пароль


def test_schema_v154_restores_and_indexes():
    s = _read("schema_v154.sql")
    # восстановлена потерянная таблица + opt-in колонка
    assert "CREATE TABLE IF NOT EXISTS bot_seo_suggestions" in s
    assert "PRIMARY KEY (owner_id, bot_id)" in s   # под ON CONFLICT(owner_id,bot_id)
    assert "auto_reoptimize" in s
    # уникальный индекс под ON CONFLICT(owner_id, worker_url)
    assert "UNIQUE INDEX" in s and "cf_worker_pool(owner_id, worker_url)" in s
