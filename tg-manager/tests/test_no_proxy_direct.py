"""Без прокси → прямой выход с реального host-IP (free-pool УДАЛЁН).

Политика транспорта: прокси задаёт пользователь; если прокси/релей/IPv6 не заданы —
стабильный ПРЯМОЙ host-IP (один и тот же на логине и в операциях). Бесплатный
публичный SOCKS-пул удалён полностью — он был нестабилен (скачок IP между логином
и операцией → AUTH_KEY_DUPLICATED) и блокировал работу флота.

telethon застаблен в conftest, поэтому _make_client возвращает лёгкую заглушку с
проставленным _infragram_transport.
"""
from __future__ import annotations

from services import account_manager as am


def _no_transport_env(monkeypatch):
    # Ни IPv6, ни CF-relay не сконфигурированы (как у оператора без прокси).
    monkeypatch.setattr(am, "_IPV6_SUBNET", "")
    monkeypatch.setattr(am, "CF_RELAY_URL", "")


def test_no_proxy_goes_direct_by_default(monkeypatch):
    _no_transport_env(monkeypatch)
    client = am._make_client("", {"id": 1, "phone": "+70000000001"})
    assert getattr(client, "_infragram_transport", None) == "direct"


def test_no_proxy_login_also_direct(monkeypatch):
    # На логине account_id ещё нет — транспорт тот же (direct), что и в операции,
    # иначе логин и операция шли бы с разных IP.
    _no_transport_env(monkeypatch)
    client = am._make_client("", {"phone": "+70000000002"})  # без id (как логин)
    assert getattr(client, "_infragram_transport", None) == "direct"


def test_free_pool_removed_no_pool_transport(monkeypatch):
    # free-pool удалён: даже без прокси/релея/IPv6 транспорт всегда 'direct',
    # публичный пул НИКОГДА не используется. Заглушка-функция — no-op.
    _no_transport_env(monkeypatch)
    am.set_pool_proxy_cache(["socks5://1.2.3.4:1080"])   # no-op, ни на что не влияет
    client = am._make_client("", {"id": 3, "phone": "+70000000003"})
    assert getattr(client, "_infragram_transport", None) == "direct"
    assert not hasattr(am, "_get_pool_proxy_url")        # функция удалена


def test_global_relay_does_not_hijack_direct_under_allow_direct(monkeypatch):
    # Требование оператора: без прокси → РЕАЛЬНЫЙ host-IP. Глобальный CF_RELAY_URL
    # (общий edge-IP пула) НЕ подменяет прямой выход под allow_direct (дефолт) —
    # иначе логин/операция разъезжаются по IP → AUTH_KEY_DUPLICATED.
    monkeypatch.setattr(am, "_IPV6_SUBNET", "")
    monkeypatch.setattr(am, "CF_RELAY_URL", "wss://relay.example/ws")
    client = am._make_client("", {"id": 10, "phone": "+70000000010",
                                  "proxy_policy": "allow_direct"})
    assert getattr(client, "_infragram_transport", None) == "direct"


def test_global_relay_used_only_under_strict(monkeypatch):
    # Под strict host-IP запрещён осознанно → глобальный релей допустим (не 'direct').
    monkeypatch.setattr(am, "_IPV6_SUBNET", "")
    monkeypatch.setattr(am, "CF_RELAY_URL", "wss://relay.example/ws")
    client = am._make_client("", {"id": 11, "phone": "+70000000011",
                                  "proxy_policy": "strict"})
    assert getattr(client, "_infragram_transport", None) == "relay"


def test_per_account_relay_honored_under_allow_direct(monkeypatch):
    # Явно назначенный аккаунту relay (выбор пользователя/пула) honored всегда.
    monkeypatch.setattr(am, "_IPV6_SUBNET", "")
    monkeypatch.setattr(am, "CF_RELAY_URL", "")
    client = am._make_client("", {"id": 12, "phone": "+70000000012",
                                  "proxy_policy": "allow_direct",
                                  "cf_relay_url": "wss://acc-relay.example/ws"})
    assert getattr(client, "_infragram_transport", None) == "relay"


def test_bound_proxy_untouched(monkeypatch):
    # Аккаунт с назначенным прокси всегда идёт через него.
    _no_transport_env(monkeypatch)
    monkeypatch.setattr(am, "_parse_proxy",
                        lambda url: (2, "9.9.9.9", 1080, True, None, None) if url else None)
    client = am._make_client("", {"id": 4, "proxy_url": "socks5://9.9.9.9:1080"})
    assert getattr(client, "_infragram_transport", None) == "bound"


# ── Экран бота не должен обещать удалённый пул ────────────────────────────────
# Пул удалён из кода, а экран «🆓 Бесплатный пул» продолжал писать владельцу
# «прокси автоматически применяются к аккаунтам без личного прокси, пул
# обновляется каждые 6 часов». Владелец читал это как «мои аккаунты прикрыты» и
# ничего не делал, а аккаунты выходили с РЕАЛЬНОГО IP хоста. Ложная уверенность
# в изоляции опаснее честного «прокси нет».

def _proxy_screen_src() -> str:
    import pathlib
    return (pathlib.Path(__file__).resolve().parent.parent
            / "bot" / "handlers" / "proxy_manager.py").read_text(encoding="utf-8")


def test_proxy_screen_does_not_use_removed_scraper():
    """`services.proxy_scraper` удалён вместе с пулом — ссылка на него означала
    бы либо мёртвый экран, либо возврат нестабильного пула."""
    assert "proxy_scraper" not in _proxy_screen_src()


def _user_visible_strings(src: str) -> list[str]:
    """Строки, которые увидит владелец. Докстринги исключены намеренно: в них
    старое обещание цитируется как объяснение, зачем экран переписан."""
    import ast
    tree = ast.parse(src)
    docs = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.ClassDef,
                          ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(n, "body", None)
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                docs.add(id(body[0].value))
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and id(n) not in docs]


def test_proxy_screen_does_not_promise_automatic_pool():
    visible = " ".join(_user_visible_strings(_proxy_screen_src()))
    for lie in ("автоматически применяются", "каждые 6 часов"):
        assert lie not in visible, (
            f"экран снова обещает владельцу то, чего в коде нет: {lie!r}")


def test_the_probe_reads_the_visible_text_at_all():
    """Если выборка строк однажды опустеет, тест выше станет зелёным всегда."""
    visible = _user_visible_strings(_proxy_screen_src())
    assert any("Бесплатного пула больше нет" in s for s in visible), (
        "пробник не видит текст экрана — проверка выключилась молча")
