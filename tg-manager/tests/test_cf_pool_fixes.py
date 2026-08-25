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
    seg = am[am.index("Выбор транспорта"):am.index("_client = TelegramClient")]
    # приоритет пер-аккаунтного relay над глобальным env; глобальный CF_RELAY_URL
    # под allow_direct НЕ подменяет прямой реальный IP (только под strict)
    assert 'device.get("cf_relay_url")' in seg
    assert 'acc_relay or (CF_RELAY_URL if _pol == "strict" else "")' in seg
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
    seg = api[api.index("async def cf_pool_deploy"):
              api.index('app.router.add_get("/api/miniapp/cf/pool/status"')]
    assert "deploy_pool" in seg and "assign_urls_to_accounts" in seg
    # деплой в ФОНЕ (иначе 100 воркеров синхронно → таймаут шлюза → «ответ не JSON»)
    assert "create_task" in seg and '"started": True' in seg
    # deploy_pool конкурентен (Semaphore/gather), а не последователен
    cf = _read("services/cf_pool_manager.py")
    assert "asyncio.Semaphore" in cf and "asyncio.gather" in cf


def test_set_cf_credentials_no_jsonb_ops_on_text_column():
    """settings_json — text-колонка (JSON-строка). set_cf_credentials должен мержить
    в Python и писать строкой, НЕ jsonb-операторами (иначе 'COALESCE types text and
    jsonb cannot be matched' — реальный сбой сохранения доступов)."""
    dbsrc = _read("database/db.py")
    seg = dbsrc[dbsrc.index("async def set_cf_credentials"):dbsrc.index("async def get_cf_credentials")]
    # старый баг убран из САМОГО UPDATE (в поясняющем комментарии ::jsonb допустим)
    assert "jsonb_build_object" not in seg
    upd = seg[seg.index("UPDATE platform_users SET settings_json"):]
    assert "::jsonb" not in upd[:200]
    # правильный паттерн: read → merge(Python) → dumps → write строкой
    assert "_json.loads" in seg and "_json.dumps(settings)" in seg
    assert "SET settings_json=$2" in seg


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


def test_worker_uses_module_format_and_sockets_import():
    """Воркер использует ES-модули (`export default` + `import ... from
    "cloudflare:sockets"`). Без импорта connect — ReferenceError в рантайме;
    залитый сырым PUT application/javascript — SyntaxError на export. Значит:
    (1) шаблон импортирует connect из cloudflare:sockets;
    (2) deploy_worker заливает как module-воркер (multipart + main_module)."""
    cf = _read("services/cf_pool_manager.py")
    assert 'import { connect } from "cloudflare:sockets";' in cf
    seg = cf[cf.index("async def deploy_worker"):cf.index("async def deploy_pool")]
    # module-загрузка, не сырой javascript PUT
    assert '"main_module": "worker.js"' in seg
    assert "application/javascript+module" in seg
    assert "FormData" in seg and '"compatibility_date"' in seg
    # старый баг (Content-Type: application/javascript на самом PUT) убран
    assert 'headers = {\n        "Authorization"' not in seg or "application/javascript+module" in seg


def test_worker_dc_ips_match_canonical():
    """Критично (anti-detection): DC-IP в воркере обязаны совпадать с каноном
    account_manager. auth_key DC-специфичен — роут на чужой DC-IP = аккаунт
    молча не подключается. Прежний шаблон роутил DC2/DC3→IP DC1, DC4→IP DC5."""
    import re
    cf = _read("services/cf_pool_manager.py")
    am = _read("services/account_manager.py")
    # канон берём из первого DC_IPS в account_manager (строки 1..5)
    canon = {"1": "149.154.175.53", "2": "149.154.167.51",
             "3": "149.154.175.100", "4": "149.154.167.91", "5": "91.108.56.130"}
    for dc, ip in canon.items():
        assert ip in am, f"канон DC{dc} {ip} пропал из account_manager"
        # в JS-шаблоне: `  N: "ip",`
        assert re.search(rf'\b{dc}:\s*"{re.escape(ip)}"', cf), \
            f"воркер DC{dc} должен указывать на {ip}"
    # старый неверный маппинг (DC2→175.53) убран
    assert not re.search(r'\b2:\s*"149\.154\.175\.53"', cf)


