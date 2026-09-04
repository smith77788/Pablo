from __future__ import annotations
import os
import json
import logging
import re
from pathlib import Path
from typing import Optional

import asyncpg

log = logging.getLogger(__name__)


SESSION_FORMATS = {
    "string_session": re.compile(r'^[A-Za-z0-9+/=]{100,}$'),
    "pyrogram_json": re.compile(r'^\{.*"dc_id".*"api_id".*"test_mode".*\}$', re.DOTALL),
    "tdata_dir": re.compile(r'^[a-f0-9]{32}$'),
}


# Бюджет на одну сессию и сколько проверяем одновременно. Подобрано под
# 30-секундный тайм-аут мини-аппа: порция в 20 строк укладывается в две волны.
VALIDATE_TIMEOUT_S = 10.0
VALIDATE_CONCURRENCY = 10


def _proxy_type(proxy_url: str) -> str | None:
    """Тип прокси по схеме URL (socks5/socks4/http) или None. Инлайн-копия
    parse_proxy_type — чтобы не тянуть тяжёлый mini_app_api в импортёр сессий."""
    u = (proxy_url or "").strip().lower()
    if u.startswith("socks5://"):
        return "socks5"
    if u.startswith("socks4://"):
        return "socks4"
    if u.startswith("http://"):
        return "http"
    return None


def detect_format(data: str) -> str:
    data = data.strip()
    if data.startswith('{'):
        try:
            parsed = json.loads(data)
            if 'dc_id' in parsed and 'api_id' in parsed:
                return 'pyrogram_json'
        except json.JSONDecodeError:
            pass
    if SESSION_FORMATS['string_session'].match(data):
        return 'string_session'
    if len(data) == 32 and all(c in '0123456789abcdef' for c in data):
        return 'tdata_hash'
    return 'unknown'


def extract_session_string(data: str, fmt: str) -> str | None:
    if fmt == 'string_session':
        return data.strip()
    if fmt == 'pyrogram_json':
        try:
            parsed = json.loads(data.strip())
            return parsed.get('session')
        except Exception:
            return None
    return None


async def validate_session(session_string: str, proxy_url: str | None = None,
                           timeout_s: float = VALIDATE_TIMEOUT_S) -> dict:
    """Проверить сессию живым подключением.

    Тайм-аут покрывает ВЕСЬ разговор, а не только connect: раньше wait_for
    оборачивал лишь установку соединения, а последующий get_me мог висеть без
    ограничения — одна такая сессия подвешивала весь импорт.
    """
    from services.account_manager import _make_client
    import asyncio
    client = None
    try:
        device = {"proxy_url": proxy_url} if proxy_url else None
        client = _make_client(session_string, device)

        async def _talk():
            await client.connect()
            return await client.get_me()

        me = await asyncio.wait_for(_talk(), timeout=timeout_s)
        return {
            "valid": True,
            "phone": me.phone or "",
            "user_id": me.id,
            "first_name": me.first_name or "",
            "username": me.username or "",
        }
    except Exception as e:
        return {"valid": False, "error": str(e)[:200]}
    finally:
        if client is not None and client.is_connected():
            await client.disconnect()


