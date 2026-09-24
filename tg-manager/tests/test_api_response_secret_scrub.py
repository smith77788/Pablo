"""Секрет не уходит в ответ API, даже если обработчик о нём не подумал.

Ответов у мини-аппа больше пятисот, и каждый собирается своим кодом. Один
`SELECT *` по таблице с сессиями — и клиенту уезжает полный доступ к аккаунту
(так уже было с токеном бота в карточке бота). Барьер стоит в единственной
двери ответа `_json_resp`, поэтому не зависит от внимательности автора
конкретного обработчика.

Тест сторожит и обратную сторону: барьер НЕ должен ломать то, что владелец
получает законно — токен входа, выгрузку своей сессии, свой прокси и QR-код,
который приезжает как base64-картинка.
"""
from __future__ import annotations

import json

from services.secret_masking import scrub_payload


# ── Что закрывается ──────────────────────────────────────────────────────────
def test_session_string_is_masked_by_key():
    payload = {"accounts": [{"id": 7, "phone": "+79990001122",
                             "session_str": "1Ab" + "x" * 120}]}
    out = scrub_payload(payload)
    assert out["accounts"][0]["session_str"] == "***"
    # Остальные поля не тронуты.
    assert out["accounts"][0]["phone"] == "+79990001122"
    assert out["accounts"][0]["id"] == 7


def test_bot_token_is_masked_by_value_wherever_it_lies():
    """Токен ловится по форме, а не по имени поля: имена бывают любые."""
    token = "1234567890:AAHabcdefghijklmnopqrstuvwxyz012345"
    # Имя поля роли не играет: по форме остаётся id (по нему бота ищут в
    # логах), а секретная часть пропадает.
    for key in ("token", "какое_то_поле", "msg"):
        out = scrub_payload({key: token})
        assert out[key] == "1234567890:***", key
    # А под известным именем секрета значение вычищается целиком.
    assert scrub_payload({"bot_token": token})["bot_token"] == "***"


def test_secret_key_variants_are_covered():
    for key in ("session_string", "string_session", "api_hash", "app_hash",
                "password", "twofa_password", "cloud_password", "admin_secret"):
        out = scrub_payload({key: "значение-секрета-достаточной-длины"})
        assert out[key] == "***", key


def test_nested_structures_are_walked():
    payload = {"a": {"b": [{"c": {"session_str": "1" + "z" * 100}}]}}
    assert scrub_payload(payload)["a"]["b"][0]["c"]["session_str"] == "***"


def test_empty_secret_stays_empty_not_masked():
    """Пустое значение — это «нет секрета», а не секрет: «***» ввело бы в
    заблуждение (выглядело бы как настроенный пароль 2FA)."""
    out = scrub_payload({"session_str": None, "password": ""})
    assert out["session_str"] is None
    assert out["password"] == ""


# ── Что НЕ должно пострадать ─────────────────────────────────────────────────
def test_login_token_of_the_mini_app_survives():
    """Токен входа мини-аппа — `uid:ts:подпись`. Если его замаскировать, вход
    в приложение перестанет работать вообще."""
    tok = "5771234567:1758670000:ab12cd34ef56ab12cd34ef56"
    assert scrub_payload({"ok": True, "token": tok})["token"] == tok


def test_owner_can_still_export_own_session():
    """Кнопка «экспорт сессии» отдаёт владельцу его же сессию под ключом
    `session` — целиком, иначе экспорт бессмыслен."""
    sess = "1Ab" + "q" * 120
    assert scrub_payload({"ok": True, "session": sess})["session"] == sess


def test_owner_proxy_url_is_not_touched():
    """Приложение отправляет этот URL назад, когда проверяет сессию через
    прокси: замена пароля на «***» сломала бы проверку."""
    url = "socks5://user:pa55word@1.2.3.4:1080"
    assert scrub_payload({"proxies": [{"proxy_url": url}]})["proxies"][0]["proxy_url"] == url


def test_base64_image_survives_untouched():
    """QR входа в аккаунт приезжает как data:image/png;base64,... Внутри base64
    есть `+` и `/`, то есть границы слова — шаблон строки сессии совпал бы с
    куском картинки, и QR приехал бы битым."""
    blob = "data:image/png;base64,iVBORw0KGgo+1" + "AbCd+/=" * 40
    out = scrub_payload({"qr": blob})
    assert out["qr"] == blob


def test_long_plain_text_is_not_truncated():
    """Тексты рассылок бывают длинными; чистка не должна их обрезать."""
    text = "Привет! " * 500
    assert scrub_payload({"message_text": text})["message_text"] == text


def test_payload_without_secrets_is_returned_as_is():
    """Без находок объект не копируется — большие ответы не платят за барьер."""
    payload = {"items": [{"id": i, "name": f"канал {i}"} for i in range(50)]}
    assert scrub_payload(payload) is payload


def test_deep_recursion_is_bounded():
    """Слишком глубокая структура не должна ронять ответ рекурсией без дна."""
    node: dict = {"session_str": "1" + "w" * 100}
    for _ in range(40):
        node = {"next": node}
    scrub_payload(node)  # не должно упасть


# ── Барьер стоит именно в двери ответа ───────────────────────────────────────
def test_json_resp_applies_the_scrub():
    from services import mini_app_api as M

    resp = M._json_resp({"acc": {"session_str": "1" + "e" * 100},
                         "phone": "+79990001122"})
    body = json.loads(resp.text)
    assert body["acc"]["session_str"] == "***"
    assert body["phone"] == "+79990001122"


def test_json_resp_source_calls_the_scrub():
    """Источник правды — код двери: барьер не должен исчезнуть при правке."""
    import ast

    from services import mini_app_api as M

    src = open(M.__file__, encoding="utf-8").read()
    lines = src.split("\n")
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.FunctionDef) and node.name == "_json_resp":
            body = "\n".join(lines[node.lineno - 1:node.end_lineno])
            assert "scrub_payload(" in body, "чистка ответа пропала из _json_resp"
            return
    raise AssertionError("_json_resp не найден")