def test_deploy_errors_are_surfaced_not_swallowed():
    """«Пустой результат» без причины недопустим: ошибки CF должны доходить до
    пользователя. deploy_pool возвращает {urls,errors,ok,count}; фон пишет
    last_deploy; /status отдаёт его; фронт показывает текст ошибки."""
    cf = _read("services/cf_pool_manager.py")
    seg = cf[cf.index("async def deploy_pool"):cf.index("async def assign_urls_to_accounts")]
    assert '"errors"' in seg and '"urls"' in seg and '"ok"' in seg
    api = _read("services/mini_app_api.py")
    dep = api[api.index("async def cf_pool_deploy"):
              api.index('app.router.add_get("/api/miniapp/cf/pool/status"')]
    # результат/ошибки сохраняются в process-local карту, а не глотаются
    assert '_cf_deploy_last' in dep and 'res.get("errors"' in dep
    st = api[api.index("async def cf_pool_status"):api.index("async def cf_credentials_save")]
    assert "_cf_deploy_last" in st and '"last_deploy"' in st
    ui = _read("mini_app/index.html")
    assert "last_deploy" in ui and "Деплой не удался" in ui


def test_deploy_auto_count_by_active_accounts():
    """count не задан/0/'auto' → деплой по числу аккаунтов, которым нужен релей
    (активные без своего прокси) — изоляция 1:1 без ручного счёта."""
    cf = _read("services/cf_pool_manager.py")
    assert "async def count_relay_targets" in cf
    seg = cf[cf.index("async def count_relay_targets"):cf.index("async def assign_urls_to_accounts")]
    assert "proxy_id IS NULL" in seg and "is_active" in seg.lower()
    api = _read("services/mini_app_api.py")
    dep = api[api.index("async def cf_pool_deploy"):
              api.index('app.router.add_get("/api/miniapp/cf/pool/status"')]
    assert "count_relay_targets" in dep
    assert "'auto'" in dep and "relay_needed" in dep
    ui = _read("mini_app/index.html")
    assert "relay_needed" in ui and "cfAutoHint" in ui


def test_assignment_skips_proxy_bound_accounts():
    """Anti-detection: релей раздаётся ТОЛЬКО аккаунтам без своего прокси —
    proxy_id-аккаунты не трогаем (у них своя IP-изоляция)."""
    cf = _read("services/cf_pool_manager.py")
    seg = cf[cf.index("async def assign_urls_to_accounts"):cf.index("async def check_pool")]
    assert "proxy_id IS NULL" in seg
    # старая безусловная выборка всех активных убрана
    assert "WHERE owner_id=$1 AND is_active=TRUE\",\n        owner_id)" not in seg


def test_worker_health_and_check_pool():
    """Воркер отвечает на /health (живость+colo); check_pool пингует все и
    обновляет статус; есть эндпоинт и кнопка в UI."""
    cf = _read("services/cf_pool_manager.py")
    assert "'/health'" in cf and "request.cf" in cf and "colo" in cf
    assert "async def check_pool" in cf and "/health" in cf
    api = _read("services/mini_app_api.py")
    assert "async def cf_pool_check" in api
    assert 'add_post("/api/miniapp/cf/pool/check", cf_pool_check)' in api
    ui = _read("mini_app/index.html")
    assert "checkCfWorkers" in ui and "/api/miniapp/cf/pool/check" in ui


def test_worker_reports_real_egress_ip():
    """Честная проверка уникальности: воркер /health возвращает РЕАЛЬНЫЙ egress-IP
    (subrequest к cdn-cgi/trace), check_pool агрегирует уникальные IP."""
    cf = _read("services/cf_pool_manager.py")
    assert "cdn-cgi/trace" in cf and "ip=" in cf
    seg = cf[cf.index("async def check_pool"):cf.index("async def get_pool_status")]
    assert '"unique_ips"' in seg and '"ips"' in seg
    ui = _read("mini_app/index.html")
    assert "unique_ips" in ui


