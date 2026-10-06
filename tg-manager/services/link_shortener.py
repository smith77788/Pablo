"""Свой сокращатель ссылок — «bit.ly для себя».

Зачем в продукте. Массовые операции (инвайты, посты, рассылки) постоянно несут
ссылки. Своя короткая ссылка даёт две вещи, которых у сырого URL нет:
  • аналитику — сколько человек реально кликнули (переходы считаются на редиректе);
  • развязку с доменом цели — в сообщении светится наш короткий домен, а не сырой
    (меньше поводов для антиспам-детекта площадки и чище вид).

Как устроено. Таблица short_links(code → target_url, owner_id, clicks …). Публичный
редирект `GET /s/<code>` отдаёт 302 на target_url и инкрементит счётчик (см.
mini_app_api.setup_routes). Создание/список/отключение — через API мини-аппа,
скоуп по owner_id. Сервер НИЧЕГО не ходит по target_url сам (редирект делает
браузер клиента) — поэтому SSRF тут не возникает; но target_url всё равно
валидируем (схема, длина, не наш же редирект — защита от петли).

Коды: base-58 (без похожих 0/O/l/I/1), 7 символов ≈ 58^7 ≈ 2·10^12 — коллизии
единичны, на них есть повтор генерации.
"""
from __future__ import annotations

import re
import secrets
from typing import Any
from urllib.parse import urlparse

from services.logger import get_logger, log_exc_swallow

log = get_logger(__name__)

# Авто-коды: base58 — без 0 O I l 1, чтобы ссылку можно было продиктовать/
# переписать без ошибок (генерим МЫ, поэтому читаемость важнее).
_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_CODE_LEN = 7
_MAX_URL_LEN = 2048
# Кастомный код выбирает владелец (напр. «link») — даём обычный URL-безопасный
# алфавит: буквы/цифры/дефис/подчёркивание, 2–32 символа, начинается с букв/цифр.
_CUSTOM_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{1,31}$")
# Для РЕЗОЛВА/РОУТИНГА принимаем широкий набор (источник истины — БД): и авто-,
# и кастомные коды. Узкую проверку делает только создание.
_RESOLVE_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
# Публичный шаблон для корневого роута infragram.app/<код> (должен совпадать с
# _RESOLVE_RE, но для aiohttp пишется как строка внутри {code:...}).
ROUTE_CODE_PATTERN = r"[A-Za-z0-9_-]{2,40}"

# Зарезервированные коды: совпали бы с существующими путями приложения —
# ни создать такой код, ни увести на него редирект нельзя.
_RESERVED = {
    "api", "miniapp", "s", "health", "webhook", "tgbot", "static", "favicon",
    "robots", "sitemap", "index", "assets", "admin", "login", "auth", "pair",
    "dashboard", "www", "app", "link",  # «link» оставляем владельцу — см. ниже
}
# «link» владелец просил как пример — он НЕ зарезервирован (убираем из набора).
_RESERVED.discard("link")


def _gen_code(n: int = _CODE_LEN) -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(n))


def is_reserved(code: str) -> bool:
    return (code or "").lower() in _RESERVED


def valid_custom_code(code: str) -> bool:
    return bool(code) and bool(_CUSTOM_RE.match(code)) and not is_reserved(code)


def normalize_url(raw: str, *, self_hosts: set[str] | None = None) -> str | None:
    """Проверить и привести целевой URL. None — ссылка недопустима.

    Без схемы дописываем https:// (операторы пишут «t.me/foo», а не полный URL).
    Разрешаем только http/https с непустым хостом; не даём заворачивать на наш же
    редирект (петля) и на не-веб-схемы (javascript:, data:, file: …).
    """
    if not raw:
        return None
    raw = raw.strip()
    if len(raw) > _MAX_URL_LEN:
        return None
    # Схемы вида javascript:/data: отсекаем до доклейки https.
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:", raw):
        _scheme = raw.split(":", 1)[0].lower()
        if _scheme not in ("http", "https"):
            return None
    else:
        raw = "https://" + raw
    try:
        p = urlparse(raw)
    except Exception:
        return None
    if p.scheme not in ("http", "https") or not p.netloc:
        return None
    host = (p.hostname or "").lower()
    if not host:
        return None
    # Петля: цель — наш же короткий редирект. Иначе ссылка вела бы сама на себя.
    if self_hosts and host in {h.lower() for h in self_hosts} and p.path.startswith("/s/"):
        return None
    return raw


async def ensure_table(pool) -> None:
    """Досоздать таблицу, если инлайн-миграция ещё не доехала (fail-open)."""
    try:
        await pool.execute(_TABLE_DDL)
        await pool.execute(
            "CREATE INDEX IF NOT EXISTS idx_short_links_owner "
            "ON short_links(owner_id, created_at DESC)")
    except Exception as e:
        log_exc_swallow(log, f"link_shortener: ensure_table: {e}")


_TABLE_DDL = (
    "CREATE TABLE IF NOT EXISTS short_links("
    "  code TEXT PRIMARY KEY,"
    "  owner_id BIGINT NOT NULL,"
    "  target_url TEXT NOT NULL,"
    "  title TEXT,"
    "  clicks BIGINT NOT NULL DEFAULT 0,"
    "  disabled BOOLEAN NOT NULL DEFAULT FALSE,"
    "  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),"
    "  last_click_at TIMESTAMPTZ)"
)