async def ensure_user_proxy(
    pool: asyncpg.Pool, owner_id: int, proxy_url: str, label: str | None = None
) -> int | None:
    """Найти или создать user_proxy владельца из сырого URL и вернуть его id.

    Переиспользует ту же логику, что и ручное «Добавить прокси» (шифр at-rest +
    детерминированный proxy_fp для дедупа). Нужна, чтобы пользователь мог задать
    НОВЫЙ прокси прямо в форме импорта (а не только выбрать из уже сохранённых) —
    вставленный прокси сразу СОЗДАЁТСЯ и ЗАКРЕПЛЯЕТСЯ за импортируемыми аккаунтами.
    Возвращает None, если URL не похож на прокси или вставка не удалась (тогда
    импорт продолжится напрямую с host-IP, а не упадёт целиком).
    """
    proxy_url = (proxy_url or "").strip()
    proxy_type = _proxy_type(proxy_url)
    if not proxy_type:
        return None
    try:
        from services.token_vault import encrypt_token, proxy_fingerprint

        _fp = proxy_fingerprint(proxy_url)
        _enc = encrypt_token(proxy_url)
        try:
            row = await pool.fetchrow(
                """INSERT INTO user_proxies(owner_id, label, proxy_url, proxy_type, proxy_fp)
                   VALUES($1,$2,$3,$4,$5)
                   ON CONFLICT(owner_id, proxy_fp) WHERE proxy_fp IS NOT NULL DO UPDATE
                   SET label=COALESCE(EXCLUDED.label, user_proxies.label) RETURNING id""",
                owner_id, label, _enc, proxy_type, _fp)
        except asyncpg.UndefinedColumnError:
            # proxy_fp ещё не мигрирован (лаг деплоя) — фолбэк без него.
            row = await pool.fetchrow(
                """INSERT INTO user_proxies(owner_id, label, proxy_url, proxy_type)
                   VALUES($1,$2,$3,$4) RETURNING id""",
                owner_id, label, _enc, proxy_type)
        return int(row["id"]) if row else None
    except Exception:
        log.exception("ensure_user_proxy owner=%s", owner_id)
        return None


