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
    """Досоздать таблицу/колонки, если инлайн-миграция ещё не доехала (fail-open)."""
    try:
        await pool.execute(_TABLE_DDL)
        for _alter in _TABLE_ALTERS:
            await pool.execute(_alter)
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
    "  last_click_at TIMESTAMPTZ,"
    "  expires_at TIMESTAMPTZ,"
    "  tags TEXT)"
)
# Для уже созданной (прошлым релизом) таблицы без новых колонок — идемпотентно.
_TABLE_ALTERS = (
    "ALTER TABLE short_links ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ",
    "ALTER TABLE short_links ADD COLUMN IF NOT EXISTS tags TEXT",
)


def apply_utm(url: str, utm: dict | None) -> str:
    """Дописать UTM-метки к URL (source/medium/campaign/term/content).

    Теги трекинга площадок/рекламы: добавляются как query-параметры, существующие
    не трогаем. Пустые значения пропускаем.
    """
    if not utm:
        return url
    from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
    keys = ("utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content")
    add = [(k, str(utm[k]).strip()) for k in keys if (utm.get(k) or "").strip()]
    if not add:
        return url
    sp = urlsplit(url)
    q = parse_qsl(sp.query, keep_blank_values=True)
    have = {k for k, _ in q}
    q += [(k, v) for k, v in add if k not in have]
    return urlunsplit((sp.scheme, sp.netloc, sp.path, urlencode(q), sp.fragment))


def _clean_tags(tags) -> str | None:
    """Теги → строка через запятую (для организации ссылок). До 10 тегов."""
    if not tags:
        return None
    if isinstance(tags, str):
        parts = re.split(r"[,\n]", tags)
    else:
        parts = list(tags)
    out, seen = [], set()
    for t in parts:
        t = str(t).strip()[:40]
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
        if len(out) >= 10:
            break
    return ", ".join(out) or None


