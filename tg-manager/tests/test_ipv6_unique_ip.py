"""Реальный уникальный IP на аккаунт БЕЗ прокси и без нескольких CF-аккаунтов.

CF Workers дают ОБЩИЙ edge-IP (эмпирически «уник. IP: 1») — тупик. Настоящее
решение: bind исходящего сокета к СВОЕМУ IPv6 аккаунта из маршрутизируемой
подсети (local_addr + use_ipv6). Один аккаунт → фикс. адрес; разные → разные.

account_manager импортирует telethon (нет в песочнице) → проверяем исходником +
воспроизводим чистую логику маппинга account_id→IPv6.
"""
from __future__ import annotations
import ipaddress
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _map(account_id, subnet_cidr):
    if not subnet_cidr or not account_id:
        return None
    net = ipaddress.ip_network(subnet_cidr, strict=False)
    if net.version != 6 or net.num_addresses <= 2:
        return None
    offset = (int(account_id) % (net.num_addresses - 2)) + 1
    return str(net[offset])


def test_ipv6_mapping_unique_and_deterministic():
    sub = "2a01:4f8:1c1c:abcd::/64"
    # детерминизм: тот же аккаунт → тот же адрес
    assert _map(18, sub) == _map(18, sub)
    # уникальность на 1000 аккаунтов
    addrs = {_map(i, sub) for i in range(1, 1001)}
    assert len(addrs) == 1000
    # валидный IPv6, не network-нулевой
    a = ipaddress.ip_address(_map(1, sub))
    assert a.version == 6 and str(a) != "2a01:4f8:1c1c:abcd::"
    # не-IPv6 / крошечная подсеть → None
    assert _map(1, "10.0.0.0/24") is None
    assert _map(1, "2a01::/127") is None
    assert _map(1, "") is None


def test_make_client_wires_ipv6_priority_over_relay():
    am = _read("services/account_manager.py")
    assert "def _account_ipv6" in am
    assert '_IPV6_SUBNET = _os.getenv("IPV6_SUBNET"' in am
    seg = am[am.index("has_bound_proxy = bool(proxy)"):am.index("return TelegramClient")]
    # IPv6 выбирается ДО ветки CF-релея (приоритет — реальный уникальный IP)
    assert seg.index("_IPV6_SUBNET and _acc_id") < seg.index("relay_url:")
    assert "local_addr = _v6" in seg and "use_ipv6 = True" in seg
    # gated: только без bound-прокси и при заданной подсети
    assert "not has_bound_proxy and _IPV6_SUBNET and _acc_id" in seg
    # zero-risk: kwargs добавляются только когда IPv6 активен
    tail = am[am.index("_extra_kwargs = {}"):am.index("StringSession(session_string)")]
    assert "if local_addr is not None:" in tail
