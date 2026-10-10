"""Регресс (честность anti-detection): «Проверить» показал уник. IP: 1 — все CF
воркеры выходят с ОДНОГО egress-IP Cloudflare (даже в разных colo). Значит CF-релей
НЕ даёт уникальный IP на аккаунт; заявлять «изоляция 1:1 / свой edge-IP» нельзя.

Проверяем, что интерфейс и аудит изоляции говорят правду:
  - карточка/подсказка не обещают уникальный IP (пишут «общий IP»);
  - «Проверить» предупреждает при unique_ips<=1;
  - audit_proxy_isolation.isolation_ok=False, пока есть аккаунты на релее (общий IP).
"""
from __future__ import annotations
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def test_ui_copy_is_honest_about_shared_ip():
    ui = _read("mini_app/index.html")
    # старое ложное обещание убрано
    assert "свой Cloudflare Worker (свой edge-IP)" not in ui
    # честно про общий IP + указание на прокси для 1:1
    assert "общий" in ui and "НЕ" in ui
    # подсказка авто-count больше не заявляет «изоляция 1:1» как факт релея
    assert "без прокси (изоляция 1:1)" not in ui
    # «Проверить» предупреждает при 1 общем IP
    assert "unique_ips<=1" in ui and "НЕ уникальный IP на аккаунт" in ui


def test_isolation_audit_excludes_relay_from_ok():
    ps = _read("services/proxy_selector.py")
    seg = ps[ps.index("async def audit_proxy_isolation"):ps.index("async def _fetch_backup_proxies")]
    # isolation_ok учитывает on_relay (релей — не изоляция)
    assert "not shared and not naked and not on_relay" in seg
