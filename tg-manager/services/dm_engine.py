"""
DM Engine — отправка личных сообщений для DM-кампаний.

Поддерживает:
- Spintax {Привет|Здравствуйте|Добрый день}
- Humanized delays между отправками
- Классификацию ошибок (flood/blocked/deactivated/permission)
- Дедупликацию через dm_campaign_log
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time

import asyncpg
from aiogram import Bot

from services.logger import log_exc_swallow
from services import infra_memory

log = logging.getLogger(__name__)

_MAX_MEDIA_BYTES = 20 * 1024 * 1024  # потолок скачивания медиа кампании

_CT_EXT = {
    "image/jpeg": ".jpg", "image/pjpeg": ".jpg", "image/png": ".png",
    "image/webp": ".webp", "image/gif": ".gif", "video/mp4": ".mp4",
    "video/quicktime": ".mov", "application/pdf": ".pdf",
}


def _media_filename(url: str, content_type: str) -> str:
    """Имя файла для BytesIO — Telethon по расширению решает фото/видео/документ."""
    ct = (content_type or "").split(";")[0].strip().lower()
    ext = _CT_EXT.get(ct)
    if not ext:
        tail = url.split("?")[0].rsplit("/", 1)[-1]
        if "." in tail:
            ext = "." + tail.rsplit(".", 1)[-1][:5].lower()
        else:
            ext = ".jpg"
    return "media" + ext


async def _download_media(url: str) -> tuple[bytes, str]:
    """Скачать медиа кампании один раз. Возвращает (bytes, filename). Бросает при
    неверной схеме/внутреннем адресе/пустом/слишком большом файле/сетевой ошибке.

    SSRF-гард здесь ОБЯЗАТЕЛЕН: это реальный сетевой sink, отложенный от момента
    сабмита — воркер тянет URL позже, уже без пользователя. Проверки не было
    вовсе, а http:// пускался как есть, то есть http://169.254.169.254 (метаданные
    облака) был достижим со стороны сервера. Резолвим DNS перед КАЖДЫМ
    скачиванием: доверять проверке на сабмите нельзя, адрес мог смениться.
    """
    from services.security import resolve_url_is_public
    if not await resolve_url_is_public(url):
        raise ValueError(
            "URL медиа должен быть публичным https (внутренние адреса запрещены)")
    import aiohttp

    async with aiohttp.ClientSession() as sess:
        async with sess.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            if resp.status >= 400:
                raise ValueError(f"URL вернул HTTP {resp.status}")
            ct = resp.headers.get("Content-Type", "")
            data = await resp.read()
    if not data:
        raise ValueError("Пустой ответ по URL")
    if len(data) > _MAX_MEDIA_BYTES:
        raise ValueError(f"Медиа слишком большое ({len(data) // 1024 // 1024} МБ)")
    return data, _media_filename(url, ct)


# ── Spintax ───────────────────────────────────────────────────────────────────


def _expand_spintax_naive(text: str) -> str:
    """Запасной разворот: одна группа за проход, случайный вариант.

    Оставлен как фолбэк, а не как основной путь: движок строже и на реальных
    пользовательских текстах падает там, где эта версия просто возвращает
    текст — незакрытая скобка, `{{CITY}}` из генератора инфраструктуры, `{}`.
    Уронить рассылку из-за скобки в тексте недопустимо.
    """
    def _replace(m: re.Match) -> str:
        parts = m.group(1).split("|")
        return random.choice(parts)

    guard = 0
    while "{" in text and "}" in text and guard < 20:
        new_text = re.sub(r"\{([^{}]+)\}", _replace, text)
        if new_text == text:
            break
        text = new_text
        guard += 1
    return text


def expand_spintax(text: str) -> str:
    """Развернуть spintax `{A|B|C}` — один случайный вариант.

    Единая точка входа для массовых операций. Логика делегирована
    `services/spintax_engine` (лексер + парсер + валидатор): он корректно
    разбирает вложенность, тогда как построчная замена регуляркой раскрывает
    внутреннюю группу раньше внешней и даёт не тот вариант, который написал
    пользователь.

    Fail-soft по устройству: движок валидирующий и бросает на текстах, которые
    в рассылках встречаются постоянно (незакрытая скобка, `{{CITY}}`,
    пустая группа). Исключение здесь означало бы оборванную посреди сети
    рассылку, поэтому при любой ошибке разбора возвращаемся к прежнему
    поведению, а не наверх.
    """
    if not text or "{" not in text:
        return text
    try:
        from services import spintax_service

        return spintax_service.expand_template(text)
    except Exception:
        return _expand_spintax_naive(text)


# ── Персонализация ────────────────────────────────────────────────────────────

# Значение по умолчанию, когда имени у получателя нет. Пустая строка ломала бы
# фразу («Привет, !»), поэтому нужен нейтральный запасной вариант.
_DEFAULT_NAME_FALLBACK = "друг"
_PERSONAL_PLACEHOLDERS = ("name", "first_name", "last_name", "username")
_PLACEHOLDER_RE = re.compile(r"\{(name|first_name|last_name|username)\}")
# Подставляемое значение не должно превращаться в spintax-разметку или в
# полстраницы текста: имя в Telegram задаёт сам получатель.
_MAX_NAME_LEN = 64


def _clean_name_value(value: str | None) -> str:
    if not value:
        return ""
    s = str(value)
    for ch in "{}|":
        s = s.replace(ch, "")
    return s.strip()[:_MAX_NAME_LEN]


def personalize(template: str, target: dict | None, fallback: str | None = None) -> str:
    """Подставить имя получателя в шаблон ДО разворота spintax.

    Порядок обязателен: spintax-движок видит `{name}` как группу из одного
    варианта и превращает её в голое слово «name». То есть до этого фикса
    очевидный синтаксис персонализации молча портил текст — пользователь
    рассылал «Привет, name!» тысячам людей.

    Поддерживает {name}, {first_name}, {last_name}, {username}. {name} — самый
    удобный: имя, иначе @username, иначе запасное слово.

    Чистая функция: тестируется отдельно от send-цикла.
    """
    if not template or "{" not in template:
        return template or ""
    if not _PLACEHOLDER_RE.search(template):
        return template
    t = target or {}
    fb = _clean_name_value(fallback) or _DEFAULT_NAME_FALLBACK
    first = _clean_name_value(t.get("first_name"))
    last = _clean_name_value(t.get("last_name"))
    uname = _clean_name_value(t.get("username")).lstrip("@")

    values = {
        "first_name": first or fb,
        "last_name": last or fb,
        "username": ("@" + uname) if uname else fb,
        # Живое обращение: имя → ник → нейтральное слово.
        "name": first or (("@" + uname) if uname else fb),
    }
    return _PLACEHOLDER_RE.sub(lambda m: values[m.group(1)], template)


def template_uses_personalization(template: str) -> bool:
    """Есть ли в тексте плейсхолдеры персонализации (для превью и подсказок)."""
    return bool(template) and bool(_PLACEHOLDER_RE.search(template))


def parse_import_list(raw, limit: int = 5000) -> list[dict]:
    """Разбирает пользовательский список получателей → [{user_id, username}].

    Принимает список или строку (по строке/запятой на получателя). Каждый элемент —
    либо числовой Telegram user_id, либо @username / t.me/<name>. Мусор отсекается,
    дедуп по (user_id||username), потолок против гигантских вставок. Общий хелпер
    для composer'а import_list и любых мест разбора списков адресатов.
    """
    if isinstance(raw, str):
        raw = re.split(r"[\n,;]+", raw)
    out: list[dict] = []
    seen: set = set()
    for item in (raw or []):
        if isinstance(item, dict):
            uid = int(item.get("user_id") or 0)
            uname = (item.get("username") or "").lstrip("@").strip() or None
        else:
            s = str(item).strip()
            if not s:
                continue
            if s.lstrip("-").isdigit():
                uid, uname = int(s), None
            else:
                m = re.match(r"^(?:https?://t\.me/|t\.me/|@)?([A-Za-z0-9_]{3,32})/?$", s)
                if not m:
                    continue
                uid, uname = 0, m.group(1)
        if uid <= 0 and not uname:
            continue
        key = uid if uid > 0 else uname.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append({"user_id": uid, "username": uname})
        if len(out) >= limit:
            break
    return out


# ── Реестр «не писать» (opt-out) ──────────────────────────────────────────────
#
# `contact_opt_out` наполняется явным действием оператора («человек попросил
# больше не писать») и до сих пор применялся ТОЛЬКО в масс-инвайте. Рассылки
# его не знали: тот, кто просил не писать, получал следующую кампанию — это и
# юридический риск, и прямая причина спам-репортов, от которых горят аккаунты.
# Фильтр ниже — общая точка для кампаний и разовой рассылки.


def opt_out_keys(user_id=None, username: str | None = None) -> list[str]:
    """Ключи, под которыми получатель мог попасть в реестр отказов.

    Реестр хранит нормализованные строки ('@username' в нижнем регистре,
    голый numeric id, телефон в E.164). У получателя рассылки есть id и/или
    username, поэтому проверяем оба ключа. Дополнительно прогоняем id через
    normalize_target: у него своя граница id/телефон (11+ цифр → '+...'), и без
    этого длинный id, занесённый оператором, тихо не совпал бы.
    """
    keys: list[str] = []
    try:
        uid = int(user_id or 0)
    except (TypeError, ValueError):
        uid = 0
    if uid > 0:
        keys.append(str(uid))
        try:
            from services.contact_opt_out import normalize_target

            norm = normalize_target(str(uid))
            if norm and norm not in keys:
                keys.append(norm)
        except Exception:
            pass
    if username:
        uname = str(username).lstrip("@").strip().lower()
        if uname:
            keys.append("@" + uname)
    return keys


def filter_opted_out(targets: list[dict], opted_out: set[str]) -> tuple[list[dict], int]:
    """Убрать из списка получателей тех, кто просил не писать.

    Чистая функция (без БД) — тестируется отдельно от send-цикла.
    Возвращает (оставшиеся, сколько убрано).
    """
    if not opted_out or not targets:
        return list(targets or []), 0
    remaining = [
        t for t in targets
        if not any(k in opted_out for k in opt_out_keys(t.get("user_id"), t.get("username")))
    ]
    return remaining, len(targets) - len(remaining)


def filter_opted_out_refs(refs: list, opted_out: set[str]) -> tuple[list, int]:
    """То же для плоского списка ссылок (@username / id) — разовая рассылка.

    Отдельная функция, а не contact_opt_out.filter_targets: та сравнивает
    строки как есть и не приводит к канону голый 'ivan' без «собаки», из-за
    чего отказ по '@ivan' для такого ввода не срабатывал бы.
    """
    if not opted_out or not refs:
        return list(refs or []), 0
    remaining = []
    for ref in refs:
        s = str(ref).strip()
        if s.lstrip("-").isdigit():
            keys = opt_out_keys(user_id=s.lstrip("-"))
        else:
            keys = opt_out_keys(username=s)
        if any(k in opted_out for k in keys):
            continue
        remaining.append(ref)
    return remaining, len(refs) - len(remaining)


def pick_account_under_cap(acc_cycle: list, start_idx: int, sent_by_acc: dict, cap):
    """Выбирает следующий аккаунт из ротации, пропуская достигших дневного лимита.

    Возвращает (acc_dict | None, new_idx). None — если все аккаунты исчерпали cap.
    Чистая функция (без БД) — тестируема отдельно от send-цикла. cap=None/0 → без лимита.
    """
    if not acc_cycle:
        return None, start_idx
    idx = start_idx
    for _ in range(len(acc_cycle)):
        cand = dict(acc_cycle[idx % len(acc_cycle)])
        idx += 1
        if cap and sent_by_acc.get(int(cand["id"]), 0) >= cap:
            continue
        return cand, idx
    return None, idx


# ── Error classification ──────────────────────────────────────────────────────

_SKIP_ERRORS = {
    "UserDeactivatedBan",
    "UserDeactivated",
    "UserNotMutualContact",
    "UserPrivacyRestricted",
    "InputUserDeactivated",
}
_FLOOD_ERRORS = {"FloodWaitError", "FloodWait"}
# PER-TARGET блокировки: конкретный юзер заблокировал/запретил запись. Аккаунт
# при этом ЗДОРОВ — такую ошибку нельзя трактовать как «убрать аккаунт».
_BLOCKED_ERRORS = {
    "UserBlockedBan",
    "YouBlockedUser",
    "ChatWriteForbidden",
    "UserBannedInChannel",
}
# ACCOUNT-LEVEL флаг: аккаунт помечен Telegram за спам пользователям ВООБЩЕ.
# Отделён от per-target: PeerFlood должен убрать аккаунт из ротации И поставить
# длинный cooldown (иначе следующая операция сразу добьёт флагнутый аккаунт).
_PEER_FLOOD_ERRORS = {"PeerFloodError"}


def _classify_error(exc: Exception) -> str:
    """Возвращает 'flood' | 'peer_flood' | 'blocked' | 'skip' | 'auth' | 'retry'."""
    name = type(exc).__name__
    exc_str = str(exc)
    if name in _FLOOD_ERRORS or "FLOOD_WAIT" in exc_str.upper():
        return "flood"
    if name in _PEER_FLOOD_ERRORS or "PEER_FLOOD" in exc_str:
        return "peer_flood"
    if name in _BLOCKED_ERRORS:
        return "blocked"
    if name in _SKIP_ERRORS:
        return "skip"
    if "AUTH_KEY" in exc_str or "SESSION_REVOKED" in exc_str or "Unauthorized" in name:
        return "auth"
    return "retry"


# Ошибки уровня ПОЛУЧАТЕЛЯ: доставить нельзя никогда, но аккаунт здоров и
# обязан продолжать работу. Строки — ровно те, что формирует
# account_manager.send_dm; имена Telethon-классов добавлены на случай, когда
# наверх просачивается сырой текст исключения.
_PERMANENT_TARGET_MARKERS = (
    "приватность",
    "заблокирован",
    "нет доступа",
    "аккаунт удалён",
    "username не существует",
    "userprivacyrestricted",
    "userisblocked",
    "chatwriteforbidden",
    "inputuserdeactivated",
    "usernamenotoccupied",
    "usernameinvalid",
)


def classify_send_result(result: dict) -> str:
    """Свести ответ account_manager.send_dm к исходу.

    Возвращает 'sent' | 'flood' | 'peer_flood' | 'auth' | 'skip' | 'retry'.

    Нужна потому, что разовая рассылка складывала ВСЁ в «ошибку»: флуд-вейт,
    флаг PeerFlood (аккаунт помечен за спам) и мёртвую сессию было не отличить
    от «у получателя закрыты личные сообщения». Аккаунт под PeerFlood при этом
    оставался в ротации и продолжал слать — самый быстрый способ его потерять.

    Отдельно: send_dm НИКОГДА не возвращает ключ 'banned', поэтому проверять
    его бессмысленно — мёртвая сессия распознаётся по тексту ошибки.
    """
    if not isinstance(result, dict):
        return "retry"
    if result.get("ok"):
        return "sent"
    if result.get("flood_wait"):
        return "flood"
    if result.get("peer_flood"):
        return "peer_flood"
    err = str(result.get("error") or "")
    low = err.lower()
    if "peer_flood" in low or "peerflood" in low:
        return "peer_flood"
    if "flood_wait" in low or "floodwait" in low:
        return "flood"
    try:
        from services.account_manager import is_dead_session_error

        if is_dead_session_error(err):
            return "auth"
    except Exception:
        pass
    if any(m in low for m in ("auth_key", "session_revoked", "unauthorized")):
        return "auth"
    if any(m in low for m in _PERMANENT_TARGET_MARKERS):
        return "skip"
    return "retry"


def _extract_flood_seconds(exc: Exception) -> int:
    """Извлекает количество секунд флуд-вейта из исключения."""
    for attr in ("seconds", "x"):
        val = getattr(exc, attr, None)
        if isinstance(val, (int, float)):
            return int(val)
    m = re.search(r"(\d+)", str(exc))
    return int(m.group(1)) if m else 60


# ── Core send ─────────────────────────────────────────────────────────────────


async def send_dm(
    session_str: str,
    user_id: int,
    text: str,
    _acc: dict | None = None,
    username: str | None = None,
    media_url: str | None = None,
    media_bytes: bytes | None = None,
    media_filename: str | None = None,
    uniquify_media: bool = False,
) -> dict:
    """
    Отправить одно личное сообщение через личный аккаунт Telegram.

    Стратегия адресации (Telethon требует entity с access_hash):
    1. Если есть username — используем его (@handle), это самый надёжный способ.
    2. Если username нет — используем числовой user_id. Telethon попробует
       разрешить его через contacts.GetContacts или по кэшу сессии.
       Если аккаунт ранее видел этого пользователя — работает. Иначе — retry error.

    Возвращает {'status': 'sent'|'flood'|'blocked'|'skip'|'auth'|'retry', 'wait': int, 'error': str}
    """
    from services import account_manager

    # Prefer username for reliable entity resolution in Telethon.
    # Raw integer user_id requires the session to have seen this user before
    # (have their access_hash in cache). Username resolves via contacts.Search.
    target: str | int = username.lstrip("@") if username else user_id

    async def _deliver(dest):
        if media_bytes is not None:
            # Массовая рассылка: медиа скачано один раз, уникализируем под каждого
            # получателя (анти-детект). URL валидируется на уровне API (SSRF-гард).
            await account_manager.send_media_via_account(
                session_str, dest, caption=text, _acc=_acc,
                media_bytes=media_bytes, media_filename=media_filename,
                uniquify=uniquify_media,
            )
        elif media_url:
            # Медиа с подписью (caption). URL валидируется на уровне API (SSRF-гард).
            await account_manager.send_media_via_account(session_str, dest, media_url, text, _acc=_acc)
        else:
            await account_manager.send_message(session_str, dest, text, _acc=_acc)

    try:
        await _deliver(target)
        return {"status": "sent"}
    except Exception as exc:
        kind = _classify_error(exc)
        wait = _extract_flood_seconds(exc) if kind == "flood" else 0
        # If username resolution failed and we have user_id, fall back to int
        if kind == "retry" and username and user_id:
            try:
                await _deliver(user_id)
                return {"status": "sent"}
            except Exception as exc2:
                kind2 = _classify_error(exc2)
                wait2 = _extract_flood_seconds(exc2) if kind2 == "flood" else 0
                return {"status": kind2, "wait": wait2, "error": str(exc2)[:200]}
        return {"status": kind, "wait": wait, "error": str(exc)[:200]}


# ── Campaign runner ────────────────────────────────────────────────────────────

# Задержки подобраны по типу аудитории: чем незнакомее аудитория, тем длиннее задержка
_DELAYS_BY_TARGET_TYPE: dict[str, tuple[float, float]] = {
    "bot_users":       (35.0, 90.0),    # свои подписчики — наиболее безопасно
    "cohort":          (35.0, 90.0),
    "all_bots":        (40.0, 100.0),   # агрегат всех своих ботов
    "crm":             (45.0, 110.0),   # легаси-таблица crm_contacts
    "contacts":        (45.0, 110.0),   # все контакты из «Контактов»
    "segment":         (45.0, 110.0),   # срез контактов — тот же класс риска
    "parsed_audience": (55.0, 130.0),   # спарсенные — незнакомые
    "import_list":     (70.0, 160.0),   # внешний список — максимальная осторожность
}
# Потолок выборки сегмента на кампанию: аудитория целиком грузится в память,
# а при задержке 45–110 с даже 20k получателей — это недели отправки.
_SEGMENT_TARGET_LIMIT = 20000
# Тот же потолок для остальных источников аудитории. Раньше выборка была
# неограниченной: аккаунт с сотнями тысяч спарсенных контактов грузил их все в
# память на старте кампании. Практического смысла в большем нет — при паузе
# 45–110 с на сообщение даже 20k получателей это недели отправки.
_MAX_CAMPAIGN_TARGETS = 20000

# Множители пользовательского темпа. Держим рядом с таблицей задержек: расчёт
# срока и сам send-цикл обязаны использовать ОДНИ и те же числа, иначе UI будет
# обещать одно, а движок делать другое.
_PACE_MULTIPLIERS: dict[str, float] = {"slow": 2.0, "normal": 1.0, "fast": 0.5}


async def _fleet_tempo_mult(pool, owner_id: int) -> float:
    """Множитель темпа по давлению флота (>= 1.0). Fail-open → 1.0.

    Тот же губернатор, под которым работают инвайт и разовая рассылка: если
    флот уже ловит флуды и баны, кампания обязана замедлиться вместе со всеми,
    а не добивать его своим темпом.
    """
    try:
        from services import fleet_governor

        mult = float(await fleet_governor.tempo_multiplier(pool, owner_id) or 1.0)
        return mult if mult >= 1.0 else 1.0
    except Exception:
        return 1.0


def accounts_awake(accounts: list[dict], now=None) -> list[dict]:
    """Аккаунты, у которых сейчас НЕ локальная ночь (по стране прокси).

    Кампания на тысячи получателей идёт сутками, поэтому неизбежно попадает на
    ночь: раньше движок слал ЛС живым людям в 4 утра — это и низкий отклик, и
    жалобы на спам, от которых горят аккаунты.

    Гео неизвестно (нет прокси/страны) → аккаунт считается бодрствующим: молча
    останавливать рассылку из-за незаполненного поля нельзя.
    """
    if not accounts:
        return []
    try:
        from services import geo_tempo
    except Exception:
        return list(accounts)
    awake = []
    for a in accounts:
        try:
            if not geo_tempo.is_local_night(a.get("geo_country"), now):
                awake.append(a)
        except Exception:
            awake.append(a)  # сбой определения — не повод пропускать аккаунт
    return awake


def estimate_duration_seconds(target_type: str, pace: str | None, remaining: int) -> int:
    """Сколько примерно займёт рассылка на `remaining` получателей.

    Отправка идёт последовательно: аккаунты чередуются, но пауза выдерживается
    между сообщениями глобально — поэтому число аккаунтов срок НЕ сокращает.

    Нужна потому, что пользователь запускал кампанию на тысячи получателей, не
    имея никакого представления о сроке: при паузе 45–110 с это недели, а на
    экране было только «0/5000 отправлено».

    'auto' считаем как обычный темп: реальный множитель движок берёт из
    состояния флота на старте, заранее он неизвестен.
    """
    try:
        n = max(0, int(remaining))
    except (TypeError, ValueError):
        return 0
    if not n:
        return 0
    dmin, dmax = _DELAYS_BY_TARGET_TYPE.get(target_type or "", _DEFAULT_DELAY_RANGE)
    mult = _PACE_MULTIPLIERS.get(pace or "normal", 1.0)
    return int(n * ((dmin + dmax) / 2.0) * mult)
_DEFAULT_DELAY_RANGE: tuple[float, float] = (45.0, 110.0)
_FLOOD_PAUSE = 120  # пауза при flood (если нет явного wait)
# Предохранитель ночного ожидания: если гео у всех аккаунтов заполнено криво,
# кампания не должна встать навсегда — через 10 часов продолжаем.
_QUIET_MAX_WAIT_S = 10 * 3600
_PEER_FLOOD_COOLDOWN = 48 * 3600  # PeerFlood — аккаунт-флаг: длинный cooldown (48ч)


async def _get_targets(pool: asyncpg.Pool, campaign: dict) -> list[dict]:
    """Получить список получателей для кампании.

    Возвращает list[dict] с ключами: user_id (int), username (str | None).
    username используется для надёжной адресации через Telethon (get_entity).
    """
    campaign_id = campaign["id"]
    target_type = campaign["target_type"]
    target_id = campaign["target_id"]

    # Отсечение уже обработанных перенесено в SQL (подзапрос ниже): раньше вся
    # аудитория грузилась в память целиком и фильтровалась в Python — на базе в
    # сотни тысяч контактов это тяжёлый старт кампании и лишние десятки МБ.
    # Python-фильтр по sent_ids оставлен вторым рубежом: он дешёв (SQL уже
    # отсеял) и страхует источники, где подзапрос неприменим (import_list).
    _EXCLUDE_SENT = (
        "NOT IN (SELECT tg_user_id FROM dm_campaign_log "
        "WHERE campaign_id=$%d AND status IN ('sent','blocked','skip'))"
    )

    # Исключить уже успешно отправленных, заблокированных и пропущенных (не retry)
    sent_ids = {
        r["tg_user_id"]
        for r in await pool.fetch(
            "SELECT tg_user_id FROM dm_campaign_log WHERE campaign_id=$1 AND status IN ('sent','blocked','skip')",
            campaign_id,
        )
    }

    if target_type == "bot_users" and target_id:
        # Fetch user_id + username so Telethon can resolve the entity reliably.
        # JOIN managed_bots ... added_by — защита от IDOR: рассылка только по
        # подписчикам бота, принадлежащего владельцу кампании.
        rows = await pool.fetch(
            "SELECT DISTINCT ON (bu.user_id) bu.user_id, bu.username, bu.first_name "
            "FROM bot_users bu JOIN managed_bots mb ON mb.bot_id=bu.bot_id "
            "WHERE bu.bot_id=$1 AND mb.added_by=$2 AND bu.user_id > 0 "
            "AND bu.user_id " + (_EXCLUDE_SENT % 3) +
            f" ORDER BY bu.user_id LIMIT {_MAX_CAMPAIGN_TARGETS}",
            target_id, campaign["owner_id"], campaign_id,
        )
        return [
            {"user_id": r["user_id"], "username": r["username"] or None,
             "first_name": r.get("first_name")}
            for r in rows
            if r["user_id"] not in sent_ids
        ]
    elif target_type == "crm":
        rows = await pool.fetch(
            "SELECT DISTINCT ON (tg_user_id) tg_user_id, username, first_name "
            "FROM crm_contacts WHERE owner_id=$1 AND tg_user_id > 0 "
            "AND tg_user_id " + (_EXCLUDE_SENT % 2) +
            f" ORDER BY tg_user_id LIMIT {_MAX_CAMPAIGN_TARGETS}",
            campaign["owner_id"], campaign_id,
        )
        return [
            {"user_id": r["tg_user_id"], "username": r["username"] or None,
             "first_name": r.get("first_name")}
            for r in rows
            if r["tg_user_id"] not in sent_ids
        ]
    elif target_type in ("segment", "contacts"):
        # Аудитория из «Контактов» (unified_contacts) — тем же движком сегментов,
        # что питает экран контактов, поэтому «вижу = пишу».
        #
        # 'contacts' — ВСЕ контакты, 'segment' — сохранённый срез («Горячие»,
        # «Молчуны 30д»). Раньше единственным контактным таргетом был 'crm',
        # который читает ЛЕГАСИ-таблицу crm_contacts: её не показывает ни один
        # экран, так что пользователь целился в одно, а письма уходили в другое
        # (обычно — никому). 'crm' оставлен рабочим ради уже созданных кампаний.
        from services.contacts_hub import repository as _repo

        if target_type == "segment":
            if not target_id:
                return []
            filters = await _repo.get_segment_filters(
                pool, campaign["owner_id"], int(target_id))
            if filters is None:
                # Сегмент удалён или принадлежит другому владельцу — не молчим,
                # иначе кампания «успешно» уйдёт в пустоту.
                raise ValueError(f"Сегмент #{target_id} не найден")
        else:
            filters = {}  # весь список контактов
        rows = await _repo.resolve_segment(
            pool, campaign["owner_id"], filters, limit=_SEGMENT_TARGET_LIMIT
        )
        out: list[dict] = []
        seen_uids: set[int] = set()
        for r in rows:
            uid = int(r.get("telegram_user_id") or 0)
            uname = (r.get("username") or "").lstrip("@").strip() or None
            # Для ЛС нужен user_id или @username: контакт, у которого есть только
            # телефон, адресовать нечем — молча пропускаем, он не «ошибка».
            if uid <= 0 and not uname:
                continue
            if uid and (uid in sent_ids or uid in seen_uids):
                continue
            if uid:
                seen_uids.add(uid)
            out.append({
                "user_id": uid, "username": uname,
                "first_name": r.get("first_name"), "last_name": r.get("last_name"),
            })
        return out
    elif target_type == "cohort" and target_id:
        # target_id = bot_id, params.cohort_type = hot|warm|cold|lost
        import json as _json

        params = campaign.get("params") or {}
        if isinstance(params, str):
            try:
                params = _json.loads(params)
            except Exception:
                log.debug('campaign params parse error')
                params = {}
        cohort = params.get("cohort_type", "warm")
        cohort_sql = {
            "hot": "ua.last_seen >= now() - INTERVAL '1 day'",
            "warm": "ua.last_seen >= now() - INTERVAL '7 days' AND ua.last_seen < now() - INTERVAL '1 day'",
            "cold": "ua.last_seen >= now() - INTERVAL '30 days' AND ua.last_seen < now() - INTERVAL '7 days'",
            "lost": "ua.last_seen < now() - INTERVAL '30 days'",
        }.get(cohort, "ua.last_seen >= now() - INTERVAL '7 days'")
        # JOIN bot_users to get username for reliable entity resolution
        rows = await pool.fetch(
            f"SELECT ua.user_id, bu.username, bu.first_name "
            f"FROM user_activity ua "
            f"JOIN managed_bots mb ON mb.bot_id = ua.bot_id AND mb.added_by=$2 "
            f"LEFT JOIN bot_users bu ON bu.bot_id = ua.bot_id AND bu.user_id = ua.user_id "
            f"WHERE ua.bot_id=$1 AND {cohort_sql} "
            f"AND ua.user_id {_EXCLUDE_SENT % 3} "
            f"ORDER BY ua.user_id LIMIT {_MAX_CAMPAIGN_TARGETS}",
            target_id, campaign["owner_id"], campaign_id,
        )
        return [
            {"user_id": r["user_id"], "username": r["username"] or None,
             "first_name": r.get("first_name")}
            for r in rows
            if r["user_id"] not in sent_ids
        ]
    elif target_type == "parsed_audience":
        # target_id = parse_run_id (0 = all runs for this owner)
        # Фильтр по полу (services/gender_classifier): campaign.params.gender_filter
        # ∈ {'m','f'} → шлём только размеченным этим полом (NULL/неизвестные
        # отсекаются). Смыкает аудиторные модули с массовой рассылкой end-to-end.
        import json as _json
        _p = campaign.get("params") or {}
        if isinstance(_p, str):
            try:
                _p = _json.loads(_p)
            except Exception:
                _p = {}
        gender_filter = _p.get("gender_filter")
        conditions = "owner_id=$1 AND tg_user_id > 0"
        params_list: list = [campaign["owner_id"]]
        if target_id:
            params_list.append(target_id)
            conditions += f" AND parse_run_id=${len(params_list)}"
        if gender_filter in ("m", "f"):
            params_list.append(gender_filter)
            conditions += f" AND gender=${len(params_list)}"
        params_list.append(campaign_id)
        rows = await pool.fetch(
            f"SELECT DISTINCT ON (tg_user_id) tg_user_id, username, first_name "
            f"FROM parsed_audiences WHERE {conditions} "
            f"AND tg_user_id {_EXCLUDE_SENT % len(params_list)} "
            f"ORDER BY tg_user_id LIMIT {_MAX_CAMPAIGN_TARGETS}",
            *params_list,
        )
        return [
            {"user_id": r["tg_user_id"], "username": r["username"] or None,
             "first_name": r.get("first_name")}
            for r in rows
            if r["tg_user_id"] not in sent_ids
        ]
    elif target_type == "all_bots":
        rows = await pool.fetch(
            "SELECT DISTINCT ON (bu.user_id) bu.user_id, bu.username, bu.first_name "
            "FROM bot_users bu JOIN managed_bots mb ON mb.bot_id = bu.bot_id "
            "WHERE mb.added_by=$1 AND bu.user_id > 0 "
            "AND bu.user_id " + (_EXCLUDE_SENT % 2) +
            f" ORDER BY bu.user_id, bu.added_at DESC LIMIT {_MAX_CAMPAIGN_TARGETS}",
            campaign["owner_id"], campaign_id,
        )
        return [
            {"user_id": r["user_id"], "username": r["username"] or None,
             "first_name": r.get("first_name")}
            for r in rows
            if r["user_id"] not in sent_ids
        ]
    elif target_type == "import_list":
        import json as _json

        params = campaign.get("params") or {}
        if isinstance(params, str):
            try:
                params = _json.loads(params)
            except Exception:
                log.debug('campaign params parse error')
                params = {}
        import_items = params.get("import_list", [])
        targets = []
        for item in import_items:
            uid = int(item.get("user_id") or 0)
            uname = item.get("username") or None
            if not uid and not uname:
                continue
            if uid and uid in sent_ids:
                continue
            targets.append({"user_id": uid, "username": uname})
        return targets
    return []


async def run_campaign(
    pool: asyncpg.Pool,
    bot: Bot,
    campaign_id: int,
    op_id: int | None = None,
) -> None:
    """Запустить или продолжить DM-кампанию. Вызывается из operation_queue."""
    campaign = await pool.fetchrow(
        "SELECT * FROM dm_campaigns WHERE id=$1", campaign_id
    )
    if not campaign:
        log.error("dm_engine: campaign %d not found", campaign_id)
        return

    campaign = dict(campaign)
    owner_id = campaign["owner_id"]

    # Пометить как running
    await pool.execute(
        "UPDATE dm_campaigns SET status='running', started_at=COALESCE(started_at, now()) WHERE id=$1",
        campaign_id,
    )

    async def _fail(reason: str, hint: str = "") -> None:
        """Пометить кампанию упавшей И СКАЗАТЬ, почему.

        Раньше все три пути отказа молча ставили status='failed': пользователь
        видел в списке «ошибка» без единого слова причины и не мог понять, чинить
        ему аккаунты, аудиторию или сегмент.
        """
        await pool.execute(
            "UPDATE dm_campaigns SET status='failed' WHERE id=$1", campaign_id
        )
        try:
            from database import db as _db

            await _db.notify_if_enabled(
                pool, bot, owner_id, "op_complete",
                f"⚠️ <b>DM «{campaign.get('name') or campaign_id}» не запущена</b>\n\n"
                f"{reason}" + (f"\n\n💡 {hint}" if hint else ""),
                dedup_key=f"dm-fail:{campaign_id}",
            )
        except Exception:
            log_exc_swallow(log, "dm_engine: failure notification failed")

    try:
        from services.flood_engine import get_active_accounts

        accounts = await get_active_accounts(pool, owner_id)
    except Exception:
        log.exception(
            "dm_engine: get_active_accounts failed for campaign %d", campaign_id
        )
        await _fail("Не удалось получить список аккаунтов для отправки.",
                    "Попробуйте запустить кампанию ещё раз.")
        return

    if not accounts:
        log.error(
            "dm_engine: no active accounts for campaign %d owner=%d",
            campaign_id,
            owner_id,
        )
        await _fail("Нет активных аккаунтов для отправки ЛС.",
                    "Подключите аккаунт или снимите с них паузу/кулдаун.")
        return

    try:
        targets = await _get_targets(pool, campaign)
    except Exception as _texc:
        log.exception("dm_engine: _get_targets failed for campaign %d", campaign_id)
        await _fail(f"Не удалось собрать аудиторию: {str(_texc)[:150]}",
                    "Проверьте выбранную аудиторию — она могла быть удалена.")
        return

    # Реестр «не писать»: тот, кто явно просил больше не писать, не должен
    # получить НИ ОДНУ следующую кампанию. Fail-open — сбой реестра не срывает
    # рассылку (тот же принцип, что в масс-инвайте).
    opt_out_removed = 0
    try:
        from services import contact_opt_out as _coo

        _opted = await _coo.load_opted_out(pool, owner_id)
        if _opted:
            targets, opt_out_removed = filter_opted_out(targets, _opted)
            if opt_out_removed:
                log.info(
                    "dm_engine campaign=%d: отфильтровано %d получателей из реестра «не писать»",
                    campaign_id, opt_out_removed,
                )
    except Exception:
        log_exc_swallow(log, "dm_engine: opt-out filter failed")

    total = len(targets)
    await pool.execute(
        "UPDATE dm_campaigns SET total_targets=$1 WHERE id=$2", total, campaign_id
    )
    if op_id and total:
        try:
            await pool.execute(
                "UPDATE operation_queue SET total_items=$1 WHERE id=$2", total, op_id
            )
        except Exception as _e:
            log.warning("dm_engine: failed to set total_items for op=%s: %s", op_id, _e)

    if not targets:
        await pool.execute(
            "UPDATE dm_campaigns SET status='done', finished_at=now() WHERE id=$1",
            campaign_id,
        )
        # Пустая аудитория без объяснения выглядит как поломка. Если её обнулил
        # реестр отказов — говорим об этом прямо, иначе пользователь будет
        # перезапускать кампанию и гадать, почему «0 отправлено».
        try:
            from database import db as _db

            _why = (
                f"\n\n🚫 Все {opt_out_removed} получателей в реестре «не писать»."
                if opt_out_removed else
                "\n\nВ выбранной аудитории нет новых получателей "
                "(всем уже отправляли или список пуст)."
            )
            await _db.notify_if_enabled(
                pool, bot, owner_id, "op_complete",
                f"📨 <b>DM «{campaign['name']}»</b> — отправлять некому{_why}",
                dedup_key=f"dm-empty:{campaign_id}",
            )
        except Exception:
            log_exc_swallow(log, "dm_engine: empty-audience notification failed")
        return

    template = campaign["text_template"]
    # Brand injection for free-tier users (plain text — DMs don't use HTML parse_mode)
    try:
        from services import brand_injection as _bi
        if await _bi.is_user_free_tier(pool, owner_id):
            template = _bi.add_promo(template, html=False, context="dm")
    except Exception as e:
        log_exc_swallow(log, "run_campaign: import")

    _delay_min, _delay_max = _DELAYS_BY_TARGET_TYPE.get(
        campaign.get("target_type", ""), _DEFAULT_DELAY_RANGE
    )
    # Пользовательский темп (params.pace): slow безопаснее, fast быстрее (риск).
    _cp = campaign.get("params") or {}
    if isinstance(_cp, str):
        try:
            import json as _json
            _cp = _json.loads(_cp)
        except Exception:
            log.debug('campaign params parse error')
            _cp = {}
    # Чем заменить {name}, если у получателя нет ни имени, ни ника.
    _name_fallback = (_cp or {}).get("name_fallback") or None
    # Тихие часы включены по умолчанию: ночная рассылка живым людям — это и
    # низкий отклик, и жалобы на спам. Отключается явным quiet_hours=false.
    _quiet_hours = (_cp or {}).get("quiet_hours", True) is not False
    _media_url = (_cp or {}).get("media_url") or None
    # Массовая рассылка с медиа: скачиваем файл ОДИН раз, дальше уникализируем под
    # каждого получателя (анти-детект — иначе у всех одинаковый серверный хэш).
    # Скачивание не удалось → откат на прежний путь (Telethon качает URL сам).
    _media_bytes: bytes | None = None
    _media_filename: str | None = None
    if _media_url:
        try:
            _media_bytes, _media_filename = await _download_media(_media_url)
        except Exception as _mexc:  # noqa: BLE001 — не срываем кампанию из-за медиа
            log_exc_swallow(log, f"dm_engine: медиа не скачано ({_mexc}), шлём по URL")
            _media_bytes = None
    try:
        _pace = (_cp or {}).get("pace")
        if _pace == "auto":
            # «Авто»: темп из состояния ВСЕГО флота за сегодня (flood_engine),
            # а не три числа, выбранные вслепую. Паритет с масс-инвайтом: Telegram
            # смотрит на аккаунты как на группу, флуд у одного тормозит всех.
            # «Авто» не имеет права быть опаснее обычного — при сбое расчёта normal.
            try:
                from services.flood_engine import auto_strategy
                _st = await auto_strategy(pool, owner_id)
                _pace_mult = float(_st.get("pace_mult") or 1.0)
                log.info("dm_engine campaign=%s: авто-темп ×%.2f (%s)",
                         campaign_id, _pace_mult, _st.get("reason") or "")
            except Exception:
                log.debug("dm_engine: auto strategy unavailable campaign=%s", campaign_id)
                _pace_mult = 1.0
            _delay_min *= _pace_mult
            _delay_max *= _pace_mult
        else:
            # Те же множители, по которым UI считает срок кампании, — иначе
            # экран обещал бы одно, а движок выдерживал другое.
            _pace_mult = _PACE_MULTIPLIERS.get(_pace)
            if _pace_mult:
                _delay_min *= _pace_mult
                _delay_max *= _pace_mult
    except Exception as e:
        log.warning('dm_engine: pace_multiplier load failed: %s', e)

    # Лимит отправок на аккаунт В ДЕНЬ (params.per_account_daily). Считаем честно:
    # префиллим уже отправленное сегодня по всем кампаниям владельца, дальше
    # доучитываем в этом прогоне. Аккаунт, достигший лимита, выбывает из ротации —
    # ключевая защита аккаунтов от массовой рассылки одним аккаунтом.
    try:
        _per_acc_cap = int((_cp or {}).get("per_account_daily") or 0) or None
    except (TypeError, ValueError):
        _per_acc_cap = None
    _sent_by_acc: dict[int, int] = {}
    if _per_acc_cap:
        try:
            _rows_today = await pool.fetch(
                """SELECT l.account_id, COUNT(*) AS c
                   FROM dm_campaign_log l
                   JOIN dm_campaigns c ON c.id = l.campaign_id
                   WHERE c.owner_id=$1 AND l.status='sent'
                     AND l.sent_at >= date_trunc('day', now())
                   GROUP BY l.account_id""",
                owner_id,
            )
            for r in _rows_today:
                if r["account_id"] is not None:
                    _sent_by_acc[int(r["account_id"])] = int(r["c"])
        except Exception:
            log_exc_swallow(log, "dm_engine: per-account daily preload failed")

    acc_cycle = list(accounts)
    # Риск-пульс (Волна S/1B + M, fail-open): DM с флагнутого аккаунта = быстрый бан.
    # Отсеиваем карантинные; если фильтр опустошает — НЕ обнуляем (лучше рискнуть).
    try:
        from services import infra_memory as _im
        _kept = []
        for _a in acc_cycle:
            if not await _im.is_account_quarantined(pool, _a["id"]):
                _kept.append(_a)
        if _kept and len(_kept) != len(acc_cycle):
            log.info("dm_engine campaign=%s: пропущено %d аккаунтов в карантине",
                     campaign_id, len(acc_cycle) - len(_kept))
            acc_cycle = _kept
    except Exception:
        log_exc_swallow(log, "dm_engine: quarantine filter failed")
    acc_idx = 0
    sent = 0
    failed = 0
    _notified_milestones: set[int] = set()  # 25, 50, 75

    for target in targets:
        # target is a dict: {user_id: int, username: str | None}
        user_id: int = target["user_id"]
        username: str | None = target.get("username")

        # Проверить не отменена ли кампания (из UI кампаний → status='paused')
        current = await pool.fetchrow(
            "SELECT status FROM dm_campaigns WHERE id=$1", campaign_id
        )
        if current is None:
            # Кампанию удалили прямо во время рассылки. Прежняя проверка
            # (`if current and ...`) на отсутствие строки не реагировала: цикл
            # шёл дальше и отправлял ЛС РЕАЛЬНЫМ ЛЮДЯМ по удалённой кампании,
            # пока не падал на FK-вставке в журнал. Удаление — однозначное
            # намерение остановить, останавливаемся немедленно.
            log.info("dm_engine: campaign %d удалена во время рассылки — стоп", campaign_id)
            return
        if current["status"] == "paused":
            log.info("dm_engine: campaign %d paused", campaign_id)
            return

        # Реагировать на отмену операции из очереди (operation_queue.status='cancelled').
        # Без этого «Отмена» во вью операций не останавливала рассылку — DM продолжали
        # уходить до конца, хотя пользователь её отменил.
        if op_id:
            op_row = await pool.fetchrow(
                "SELECT status FROM operation_queue WHERE id=$1", op_id
            )
            if op_row and op_row["status"] == "cancelled":
                log.info(
                    "dm_engine: operation %s отменена → останавливаю кампанию %d",
                    op_id, campaign_id,
                )
                await pool.execute(
                    "UPDATE dm_campaigns SET status='paused' WHERE id=$1", campaign_id
                )
                return

        # Выбираем следующий аккаунт, пропуская достигших дневного лимита.
        # Тихие часы: не пишем живым людям среди ночи. Берём только аккаунты,
        # у которых сейчас не локальная ночь (страна прокси); если ночь у всех —
        # ждём, а не шлём. Ожидание дробим по минуте, чтобы пауза и отмена
        # кампании продолжали работать, а не блокировались до утра.
        _pool_now = acc_cycle
        if _quiet_hours:
            _awake = accounts_awake(acc_cycle)
            _slept = 0
            while not _awake and acc_cycle:
                if _slept == 0:
                    log.info("dm_engine campaign=%d: у всех аккаунтов ночь — ждём утра",
                             campaign_id)
                await asyncio.sleep(60)
                _slept += 60
                _cur = await pool.fetchrow(
                    "SELECT status FROM dm_campaigns WHERE id=$1", campaign_id)
                if _cur and _cur["status"] == "paused":
                    log.info("dm_engine: campaign %d paused во время ночного ожидания",
                             campaign_id)
                    return
                if op_id:
                    _oq = await pool.fetchrow(
                        "SELECT status FROM operation_queue WHERE id=$1", op_id)
                    if _oq and _oq["status"] == "cancelled":
                        await pool.execute(
                            "UPDATE dm_campaigns SET status='paused' WHERE id=$1", campaign_id)
                        return
                if _slept >= _QUIET_MAX_WAIT_S:
                    # Предохранитель: гео у всех могло оказаться кривым — лучше
                    # продолжить, чем встать навсегда.
                    log.warning("dm_engine campaign=%d: ночное ожидание превысило предел — продолжаем",
                                campaign_id)
                    break
                _awake = accounts_awake(acc_cycle)
            if _awake:
                _pool_now = _awake

        acc, acc_idx = pick_account_under_cap(_pool_now, acc_idx, _sent_by_acc, _per_acc_cap)
        if acc is None:
            # Все аккаунты исчерпали дневной лимит — пауза до следующего дня
            # (кампания возобновляема). Не failed: это штатная защита, не ошибка.
            log.info(
                "dm_engine: все аккаунты достигли дневного лимита (%s) — пауза кампании %d",
                _per_acc_cap, campaign_id,
            )
            await pool.execute(
                "UPDATE dm_campaigns SET status='paused' WHERE id=$1", campaign_id
            )
            return

        # Персонализация ДО spintax: иначе spintax-движок увидит `{name}` как
        # группу из одного варианта и подставит голое слово «name».
        text = expand_spintax(personalize(template, target, _name_fallback))
        t0_dm = time.monotonic()
        result = await send_dm(
            acc["session_str"], user_id, text, _acc=acc, username=username,
            media_url=_media_url, media_bytes=_media_bytes,
            media_filename=_media_filename, uniquify_media=_media_bytes is not None,
        )
        status = result["status"]

        if status == "sent":
            sent += 1
            _sent_by_acc[int(acc["id"])] = _sent_by_acc.get(int(acc["id"]), 0) + 1
            await pool.execute(
                "INSERT INTO dm_campaign_log(campaign_id, account_id, tg_user_id, status) "
                "VALUES ($1,$2,$3,'sent') ON CONFLICT DO NOTHING",
                campaign_id,
                acc["id"],
                user_id,
            )
            await pool.execute(
                "UPDATE dm_campaigns SET sent_count=sent_count+1 WHERE id=$1",
                campaign_id,
            )
            infra_memory.record_account_op(
                acc["id"], "dm_campaign", True, duration_s=time.monotonic() - t0_dm
            )
        elif status == "flood":
            wait = result.get("wait") or _FLOOD_PAUSE
            log.info(
                "dm_engine: flood wait %ds acc=%d (campaign %d)",
                wait,
                acc["id"],
                campaign_id,
            )
            # Установить cooldown_until для аккаунта
            try:
                await pool.execute(
                    "UPDATE tg_accounts SET cooldown_until = NOW() + ($1 * INTERVAL '1 second'), "
                    "last_flood_at = NOW(), flood_count_7d = COALESCE(flood_count_7d, 0) + 1 "
                    "WHERE id=$2",
                    min(wait, 3600),
                    acc["id"],
                )
            except Exception:
                log_exc_swallow(
                    log, "dm_engine: failed to set cooldown_until for acc=%d", acc["id"]
                )
            # Убрать аккаунт из цикла временно и подождать
            if wait <= 60:
                await asyncio.sleep(min(wait, 60))
            else:
                # Для долгих флуд-вейтов — убираем аккаунт из ротации, продолжаем другими
                acc_cycle_without = [a for a in acc_cycle if a["id"] != acc["id"]]
                if acc_cycle_without:
                    acc_cycle = acc_cycle_without
                    log.info(
                        "dm_engine: removed flooded acc %d from rotation, %d remaining",
                        acc["id"],
                        len(acc_cycle),
                    )
                else:
                    await asyncio.sleep(min(wait, 300))
            infra_memory.record_account_op(
                acc["id"], "dm_campaign", False, "flood_wait"
            )
            continue
        elif status in ("blocked", "peer_flood", "auth"):
            # Цель в любом случае не доставлена — фиксируем как failed.
            log.warning(
                "dm_engine: acc %d target %d status %s", acc["id"], user_id, status
            )
            failed += 1
            await pool.execute(
                "INSERT INTO dm_campaign_log(campaign_id, account_id, tg_user_id, status, error_msg) "
                "VALUES ($1,$2,$3,$4,$5) ON CONFLICT DO NOTHING",
                campaign_id,
                acc["id"],
                user_id,
                status,
                result.get("error", "")[:200],
            )
            await pool.execute(
                "UPDATE dm_campaigns SET fail_count=fail_count+1 WHERE id=$1",
                campaign_id,
            )
            infra_memory.record_account_op(
                acc["id"], "dm_campaign", False, result.get("error", "")
            )

            if status == "blocked":
                # PER-TARGET (юзер заблокировал/запретил запись) — аккаунт ЗДОРОВ.
                # НЕ убираем его из ротации: раньше один недружелюбный таргет
                # выкидывал здоровый аккаунт из всей кампании. Идём дальше тем же пулом.
                pass
            else:
                # auth (сессия мертва) или peer_flood (аккаунт флагнут за спам) —
                # account-level: убираем аккаунт из ротации кампании.
                if status == "peer_flood":
                    # Длинный cooldown, чтобы СЛЕДУЮЩАЯ операция не добила флагнутый
                    # аккаунт (per-campaign удаления мало — флаг живёт на аккаунте).
                    try:
                        await pool.execute(
                            "UPDATE tg_accounts SET cooldown_until = NOW() + ($1 * INTERVAL '1 second'), "
                            "last_flood_at = NOW(), flood_count_7d = COALESCE(flood_count_7d, 0) + 1 "
                            "WHERE id=$2",
                            _PEER_FLOOD_COOLDOWN,
                            acc["id"],
                        )
                    except Exception:
                        log_exc_swallow(
                            log, "dm_engine: peer_flood cooldown failed acc=%d", acc["id"]
                        )
                acc_cycle = [a for a in acc_cycle if a["id"] != acc["id"]]
                if not acc_cycle:
                    log.error(
                        "dm_engine: no more accounts for campaign %d, stopping", campaign_id
                    )
                    break  # Оставшиеся цели будут учтены после цикла
        elif status == "skip":
            # Пользователь заблокировал бота или деактивирован — не ошибка, пропускаем тихо
            log.debug(
                "dm_engine: user %d blocked/deactivated, skipping silently", user_id
            )
            await pool.execute(
                "INSERT INTO dm_campaign_log(campaign_id, account_id, tg_user_id, status, error_msg) "
                "VALUES ($1,$2,$3,'skip',$4) ON CONFLICT DO NOTHING",
                campaign_id,
                acc["id"],
                user_id,
                result.get("error", "")[:200],
            )
            # Не считаем как fail — пользователь просто недоступен.
            # Применяем задержку, чтобы не спамить API при большом числе неактивных.
            if op_id:
                try:
                    await pool.execute(
                        "UPDATE operation_queue SET done_items=$1 WHERE id=$2",
                        sent + failed,
                        op_id,
                    )
                except Exception as e:
                    log_exc_swallow(log, "run_campaign: update operation")
            await asyncio.sleep(random.uniform(_delay_min, _delay_max))
            continue
        else:
            # retry — логируем как ошибку
            failed += 1
            await pool.execute(
                "INSERT INTO dm_campaign_log(campaign_id, account_id, tg_user_id, status, error_msg) "
                "VALUES ($1,$2,$3,$4,$5) ON CONFLICT DO NOTHING",
                campaign_id,
                acc["id"],
                user_id,
                status,
                result.get("error", "")[:200],
            )
            await pool.execute(
                "UPDATE dm_campaigns SET fail_count=fail_count+1 WHERE id=$1",
                campaign_id,
            )
            infra_memory.record_account_op(
                acc["id"], "dm_campaign", False, result.get("error", "")
            )

        # Sync done_items into operation_queue for progress bar in ops list
        if op_id:
            try:
                await pool.execute(
                    "UPDATE operation_queue SET done_items=$1 WHERE id=$2",
                    sent + failed,
                    op_id,
                )
            except Exception as e:
                log_exc_swallow(log, "run_campaign: update operation")

        # Milestone progress notifications (25%, 50%, 75%)
        if total > 0:
            _done = sent + failed
            _pct = int(_done * 100 / total)
            for _milestone in (25, 50, 75):
                if _pct >= _milestone and _milestone not in _notified_milestones:
                    _notified_milestones.add(_milestone)
                    try:
                        from database import db as _db

                        await _db.notify_if_enabled(
                            pool,
                            bot,
                            owner_id,
                            "op_complete",
                            f"📨 <b>DM «{campaign['name']}»</b> — {_milestone}%\n"
                            f"✅ {sent} отправлено · ❌ {failed} ошибок · 📊 {total} всего",
                            dedup_key=f"dm-milestone:{campaign_id}:{_milestone}",
                        )
                    except Exception:
                        log_exc_swallow(
                            log,
                            f"dm_engine: progress notification failed campaign={campaign.get('id')} owner={owner_id}",
                        )

        # Humanized delay (per target-type range), растянутая по давлению флота.
        # DM-кампании были единственным массовым расходником ВНЕ губернатора:
        # инвайт, разовая рассылка и ещё два десятка операций уважают общее
        # давление, а кампания долбила своим темпом, даже когда флот уже ловил
        # флуды и баны от других операций. Telegram смотрит на аккаунты как на
        # группу — не замедлиться здесь значило добивать уже просевший флот.
        delay = random.uniform(_delay_min, _delay_max) * await _fleet_tempo_mult(pool, owner_id)
        await asyncio.sleep(delay)

    # Учесть необработанные цели (напр., при исчерпании всех аккаунтов)
    unprocessed = max(0, total - sent - failed)
    if unprocessed > 0:
        failed += unprocessed
        await pool.execute(
            "UPDATE dm_campaigns SET fail_count=fail_count+$1 WHERE id=$2",
            unprocessed,
            campaign_id,
        )
        log.warning(
            "dm_engine: campaign %d — %d targets unprocessed (accounts exhausted)",
            campaign_id,
            unprocessed,
        )

    await pool.execute(
        "UPDATE dm_campaigns SET status='done', finished_at=now() WHERE id=$1",
        campaign_id,
    )

    try:
        from database import db as _db

        await _db.notify_if_enabled(
            pool,
            bot,
            owner_id,
            "op_complete",
            f"📨 <b>DM-кампания «{campaign['name']}» завершена</b>\n\n"
            f"✅ Отправлено: <b>{sent}</b>\n"
            f"❌ Ошибок: <b>{failed}</b>\n"
            f"📊 Всего целей: <b>{total}</b>"
            + (f"\n🚫 Пропущено по реестру «не писать»: <b>{opt_out_removed}</b>"
               if opt_out_removed else ""),
            dedup_key=f"dm-done:{campaign_id}",
        )
    except Exception:
        log_exc_swallow(
            log, "Сбой уведомления о завершении DM-кампании", campaign_id=campaign_id
        )