def _parse_expires(value) -> tuple[Any, str | None]:
    """ISO-строка/None → (datetime|None, error|None). Дата в прошлом — ошибка."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None, None
    import datetime as _dt
    try:
        s = str(value).strip().replace("Z", "+00:00")
        dt = _dt.datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_dt.timezone.utc)
    except (TypeError, ValueError):
        return None, "Неверная дата окончания"
    if dt <= _dt.datetime.now(_dt.timezone.utc):
        return None, "Срок действия уже истёк — выберите дату в будущем"
    return dt, None


async def create(pool, owner_id: int, target_url: str, title: str | None = None,
                 *, self_hosts: set[str] | None = None,
                 custom_code: str | None = None, utm: dict | None = None,
                 tags=None, expires_at=None) -> dict[str, Any]:
    """Создать короткую ссылку. {"ok":True, "code":..} или {"ok":False,"error":..}.

    Поддержка (как у bit.ly): свой код, UTM-метки, срок действия, теги. Дедуп по
    (owner, target) — только для ПРОСТОЙ ссылки без этих опций (не плодим дубли,
    копим клики в одном месте). С любой опцией всегда создаём новую.
    """
    url = normalize_url(target_url, self_hosts=self_hosts)
    if not url:
        return {"ok": False, "error": "Нужна корректная http(s)-ссылка"}
    url = apply_utm(url, utm)
    title = (title or "").strip()[:200] or None
    tags_s = _clean_tags(tags)
    exp_dt, exp_err = _parse_expires(expires_at)
    if exp_err:
        return {"ok": False, "error": exp_err}

    _advanced = bool(custom_code or tags_s or exp_dt or (utm and any(utm.values())))

    async def _insert(code: str):
        return await pool.fetchrow(
            "INSERT INTO short_links(code, owner_id, target_url, title, tags, expires_at) "
            "VALUES($1,$2,$3,$4,$5,$6) ON CONFLICT (code) DO NOTHING RETURNING code",
            code, int(owner_id), url, title, tags_s, exp_dt)

    def _ok(code):
        return {"ok": True, "code": code, "target_url": url, "title": title,
                "clicks": 0, "tags": tags_s,
                "expires_at": exp_dt.isoformat() if exp_dt else None}

    if custom_code is not None:
        custom_code = custom_code.strip().lstrip("/")
        if not valid_custom_code(custom_code):
            return {"ok": False, "error": "Код: 2–32 символа — латиница, цифры, «-», «_»; "
                                          "начинается с буквы/цифры и не зарезервированное слово"}
        try:
            _row = await _insert(custom_code)
        except Exception as e:
            log_exc_swallow(log, f"link_shortener: insert custom: {e}")
            return {"ok": False, "error": "Не удалось сохранить ссылку"}
        if not _row:
            return {"ok": False, "error": "Такой код уже занят — выберите другой"}
        return _ok(custom_code)

    if not _advanced:
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
            _row = await _insert(code)
        except Exception as e:
            log_exc_swallow(log, f"link_shortener: insert: {e}")
            return {"ok": False, "error": "Не удалось сохранить ссылку"}
        if _row:
            return _ok(code)
    return {"ok": False, "error": "Не удалось сгенерировать свободный код, повторите"}


async def resolve(pool, code: str) -> str | None:
    """Найти target_url по коду и ЗАСЧИТАТЬ переход. None — нет/отключена.

    Инкремент клика — тем же запросом (UPDATE ... RETURNING): без лишнего захода и
    без гонки. Ошибку счётчика глотаем — редирект важнее аналитики.
    """
    if not code or not _RESOLVE_RE.match(code) or is_reserved(code):
        return None
    # Истёкшие ссылки не ведут никуда (как «link expiration» у bit.ly).
    _live = "AND NOT disabled AND (expires_at IS NULL OR expires_at > now())"
    try:
        row = await pool.fetchrow(
            f"UPDATE short_links SET clicks=clicks+1, last_click_at=now() "
            f"WHERE code=$1 {_live} RETURNING target_url",
            code)
    except Exception as e:
        log_exc_swallow(log, f"link_shortener: resolve: {e}")
        # Фолбэк: хотя бы отдать цель без учёта клика.
        try:
            row = await pool.fetchrow(
                f"SELECT target_url FROM short_links WHERE code=$1 {_live}", code)
        except Exception:
            return None
    return row["target_url"] if row else None


async def list_for_owner(pool, owner_id: int, limit: int = 100) -> list[dict]:
    import datetime as _dt
    try:
        rows = await pool.fetch(
            "SELECT code, target_url, title, clicks, disabled, created_at, "
            "last_click_at, expires_at, tags "
            "FROM short_links WHERE owner_id=$1 ORDER BY created_at DESC LIMIT $2",
            int(owner_id), int(max(1, min(limit, 500))))
    except Exception as e:
        log_exc_swallow(log, f"link_shortener: list: {e}")
        return []
    _now = _dt.datetime.now(_dt.timezone.utc)
    out = []
    for r in rows or []:
        _exp = r["expires_at"] if "expires_at" in r.keys() else None
        _expired = bool(_exp and _exp <= _now)
        out.append({
            "code": r["code"], "target_url": r["target_url"], "title": r["title"],
            "clicks": int(r["clicks"] or 0), "disabled": bool(r["disabled"]),
            "tags": (r["tags"] if "tags" in r.keys() else None),
            "expires_at": _exp.isoformat() if _exp else None,
            "expired": _expired,
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            "last_click_at": r["last_click_at"].isoformat() if r["last_click_at"] else None,
        })
    return out


def make_qr_data_uri(text: str, *, box_size: int = 8, border: int = 2) -> str | None:
    """QR-код для текста (короткой ссылки) → data:image/png;base64,…  или None.

    Генерим на сервере библиотекой qrcode (есть в requirements). Ошибку/отсутствие
    библиотеки глотаем — QR это дополнение, а не критичный путь.
    """
    if not text:
        return None
    try:
        import base64
        import io
        import qrcode
        qr = qrcode.QRCode(box_size=box_size, border=border)
        qr.add_data(text)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception as e:
        log_exc_swallow(log, f"link_shortener: qr: {e}")
        return None


async def get_one(pool, owner_id: int, code: str) -> dict | None:
    """Одна ссылка владельца (для QR/статистики)."""
    try:
        r = await pool.fetchrow(
            "SELECT code, target_url, title, clicks FROM short_links "
            "WHERE code=$1 AND owner_id=$2", code, int(owner_id))
    except Exception as e:
        log_exc_swallow(log, f"link_shortener: get_one: {e}")
        return None
    if not r:
        return None
    return {"code": r["code"], "target_url": r["target_url"],
            "title": r["title"], "clicks": int(r["clicks"] or 0)}


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
