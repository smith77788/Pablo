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
# Enterprise-дефолт: strict — массовая операция без прокси/релея/IPv6 НЕ уходит
# напрямую с IP хоста (kill-switch), а честно падает ProxyIsolationError. Оператор
# может вернуть прежнее лояльное поведение через env PROXY_POLICY=allow_direct.
# Низкорисковые одиночные чтения (low_risk) от этого НЕ страдают — они не блокируются.
DEFAULT_POLICY = "strict"


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
    if has_proxy_url and not proxy_parsed_ok:
        # Аккаунту НАЗНАЧЕН прокси, но он битый/недоступен. НЕЛЬЗЯ подключаться с
        # другого IP (прямого/иного) — сессия привязана к IP прокси, и её
        # использование с другого адреса убивает сессию (Telegram
        # AUTH_KEY_DUPLICATED: «used under two different IP addresses»). Поэтому
        # блокируем В ЛЮБОМ режиме, включая low_risk. Пусть оператор починит прокси.
        return "block"
    # У аккаунта нет НАЗНАЧЕННОГО прокси — его каноничный транспорт и есть прямое
    # соединение (сессия создавалась без прокси), сменой IP не рискуем.
    if low_risk:
        return "fallback"
    strict = bool(enforce) or normalize_policy(policy) == "strict"
    return "block" if strict else "fallback"
