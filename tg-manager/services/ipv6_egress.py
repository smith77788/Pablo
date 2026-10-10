"""Свой IPv6 на аккаунт: выбор адреса, сохранение DC сессии, проверка хоста.

Чистая логика без telethon, чтобы её можно было проверять отдельно от клиента.
Пользуется ею `account_manager._make_client` и сохранение подсети в мини-аппе.
"""
from __future__ import annotations

import hashlib
import ipaddress
import socket

# Адреса продовых DC Telegram по семействам (help.getConfig). Сессия хранит
# dc_id + адрес; при смене семейства нужен адрес ТОГО ЖЕ DC, иначе telethon
# сбрасывает сессию на DC2 (см. preserve_session_dc).
DC_IPV4 = {
    1: "149.154.175.53",
    2: "149.154.167.51",
    3: "149.154.175.100",
    4: "149.154.167.91",
    5: "91.108.56.130",
}
DC_IPV6 = {
    1: "2001:b28:f23d:f001::a",
    2: "2001:67c:4e8:f002::a",
    3: "2001:b28:f23d:f003::a",
    4: "2001:67c:4e8:f004::a",
    5: "2001:b28:f23f:f005::a",
}
DC_PORT = 443


def account_ipv6(account_id: int, subnet_cidr: str) -> str | None:
    """Детерминированный уникальный IPv6 аккаунта из подсети (или None).

    Подсеть /64 и уже: адрес = network + account_id + 1 (прежняя раскладка,
    у уже работающих аккаунтов адрес не меняется).

    Подсеть шире /64 (/56, /48, …): каждому аккаунту СВОЯ /64. Антиспам-системы
    обычно считают всю /64 одним абонентом (это стандартный размер выдачи одному
    клиенту), поэтому раздавать адреса подряд из первой /64 значит показать
    Telegram весь флот как одного пользователя. Хост-часть внутри /64 берётся из
    хэша — адрес похож на обычный SLAAC, а не на ::1, ::2, ::3.
    """
    if not subnet_cidr or not account_id:
        return None
    try:
        net = ipaddress.ip_network(subnet_cidr, strict=False)
    except ValueError:
        return None
    if net.version != 6 or net.num_addresses <= 2:
        return None
    aid = int(account_id)
    if net.prefixlen >= 64:
        return str(net[(aid % (net.num_addresses - 2)) + 1])
    n64 = 1 << (64 - net.prefixlen)
    # Первую /64 пропускаем: в ней обычно живёт адрес самого хоста.
    idx = (aid % (n64 - 1)) + 1
    base = int(net.network_address) + (idx << 64)
    host = int.from_bytes(
        hashlib.blake2b(str(aid).encode(), digest_size=8).digest(), "big")
    host = host or 1
    return str(ipaddress.IPv6Address(base + host))


def dc_address_for(dc_id: int | None, use_ipv6: bool) -> str | None:
    """Адрес DC нужного семейства; None для неизвестного (тестовые DC и т.п.)."""
    if not dc_id:
        return None
    return (DC_IPV6 if use_ipv6 else DC_IPV4).get(int(dc_id))


def preserve_session_dc(session, use_ipv6: bool) -> bool:
    """Перевести адрес сессии на семейство транспорта, сохранив её DC.

    telethon в connect(): если семейство адреса в сессии не совпадает с
    use_ipv6, он делает session.set_dc(DC2, адрес DC2, 443), а auth_key
    оставляет прежний. Сессия, залогиненная по IPv4 на DC1/3/4/5, при первом
    подключении со своего IPv6 шла бы со своим ключом на DC2 — для Telegram это
    чужой ключ (AUTH_KEY_UNREGISTERED), и живой аккаунт выглядел бы мёртвым.
    То же в обратную сторону: IPv6-сессия при откате на прямой IPv4.
    Возвращает True, если адрес поменяли.
    """
    addr = getattr(session, "server_address", None)
    dc_id = getattr(session, "dc_id", None)
    if not addr or not dc_id:
        return False  # новая сессия: telethon сам возьмёт DC2 нужного семейства
    if (":" in addr) == bool(use_ipv6):
        return False
    target = dc_address_for(dc_id, use_ipv6)
    if not target:
        return False
    session.set_dc(dc_id, target, DC_PORT)
    return True


def probe_subnet(subnet_cidr: str, timeout: float = 5.0) -> dict:
    """Проверить, что хост реально может ходить в Telegram из этой подсети.

    Берём адрес, который получил бы аккаунт, bind на него и TCP-коннект к DC2
    по IPv6. Без routed-подсети и AnyIP bind падает (EADDRNOTAVAIL), и
    тогда каждый аккаунт молча откатывался бы на прямой host-IP — включение
    такой подсети только создаёт видимость изоляции.
    """
    addr = account_ipv6(1, subnet_cidr)
    if not addr:
        return {"ok": False, "stage": "subnet", "addr": None,
                "error": "Это не IPv6-подсеть"}
    s = None
    try:
        s = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        s.settimeout(timeout)
        try:
            s.bind((addr, 0))
        except OSError as e:
            return {"ok": False, "stage": "bind", "addr": addr, "error": str(e)[:160],
                    "hint": "Подсеть не маршрутизирована на этот сервер или не "
                            "включён AnyIP: ip -6 route add local <подсеть> dev lo; "
                            "sysctl -w net.ipv6.ip_nonlocal_bind=1"}
        try:
            s.connect((DC_IPV6[2], DC_PORT))
        except OSError as e:
            return {"ok": False, "stage": "connect", "addr": addr, "error": str(e)[:160],
                    "hint": "Адрес привязался, но Telegram по IPv6 недоступен: нет "
                            "исходящего IPv6-маршрута или провайдер не отдаёт подсеть "
                            "наружу"}
        return {"ok": True, "stage": "connect", "addr": addr}
    except OSError as e:
        return {"ok": False, "stage": "socket", "addr": addr, "error": str(e)[:160],
                "hint": "На сервере нет IPv6"}
    finally:
        if s is not None:
            try:
                s.close()
            except OSError:
                pass
