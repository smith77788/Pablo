"""Общая стратегия сети: настройки владельца, роли каналов и контроль призывов."""
from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

ROLES = {
    "discovery": "Знакомить новую аудиторию: понятная польза и материалы для пересылки",
    "expert": "Укреплять доверие: подробные разборы и проверенные объяснения",
    "community": "Развивать сообщество: вопросы, обратная связь и обсуждения",
    "conversion": "Помогать принять решение: предложение и ответы на возражения",
    "independent": "Работать отдельно от общей стратегии",
}


class StrategyError(ValueError):
    """Ошибка настроек, которую можно показать владельцу."""


def _object(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            value = {}
    return value if isinstance(value, dict) else {}


def validate(value):
    from services.channel_admin import validate_business

    if not isinstance(value, dict):
        raise StrategyError("Ожидались настройки сети")
    out = {"enabled": value.get("enabled", False)}
    if not isinstance(out["enabled"], bool):
        raise StrategyError("Режим сети должен быть включён или выключен")
    for key, cap in (("name", 100), ("project_info", 2000), ("destination", 500),
                     ("action", 300), ("audience", 600)):
        raw = value.get(key, "")
        if not isinstance(raw, str) or len(raw.strip()) > cap:
            raise StrategyError(f"Текст настройки слишком длинный или имеет неверный тип: до {cap} символов")
        out[key] = raw.strip()
    destination = out["destination"]
    if destination:
        try:
            parsed = urlsplit(destination)
        except ValueError as exc:
            raise StrategyError("Целевой ресурс: некорректная ссылка") from exc
        username = re.fullmatch(r"@[A-Za-z][A-Za-z0-9_]{3,31}", destination)
        if not username and not (
            parsed.scheme == "https" and parsed.hostname and not parsed.username
            and not parsed.password and not re.search(r"[\s<>\"']", destination)
        ):
            raise StrategyError("Целевой ресурс: укажите @профиль или полную ссылку https://")
    if out["enabled"] and not (out["destination"] and out["action"]):
        raise StrategyError("Для общей стратегии укажите целевой ресурс и действие читателя")
    share = value.get("cta_share", 20)
    if isinstance(share, bool) or not isinstance(share, int) or not 0 <= share <= 60:
        raise StrategyError("Доля постов с переходом: целое число от 0 до 60 %")
    out["cta_share"] = share
    out["business"], errors = validate_business(value.get("business", {}))
    if errors:
        raise StrategyError("; ".join(errors))
    return out


async def get_strategy(pool, owner_id):
    row = await pool.fetchrow(
        "SELECT settings, revision FROM va_network_strategy WHERE owner_id=$1", int(owner_id))
    if not row:
        return {"settings": validate({}), "revision": 0}
    return {"settings": validate(_object(row.get("settings"))),
            "revision": int(row.get("revision") or 0)}


async def save_strategy(pool, owner_id, payload):
    if not isinstance(payload, dict):
        raise StrategyError("Ожидались настройки сети")
    revision = payload.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise StrategyError("Обновите экран перед сохранением стратегии")
    settings = validate(payload.get("settings"))
    encoded = json.dumps(settings, ensure_ascii=False)
    # Compare-and-swap prevents a second browser overwriting a newer strategy.
    if revision == 0:
        row = await pool.fetchrow(
            "INSERT INTO va_network_strategy(owner_id, settings) VALUES($1,$2::jsonb) "
            "ON CONFLICT (owner_id) DO NOTHING RETURNING revision", int(owner_id), encoded)
    else:
        row = await pool.fetchrow(
            "UPDATE va_network_strategy SET settings=$2::jsonb, revision=revision+1, updated_at=now() "
            "WHERE owner_id=$1 AND revision=$3 RETURNING revision", int(owner_id), encoded, revision)
    if not row:
        raise StrategyError("Стратегию уже изменили в другом окне. Обновите экран и повторите")
    return {"settings": settings, "revision": int(row["revision"])}


def apply_strategy(profile, strategy):
    """Наследование не меняет сохранённые настройки отдельных каналов."""
    result = dict(profile)
    own = _object(profile.get("business"))
    role = own.get("network_role", "discovery")
    if not strategy.get("enabled") or role == "independent":
        return result
    result["business"] = {**strategy.get("business", {}), **own}
    # Обязательные запреты сети дополняют местные, а не стираются ими.
    for key in ("banned_topics", "competitors"):
        result["business"][key] = "\n".join(filter(None, (
            strategy.get("business", {}).get(key), own.get(key))))
    result["project_info"] = "\n".join(filter(None, (
        strategy.get("project_info"), profile.get("project_info"))))
    result["lead_contact"] = strategy["destination"]
    result["network_strategy"] = strategy
    result["network_role"] = role
    return result


async def enrich_profile(pool, owner_id, profile):
    strategy = (await get_strategy(pool, owner_id))["settings"]
    result = apply_strategy(profile, strategy)
    if result.get("network_strategy"):
        rows = await pool.fetch(
            "SELECT body FROM va_channel_posts WHERE owner_id=$1 "
            "AND published_at > now() - interval '7 days' "
            "ORDER BY published_at DESC, id DESC LIMIT 40", int(owner_id))
        result["network_recent"] = [r["body"] for r in rows if r.get("body")]
    return result


def allow_cta(profile, recent_texts):
    strategy = profile.get("network_strategy")
    if not strategy:
        return None
    share = strategy.get("cta_share", 20)
    # A ten-post window: includes the candidate and the preceding nine posts.
    cap = share // 10
    target = strategy["destination"].casefold()
    used = sum(target in text.casefold() for text in list(recent_texts)[:9])
    return cap > used


def prompt_lines(profile):
    strategy = profile.get("network_strategy")
    if not strategy:
        return []
    return [
        "ОБЩАЯ СТРАТЕГИЯ СЕТИ",
        f"Цель читателя: {strategy['action']}",
        f"Единый целевой ресурс: {strategy['destination']}",
        f"Общая аудитория: {strategy.get('audience') or 'учитывай аудиторию этого канала'}",
        f"Роль этого канала: {ROLES.get(profile.get('network_role'), ROLES['discovery'])}",
        "Каналы дополняют друг друга. Сохраняй свой голос и угол темы; не копируй соседние посты.",
        "Не изображай независимую рекомендацию или опыт клиента. Не выдумывай результаты и отзывы.",
    ]


def review_reasons(profile, text, recent_texts):
    strategy = profile.get("network_strategy")
    if not strategy:
        return []
    from services.channel_brain import repetition_check

    reasons = []
    if allow_cta(profile, recent_texts) is False and strategy["destination"].casefold() in text.casefold():
        reasons.append("достигнут лимит постов с переходом: убери целевой контакт из этого поста")
    # Общую ссылку исключаем из антиповтора: совпадение CTA само по себе не дубль.
    destination = strategy["destination"]
    candidate = text.replace(destination, "")
    history = [x.replace(destination, "") for x in profile.get("network_recent", [])]
    duplicate = repetition_check(candidate, history, dup_threshold=0.8)
    if duplicate["is_duplicate"]:
        reasons.append("текст повторяет недавний пост сети: нужен другой материал и формулировки")
    return reasons
