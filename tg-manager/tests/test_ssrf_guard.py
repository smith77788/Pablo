"""Регрессия: SSRF-гард для загрузки аватара бота по URL.

set_photo/delete_my_photo (аватар бота) раньше были мертвы; аватар ставится по
публичному URL — is_safe_public_url отсекает http, localhost и приватные IP,
чтобы сервер не дёргал внутренние адреса.
"""
from __future__ import annotations

from services.mini_app_api import is_safe_public_url


def test_valid_https_public():
    assert is_safe_public_url("https://example.com/avatar.jpg") is True
    assert is_safe_public_url("https://cdn.telegram.org/x.png") is True


def test_http_rejected():
    assert is_safe_public_url("http://example.com/a.jpg") is False


def test_localhost_rejected():
    assert is_safe_public_url("https://localhost/a.jpg") is False
    assert is_safe_public_url("https://127.0.0.1/a.jpg") is False
    assert is_safe_public_url("https://0.0.0.0/a.jpg") is False


def test_private_ranges_rejected():
    assert is_safe_public_url("https://10.0.0.5/a.jpg") is False
    assert is_safe_public_url("https://192.168.1.1/a.jpg") is False
    assert is_safe_public_url("https://172.16.0.1/a.jpg") is False
    assert is_safe_public_url("https://172.31.255.1/a.jpg") is False
    assert is_safe_public_url("https://169.254.169.254/latest/meta-data") is False


def test_public_172_not_private():
    # 172.15 и 172.32 — вне приватного диапазона 172.16-31
    assert is_safe_public_url("https://172.15.0.1/a.jpg") is True
    assert is_safe_public_url("https://172.32.0.1/a.jpg") is True


def test_internal_tlds_rejected():
    assert is_safe_public_url("https://service.internal/a.jpg") is False
    assert is_safe_public_url("https://box.local/a.jpg") is False


def test_garbage_rejected():
    assert is_safe_public_url("") is False
    assert is_safe_public_url(None) is False
    assert is_safe_public_url("not a url") is False
