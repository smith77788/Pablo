"""Политика прокси-изоляции: чистое решение о транспорте соединения.

Два ортогональных входа:
  - policy: глобальный выбор владельца —
      'strict'       — все соединения ТОЛЬКО через прокси (нет прокси → блок),
      'allow_direct' — можно без прокси, но с риском блокировок.
  - low_risk: операция низкого риска (одиночное чтение, не массовое быстрое
      действие). Её НИКОГДА не блокируем из-за отсутствия прокси — пользователь
      не должен упираться в «нужен прокси» ради, например, чтения контактов.

Отдельно сохраняется старое безопасное поведение: если аккаунту НАЗНАЧЕН прокси,
но его URL битый/недоступен — не рвём изоляцию молча (блок для не-low_risk
операций в любой политике), чтобы прокси-аккаунт не ушёл в сеть напрямую по-тихому.

Чистый модуль без внешних зависимостей — тестируется в песочнице.
"""
from __future__ import annotations

VALID_POLICIES = ("strict", "allow_direct")
DEFAULT_POLICY = "allow_direct"


def normalize_policy(value) -> str:
    """Привести значение политики к допустимому; неизвестное → DEFAULT_POLICY."""
    v = str(value).strip().lower() if value is not None else ""
    return v if v in VALID_POLICIES else DEFAULT_POLICY


def proxy_decision(
    *,
    has_proxy_url: bool,
    proxy_parsed_ok: bool,
    policy: str,
    enforce: bool,
    low_risk: bool,
) -> str:
    """Решение о транспорте: 'use' | 'block' | 'fallback'.

    use      — использовать назначенный аккаунту прокси;
    block    — запретить соединение (политика/изоляция требуют прокси, которого нет);
    fallback — глобальный TG_PROXY либо прямое соединение (риск блокировок).
    """
    if has_proxy_url and proxy_parsed_ok:
        return "use"
    if low_risk:
        # низкорисковую операцию не блокируем никогда — даём работать без прокси
        return "fallback"
    if has_proxy_url and not proxy_parsed_ok:
        # аккаунту назначен прокси, но он битый/недоступен — не уводим в сеть
        # напрямую втихую (сохранение изоляции для прокси-аккаунта)
        return "block"
    # у аккаунта нет назначенного прокси
    strict = bool(enforce) or normalize_policy(policy) == "strict"
    return "block" if strict else "fallback"
