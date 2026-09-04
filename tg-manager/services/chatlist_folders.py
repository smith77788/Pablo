"""Инвайтинг через общие папки Telegram (Shared Folders / chatlists).

Идея метода. Вместо того чтобы затаскивать людей в группу по одному, владелец
собирает свои каналы и чат в общую папку и раздаёт ОДНУ ссылку
(`chatlists.ExportChatlistInvite`). По ней человек добавляет сразу всю связку —
наши каналы появляются у него блоком в списке папок. Это сильнее обычного
инвайта: не «вступи в группу», а «забери готовую подборку», и вступление —
осознанное действие пользователя, а не навязанное.

Что здесь ЕСТЬ и что НЕТ (важно — иначе получилась бы кнопка-обманка):
  • есть: сборка папки из СВОИХ ресурсов владельца и экспорт chatlist-ссылки
    через сессию его аккаунта; раздачу ссылки делают наши обычные каналы
    (рассылки/боты/посты), а `JoinChatlistInvite` вызывает сам пользователь;
  • нет: «автоприкрепления» папки пользователю. `CheckChatlistInvite` только
    показывает превью ссылки; добавить папку может ТОЛЬКО сам пользователь из
    своей авторизованной сессии. Login Widget отдаёт лишь идентичность, не
    сессию. Поэтому никакой «жёсткой рассылки по скрытым эндпоинтам» тут не
    делается и делаться не может.

Здесь — чистая доменная логика (отбор пригодных чатов, лимиты, заголовок,
форма записи). Сетевые вызовы Telegram изолированы в исполнителе операции и
проверяются на живом флоте.
"""
from __future__ import annotations

# Заголовок папки в Telegram — не длиннее 12 символов (ограничение DialogFilter).
# Режем заранее, чтобы Telethon-вызов не упал уже на боевой сессии.
MAX_TITLE_LEN = 12

# Сколько чатов кладём в одну общую папку. Telegram допускает до ~200, но
# ссылка-приглашение включает КОНКРЕТНЫЕ peer'ы, и раздувать связку смысла нет:
# человек не станет добавлять папку на полсотни каналов. Держим компактно.
MAX_CHATS_PER_FOLDER = 50
MIN_CHATS_PER_FOLDER = 1

# Типы ресурсов, которые имеет смысл класть в общую папку. Личные диалоги и
# ботов в chatlist не включаем — приглашение к папке добавляет публичные/
# групповые сущности, к которым у нас есть админ-доступ.
_ELIGIBLE_TYPES = ("channel", "supergroup", "group", "chat")


def clean_title(raw: str, fallback: str = "Подборка") -> str:
    """Заголовок папки: обрезка до лимита Telegram, без пустышек.

    Пустой/пробельный ввод заменяем осмысленным запасным словом, а не пустой
    строкой — иначе папка у пользователя будет безымянной.
    """
    t = (raw or "").strip()
    if not t:
        t = fallback
    # Telegram считает длину в символах (не байтах); эмодзи из одной кодовой
    # точки занимают одну позицию — этого достаточно для нашей проверки.
    if len(t) > MAX_TITLE_LEN:
        t = t[:MAX_TITLE_LEN].rstrip()
    return t or fallback


def _asset_type(asset: dict) -> str:
    for key in ("type", "chat_type", "kind"):
        v = asset.get(key)
        if v:
            return str(v).lower()
    # managed_channels не хранит тип — но это заведомо каналы/супергруппы,
    # пригодные для папки. Отсутствие типа трактуем как пригодный, а не отсев.
    return "channel"


def select_eligible(assets: list[dict]) -> tuple[list[dict], list[dict]]:
    """Разделить ресурсы на пригодные для папки и отсеянные (с причиной).

    Пригодность: это канал/группа (не бот, не личный диалог), есть числовой
    chat_id, дублей по chat_id нет. Возвращает (eligible, rejected), где у
    отсеянного проставлен ключ ``reason``.
    """
    eligible: list[dict] = []
    rejected: list[dict] = []
    seen: set[int] = set()
    for a in assets or []:
        cid = a.get("chat_id", a.get("channel_id"))
        try:
            cid = int(cid)
        except (TypeError, ValueError):
            rejected.append({**a, "reason": "нет числового chat_id"})
            continue
        if _asset_type(a) not in _ELIGIBLE_TYPES:
            rejected.append({**a, "reason": "не канал и не группа"})
            continue
        if cid in seen:
            rejected.append({**a, "reason": "дубль"})
            continue
        seen.add(cid)
        eligible.append({**a, "chat_id": cid})
    return eligible, rejected


def validate_selection(assets: list[dict]) -> tuple[bool, str, list[dict]]:
    """Проверить набор для папки. (ok, причина_отказа, пригодные).

    Отказываем ДО обращения к Telegram: пустой набор, слишком большой набор,
    ни одного пригодного чата — всё это ловится без сессии.
    """
    eligible, _rej = select_eligible(assets)
    n = len(eligible)
    if n < MIN_CHATS_PER_FOLDER:
        return False, "В папку не попал ни один канал или чат — добавьте хотя бы один свой ресурс.", []
    if n > MAX_CHATS_PER_FOLDER:
        return (False,
                f"Слишком много чатов для одной папки ({n}). Максимум "
                f"{MAX_CHATS_PER_FOLDER} — разбейте на несколько подборок.",
                eligible)
    return True, "", eligible


def build_folder_record(owner_id: int, title: str, eligible: list[dict],
                        instance_id: int | None = None) -> dict:
    """Форма записи папки для БД (до экспорта ссылки).

    instance_id связывает папку со связкой (network_builder), если папка
    собрана из её узлов — тогда «развернул связку → раздал папку» становится
    одной цепочкой, а не двумя несвязанными экранами.
    """
    return {
        "owner_id": int(owner_id),
        "title": clean_title(title),
        "chat_ids": [int(a["chat_id"]) for a in eligible],
        "chat_count": len(eligible),
        "instance_id": int(instance_id) if instance_id else None,
    }


def summarize(record: dict, invite_link: str | None = None,
              join_count: int = 0) -> str:
    """Короткая человеческая сводка по папке — для списка и уведомлений."""
    n = int(record.get("chat_count") or len(record.get("chat_ids") or []))
    title = record.get("title") or "Подборка"
    head = f"📁 «{title}» — {n} чат(ов) в подборке"
    if not invite_link:
        return head + " · ссылка ещё не создана"
    if join_count:
        return head + f" · добавили {join_count}"
    return head + " · ссылка готова, добавлений пока нет"