async def create(pool, owner_id: int, target_url: str, title: str | None = None,
                 *, self_hosts: set[str] | None = None,
                 custom_code: str | None = None) -> dict[str, Any]:
    """Создать короткую ссылку. {"ok":True, "code":..} или {"ok":False,"error":..}.

    Дедуп: у одного владельца один и тот же target_url переиспользует существующий
    код (не плодим дубли и копим клики в одном месте). custom_code — опциональный
    человекочитаемый код (проверяется на формат и занятость).
    """
    url = normalize_url(target_url, self_hosts=self_hosts)
    if not url:
        return {"ok": False, "error": "Нужна корректная http(s)-ссылка"}
    title = (title or "").strip()[:200] or None

    if custom_code is not None:
        custom_code = custom_code.strip().lstrip("/")
        if not valid_custom_code(custom_code):
            return {"ok": False, "error": "Код: 2–32 символа — латиница, цифры, «-», «_»; "
                                          "начинается с буквы/цифры и не зарезервированное слово"}
        try:
            _row = await pool.fetchrow(
                "INSERT INTO short_links(code, owner_id, target_url, title) "
                "VALUES($1,$2,$3,$4) ON CONFLICT (code) DO NOTHING RETURNING code",
                custom_code, int(owner_id), url, title)
        except Exception as e:
            log_exc_swallow(log, f"link_shortener: insert custom: {e}")
            return {"ok": False, "error": "Не удалось сохранить ссылку"}
        if not _row:
            return {"ok": False, "error": "Такой код уже занят — выберите другой"}
        return {"ok": True, "code": custom_code, "target_url": url, "title": title,
                "clicks": 0}

    # Дедуп по (owner, target): тот же URL → тот же код.
    try:
        _ex = await pool.fetchrow(
            "SELECT code, clicks, title FROM short_links "
            "WHERE owner_id=$1 AND target_url=$2 AND NOT disabled LIMIT 1",
            int(owner_id), url)
    except Exception:
        _ex = None
    if _ex:
        return {"ok": True, "code": _ex["code"], "target_url": url,
                "title": _ex["title"], "clicks": int(_ex["clicks"] or 0),
                "existing": True}

    # Генерация уникального кода с повтором на коллизии.
    for _ in range(6):
        code = _gen_code()
        try:
            _row = await pool.fetchrow(
                "INSERT INTO short_links(code, owner_id, target_url, title) "
                "VALUES($1,$2,$3,$4) ON CONFLICT (code) DO NOTHING RETURNING code",
                code, int(owner_id), url, title)
        except Exception as e:
            log_exc_swallow(log, f"link_shortener: insert: {e}")
            return {"ok": False, "error": "Не удалось сохранить ссылку"}
        if _row:
            return {"ok": True, "code": code, "target_url": url, "title": title,
                    "clicks": 0}
    return {"ok": False, "error": "Не удалось сгенерировать свободный код, повторите"}


async def resolve(pool, code: str) -> str | None:
    """Найти target_url по коду и ЗАСЧИТАТЬ переход. None — нет/отключена.

    Инкремент клика — тем же запросом (UPDATE ... RETURNING): без лишнего захода и
    без гонки. Ошибку счётчика глотаем — редирект важнее аналитики.
    """
    if not code or not _RESOLVE_RE.match(code) or is_reserved(code):
        return None
    try:
        row = await pool.fetchrow(
            "UPDATE short_links SET clicks=clicks+1, last_click_at=now() "
            "WHERE code=$1 AND NOT disabled RETURNING target_url",
            code)
    except Exception as e:
        log_exc_swallow(log, f"link_shortener: resolve: {e}")
        # Фолбэк: хотя бы отдать цель без учёта клика.
        try:
            row = await pool.fetchrow(
                "SELECT target_url FROM short_links WHERE code=$1 AND NOT disabled", code)
        except Exception:
            return None
    return row["target_url"] if row else None


async def list_for_owner(pool, owner_id: int, limit: int = 100) -> list[dict]:
    try:
        rows = await pool.fetch(
            "SELECT code, target_url, title, clicks, disabled, created_at, last_click_at "
            "FROM short_links WHERE owner_id=$1 ORDER BY created_at DESC LIMIT $2",
            int(owner_id), int(max(1, min(limit, 500))))
    except Exception as e:
        log_exc_swallow(log, f"link_shortener: list: {e}")
        return []
    out = []
    for r in rows or []:
        out.append({
            "code": r["code"], "target_url": r["target_url"], "title": r["title"],
            "clicks": int(r["clicks"] or 0), "disabled": bool(r["disabled"]),
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            "last_click_at": r["last_click_at"].isoformat() if r["last_click_at"] else None,
        })
    return out


async def set_disabled(pool, owner_id: int, code: str, disabled: bool) -> bool:
    try:
        res = await pool.execute(
            "UPDATE short_links SET disabled=$3 WHERE code=$1 AND owner_id=$2",
            code, int(owner_id), bool(disabled))
        return str(res).endswith("1")
    except Exception as e:
        log_exc_swallow(log, f"link_shortener: set_disabled: {e}")
        return False


async def delete(pool, owner_id: int, code: str) -> bool:
    try:
        res = await pool.execute(
            "DELETE FROM short_links WHERE code=$1 AND owner_id=$2", code, int(owner_id))
        return str(res).endswith("1")
    except Exception as e:
        log_exc_swallow(log, f"link_shortener: delete: {e}")
        return False