def test_pool_lifecycle_ops_exist():
    """Реконсиляция/удаление/раздача/лечение — полноценный жизненный цикл пула."""
    cf = _read("services/cf_pool_manager.py")
    for fn in ("async def delete_worker", "async def clear_pool",
               "async def reconcile_pool", "async def sync_relay_assignment",
               "async def heal_dead_relays"):
        assert fn in cf, fn
    # deploy делает reconcile (снос старых лишних воркеров)
    api = _read("services/mini_app_api.py")
    assert "reconcile_pool" in api
    for route in ('add_post("/api/miniapp/cf/pool/assign", cf_pool_assign)',
                  'add_post("/api/miniapp/cf/pool/clear", cf_pool_clear)'):
        assert route in api, route
    ui = _read("mini_app/index.html")
    assert "assignCfRelay" in ui and "clearCfPool" in ui


def test_cf_pool_monitor_only_heals_no_autoassign():
    """Монитор пула — только health→heal для аккаунтов, УЖЕ на CF (пользователь
    выбрал CF явно). Авто-раздачи релея «голым» аккаунтам в цикле больше НЕТ
    (CF opt-in, безпроксёвый аккаунт идёт прямым host-IP)."""
    cf = _read("services/cf_pool_manager.py")
    assert "async def run(" in cf
    seg = cf[cf.index("async def run("):]
    assert "check_pool" in seg and "heal_dead_relays" in seg
    # авто-раздача релея в фоновом цикле убрана
    assert "await sync_relay_assignment(pool, oid)" not in seg
    m = _read("main.py")
    assert "cf_pool_monitor" in m and "_cf_pool_manager.run" in m


def test_isolation_audit_is_relay_aware():
    """Аккаунт на CF-релее — не «голый»: audit отделяет on_relay от naked,
    ловит слабую изоляцию релея (один воркер на многих)."""
    ps = _read("services/proxy_selector.py")
    seg = ps[ps.index("async def audit_proxy_isolation"):ps.index("async def _fetch_backup_proxies")]
    assert "a.cf_relay_url" in seg
    assert '"on_relay"' in seg and '"relay_shared_groups"' in seg
    # «голые» = ни прокси, ни релея
    assert "not a.get(\"proxy_url\") and not a.get(\"cf_relay_url\")" in seg
    ui = _read("mini_app/index.html")
    assert "on_relay" in ui and "relay_shared_groups" in ui


def test_down_worker_debounced():
    """Разовый сетевой блип не должен двигать аккаунты: воркер помечается 'down'
    только после N подряд провалов health (fail_streak)."""
    cf = _read("services/cf_pool_manager.py")
    assert "_DOWN_THRESHOLD" in cf
    seg = cf[cf.index("async def check_pool"):cf.index("async def get_pool_status")]
    assert "fail_streak=fail_streak+1" in seg
    assert "fail_streak+1 >= $3" in seg and "fail_streak=0" in seg
    # колонка гарантируется self-heal на старте
    m = _read("main.py")
    assert "ADD COLUMN IF NOT EXISTS fail_streak" in m


def test_cf_relay_not_auto_assigned_on_account_add():
    """CF-релей НЕ раздаётся автоматически при добавлении/импорте аккаунта — CF
    opt-in. Без прокси аккаунт идёт прямым host-IP; прокси задаёт пользователь.
    (Раньше авто-CF делала релей «основой» и запирала аккаунты на нерабочих воркерах.)"""
    dbsrc = _read("database/db.py")
    seg = dbsrc[dbsrc.index("async def add_tg_account"):]
    seg = seg[:seg.index("return acc_id")+20]
    assert "sync_relay_assignment" not in seg
    imp = _read("services/session_importer.py")
    assert "sync_relay_assignment" not in imp


def test_cf_relay_url_column_self_healed():
    """Регресс контактов: 'column a.cf_relay_url does not exist'. Имя schema_v153
    занято двумя агентами → CF-миграция пропускалась. Колонка/таблица должны быть
    гарантированы (1) self-heal в main.py на старте, (2) само-содержащимся v154."""
    m = _read("main.py")
    assert "ADD COLUMN IF NOT EXISTS cf_relay_url TEXT" in m  # self-heal на старте
    assert "CREATE TABLE IF NOT EXISTS cf_worker_pool" in m
    v = _read("schema_v154.sql")
    # v154 само-содержащийся: колонка+таблица создаются ДО индекса, не зависят от v153
    assert "ADD COLUMN IF NOT EXISTS cf_relay_url" in v
    assert v.index("CREATE TABLE IF NOT EXISTS cf_worker_pool") < v.index("uq_cf_worker_pool_owner_url")