async def import_sessions(
    pool: asyncpg.Pool,
    owner_id: int,
    raw_data: str,
    proxy_url: str | None = None,
    proxy_id: int | None = None,
) -> dict:
    # proxy_url — через что ПРОВЕРЯЕМ сессию. proxy_id (если задан) — реальный прокси
    # владельца, который СРАЗУ закрепляем за импортированным аккаунтом (изоляция с
    # первого шага: иначе аккаунт падает на общий CF-relay с единым IP). proxy_id
    # доверяем только свой — сверяем принадлежность владельцу.
    if proxy_id is not None:
        try:
            owns_proxy = await pool.fetchval(
                "SELECT 1 FROM user_proxies WHERE id=$1 AND owner_id=$2", proxy_id, owner_id)
            if not owns_proxy:
                proxy_id = None  # чужой/несуществующий прокси не закрепляем
        except Exception:
            proxy_id = None
    # Пользователь задал НОВЫЙ прокси прямо в форме (без выбора из сохранённых):
    # proxy_id не пришёл, но есть сырой proxy_url — создаём/находим user_proxy и
    # закрепляем его. «Задать прокси перед подключением» без лишнего шага в UI.
    if proxy_id is None and proxy_url:
        proxy_id = await ensure_user_proxy(pool, owner_id, proxy_url)
    # Прокси не выбран — РАСКЛАДЫВАЕМ партию по живым прокси владельца, а не
    # оставляем всех на одном выходе. Раньше здесь было два одинаково плохих
    # исхода: выбранный прокси прописывался КАЖДОМУ аккаунту партии, либо все
    # получали NULL и уходили в сеть с одного реального IP сервера. Общий IP —
    # сильнейший признак когорты: банят один аккаунт — уезжает вся партия.
    # Собственный аудит продукта (proxy_selector.audit_proxy_isolation) считает
    # нормой один аккаунт на один IP, то есть продукт сам создавал нарушение,
    # о котором потом честно докладывал.
    auto_pool: list[tuple] = []
    if proxy_id is None:
        try:
            prows = await pool.fetch(
                """SELECT p.id,
                          (SELECT COUNT(*) FROM tg_accounts a
                            WHERE a.proxy_id = p.id) AS used
                     FROM user_proxies p
                    WHERE p.owner_id=$1 AND p.is_active
                      AND COALESCE(p.is_alive, TRUE)""",
                owner_id)
            auto_pool = [(int(r["id"]), int(r["used"] or 0)) for r in prows]
        except Exception:
            auto_pool = []   # нет прокси или база моргнула — импорт не рушим
    else:
        # Явный выбор пользователя уважаем, но считаем честно: сколько
        # аккаунтов окажется за этим выходом ВСЕГО, включая уже стоявшие там.
        try:
            _used = await pool.fetchval(
                "SELECT COUNT(*) FROM tg_accounts WHERE proxy_id=$1", proxy_id)
            auto_pool = [(int(proxy_id), int(_used or 0))]
        except Exception:
            auto_pool = [(int(proxy_id), 0)]
    lines = [l.strip() for l in raw_data.strip().splitlines() if l.strip()]
    if not lines:
        return {"imported": 0, "failed": 0, "errors": ["Пустые данные"]}
    imported = 0
    failed = 0
    errors = []
    # Каждая строка валидируется реальным подключением к Telegram (до 15с) —
    # синхронно. Ограничиваем порцию, чтобы запрос не завис молча по тайм-ауту;
    # честно сообщаем про остаток. Массовый импорт (файлы) — через бота.
    MAX_PER_IMPORT = 20
    truncated = 0
    if len(lines) > MAX_PER_IMPORT:
        truncated = len(lines) - MAX_PER_IMPORT
        lines = lines[:MAX_PER_IMPORT]
    # Разбор формата — чистый и мгновенный, делаем его первым проходом; сетевую
    # проверку запускаем ПАРАЛЛЕЛЬНО с ограничением. Раньше сессии проверялись
    # строго по очереди по 15 с каждая: три медленные строки уже перекрывали
    # 30-секундный тайм-аут мини-аппа, и пользователь видел «нет соединения»,
    # хотя импорт на сервере продолжался. Это ломало самый первый шаг продукта.
    import asyncio as _asyncio

    parsed: list[tuple[int, str | None, str | None]] = []   # (i, session_str, error)
    for i, line in enumerate(lines):
        fmt = detect_format(line)
        if fmt == 'unknown':
            parsed.append((i, None, "неизвестный формат"))
            continue
        session_str = extract_session_string(line, fmt)
        if not session_str:
            parsed.append((i, None, "не удалось извлечь сессию"))
            continue
        parsed.append((i, session_str, None))

    _sem = _asyncio.Semaphore(VALIDATE_CONCURRENCY)

    async def _validate(idx: int, sess: str):
        async with _sem:
            try:
                return idx, await validate_session(sess, proxy_url)
            except Exception as exc:      # проверка одной строки не рвёт импорт
                return idx, {"valid": False, "error": str(exc)[:200]}

    checked = dict(await _asyncio.gather(
        *[_validate(i, sess) for i, sess, err in parsed if sess]))

    # Раскладку считаем на число реально прошедших проверку сессий, а не на
    # число строк: иначе половина прокси «занялась бы» под битые строки.
    from services.proxy_balancer import (
        isolation_note, isolation_summary, plan_distribution,
    )

    _ok_count = sum(1 for i, sess, err in parsed
                    if sess and (checked.get(i) or {}).get("valid"))
    _prior_loads = {pid: used for pid, used in auto_pool}
    _plan = (plan_distribution(auto_pool, _ok_count) if proxy_id is None
             else [proxy_id] * _ok_count)
    _plan_pos = 0
    _assigned: list = []

    # Дальше — строго по порядку строк: и записи в БД, и нумерация ошибок.
    for i, session_str, parse_error in parsed:
        if parse_error:
            failed += 1
            errors.append(f"Строка {i+1}: {parse_error}")
            continue
        result = checked.get(i) or {"valid": False, "error": "проверка не выполнена"}
        if not result['valid']:
            failed += 1
            # Классифицируем причину, а не сваливаем всё в «невалидная сессия»:
            # мёртвый прокси/сеть/флуд → сессия скорее всего ЦЕЛА, дело в прокси —
            # иначе пользователь удалит рабочую сессию. (net/flood отделяем от
            # реально недействительной сессии.)
            from services.contacts_hub.sync_service import classify_session_error
            friendly, _kind = classify_session_error(result.get('error', '') or '')
            errors.append(f"Строка {i+1}: {friendly}")
            continue
        # Дедуп по детерминированному fingerprint (шифр недетерминирован, поэтому
        # сравнение по session_str=шифротекст не сработало бы). Fallback на
        # плейнтекст-равенство ловит legacy-строки, у которых session_fp ещё NULL.
        from services.token_vault import encrypt_token, session_fingerprint

        _fp = session_fingerprint(session_str)
        existing = await pool.fetchrow(
            "SELECT id, owner_id FROM tg_accounts WHERE session_fp=$1 OR session_str=$2",
            _fp, session_str,
        )
        if existing:
            failed += 1
            # Дедуп глобальный (одна Telegram-сессия = один аккаунт на платформе),
            # но id раскрываем ТОЛЬКО владельцу. Иначе импорт чужой сессии выдал бы
            # внутренний id аккаунта другого владельца (cross-tenant disclosure).
            if existing["owner_id"] == owner_id:
                errors.append(f"Строка {i+1}: сессия уже есть в вашем аккаунте (id={existing['id']})")
            else:
                errors.append(f"Строка {i+1}: сессия уже используется на платформе")
            continue
        # Уникальный device-отпечаток НА АККАУНТ. Иначе импортные сессии идут в
        # БД без device_* и на _make_client получают ОДИН дефолтный отпечаток
        # (Samsung SM-S911B / en-US) — массовая коллизия фингерпринтов на самом
        # частом пути (покупные сессии). locale выводится из страны номера.
        from services.account_manager import (
            generate_device_fingerprint,
            country_code_from_phone,
        )

        # Закрепляем Telegram-приложение из пула (services/tg_apps.py): весь
        # флот под одним api_id — прямой корреляционный признак когорты.
        from services.tg_apps import assign_api_id as _assign_api_id

        phone = result.get('phone', '') or ''
        dev = generate_device_fingerprint(country_code_from_phone(phone))
        # Свой прокси на аккаунт — из заранее посчитанной раскладки. Дубли и
        # прочие отсеянные строки позиции в плане НЕ занимают: план сдвигается
        # только на реально записанный аккаунт.
        _acc_proxy = (_plan[_plan_pos] if _plan_pos < len(_plan) else proxy_id)
        try:
            await pool.execute(
                """INSERT INTO tg_accounts
                       (owner_id, session_str, session_fp, phone, is_active, acc_status,
                        device_model, system_version, app_version, lang_code, system_lang_code,
                        api_id, proxy_id)
                   VALUES ($1, $2, $3, $4, TRUE, 'active',
                        $5, $6, $7, $8, $9, $10, $11)""",
                owner_id, encrypt_token(session_str), _fp, phone,
                dev["device_model"], dev["system_version"], dev["app_version"],
                dev["lang_code"], dev["system_lang_code"],
                _assign_api_id(_fp), _acc_proxy,
            )
            imported += 1
            _plan_pos += 1
            _assigned.append(_acc_proxy)
        except Exception as e:
            failed += 1
            errors.append(f"Строка {i+1}: ошибка БД — {str(e)[:100]}")
    # CF-релей НЕ раздаём импортированным аккаунтам автоматически: без прокси —
    # прямой host-IP. Прокси задаёт пользователь (proxy_id при импорте), CF/IPv6 —
    # только по явному выбору. Авто-CF делала релей «основой» и запирала аккаунты.
    if truncated:
        # ВПЕРЁД: иначе срез errors[:20] обрезал бы это важное сообщение, когда
        # набралось 20 построчных ошибок.
        errors.insert(0,
            f"Ещё {truncated} строк не обработано за раз — импортируйте порцией "
            f"до {MAX_PER_IMPORT} или загрузите файл сессий в боте.")
    # Итог по изоляции — сразу, а не в отдельном аудите, который пользователь
    # откроет когда-нибудь потом (или никогда). Объяснять общий IP после
    # массового бана когорты уже поздно.
    _iso = isolation_summary(_assigned, _prior_loads)
    return {"imported": imported, "failed": failed, "errors": errors[:20],
            "isolation": _iso, "isolation_note": isolation_note(_iso)}
