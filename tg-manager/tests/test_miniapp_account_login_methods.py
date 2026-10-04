"""Мини-апп: добавление аккаунта всеми способами входа (как в боте).

Раньше в мини-аппе был ТОЛЬКО импорт строки сессии. Добавлены вход по номеру
(код + 2FA) и по QR. Хендлеры — тонкие обёртки над примитивами account_manager;
тест сторожит регистрацию роутов, использование vetted-персистенции
(add_tg_account + закрепление прокси) и наличие контракта примитивов.
"""
from __future__ import annotations

import inspect
import pathlib


API = (pathlib.Path(__file__).resolve().parents[1] / "services" / "mini_app_api.py").read_text("utf-8")


def test_all_login_routes_registered():
    for route in (
        "/api/miniapp/account/add/phone/start",
        "/api/miniapp/account/add/phone/code",
        "/api/miniapp/account/add/phone/2fa",
        "/api/miniapp/account/add/phone/resend",
        "/api/miniapp/account/add/qr/start",
        "/api/miniapp/account/add/qr/poll",
        "/api/miniapp/account/add/qr/2fa",
        "/api/miniapp/account/add/session_file",
        "/api/miniapp/account/add/tdata",
    ):
        assert route in API, f"роут не зарегистрирован: {route}"


def test_file_upload_bounded_and_guarded():
    # multipart читается с жёстким лимитом; tdata распаковка — через vetted-хелпер
    assert "_read_upload" in API and "read_chunk" in API
    assert "import_tdata_from_zip_bytes" in API
    assert "import_from_session_file" in API
    from services import account_manager as am
    assert inspect.iscoroutinefunction(am.import_tdata_from_zip_bytes)


def test_tdata_zip_helper_guards():
    """import_tdata_from_zip_bytes защищает от zip-bomb и path-traversal."""
    from services import account_manager as am
    src = inspect.getsource(am.import_tdata_from_zip_bytes)
    assert "_MAX_UNCOMPRESSED" in src and "_MAX_FILES" in src
    assert "path traversal" in src.lower() or ".." in src
    assert "BadZipFile" in src


def test_handlers_use_vetted_persistence_and_proxy_binding():
    # сохранение через add_tg_account (шифрование+дедуп) + закрепление прокси
    assert "add_tg_account" in API
    assert "UPDATE tg_accounts SET proxy_id=$1 WHERE id=$2 AND owner_id=$3" in API
    # прокси резолвится со скоупом по владельцу
    assert "FROM user_proxies WHERE id=$1 AND owner_id=$2" in API


def test_phone_flow_handles_need_2fa_and_code():
    # confirm_code → "need_2fa" ветка и получение сессии
    assert 'res == "need_2fa"' in API
    assert "get_client_info_and_session" in API


def test_qr_flow_handles_2fa_and_pending():
    # QR 2FA сигналится SessionPasswordNeededError, ожидание — pending
    assert "SessionPasswordNeededError" in API
    assert '"status": "pending"' in API
    assert '"status": "need_2fa"' in API


def test_account_manager_login_primitives_exist():
    """Контракт примитивов, на которые опираются эндпоинты."""
    from services import account_manager as am
    for fn in ("start_login", "confirm_code", "confirm_2fa",
               "get_client_info_and_session", "start_qr_login",
               "wait_qr_login", "confirm_qr_2fa", "resend_code"):
        assert hasattr(am, fn), f"account_manager.{fn} отсутствует"
        assert inspect.iscoroutinefunction(getattr(am, fn)), f"{fn} должна быть async"


def test_resend_code_returns_next_timeout():
    """resend_code отдаёт 3-й элемент — рекомендованную паузу до след. запроса.

    По нему автоповтор кода пейсится, а не бьётся в FloodWait с первого тика.
    Контракт общий: единственный потребитель в боте распаковывает 3-кортеж.
    """
    from services import account_manager as am
    src = inspect.getsource(am.resend_code)
    assert "next_timeout" in src, "resend_code должна возвращать рекомендованную паузу"
    assert "return result.phone_code_hash, hint, next_timeout" in src
    bot = (pathlib.Path(__file__).resolve().parents[1] / "bot" / "handlers" / "accounts.py").read_text("utf-8")
    assert "new_hash, hint, _ = await resend_login_code" in bot, \
        "бот должен распаковывать 3-кортеж resend_code"


def test_phone_resend_endpoint_surfaces_flood():
    """Эндпоинт повтора отдаёт FloodWait структурно (не сырым 500/ошибкой),
    чтобы фронт показал паузу и остановил автоповтор."""
    assert "account_add_phone_resend" in API
    assert "FloodWaitError" in API
    assert '"flood": True' in API and '"retry_after"' in API


def test_ui_has_resend_and_autorepeat():
    """UI повтора кода: кнопка одиночного повтора и автоповтор, который сам
    останавливается на флуде."""
    html = (pathlib.Path(__file__).resolve().parents[1] / "mini_app" / "index.html").read_text("utf-8")
    for fn in ("accPhoneResend", "accPhoneAutoToggle", "accPhoneAutoStop"):
        assert fn in html, f"нет JS-функции {fn}"
    assert "accPhoneResendBtn" in html and "accPhoneAutoBtn" in html
    assert "/api/miniapp/account/add/phone/resend" in html
    # автоповтор останавливается на флуде/ошибке
    assert "res.flood||res.error" in html


def test_ui_has_all_login_methods():
    """Мини-апп UI предлагает все способы, а не только строку сессии."""
    html = (pathlib.Path(__file__).resolve().parents[1] / "mini_app" / "index.html").read_text("utf-8")
    # вкладки способов входа
    assert "accSetLoginMethod" in html, "нет переключателя способов входа"
    for fn in ("accPhoneStart", "accPhoneCode", "accQrStart", "accQrPoll",
               "accFileUpload", "accTdataUpload"):
        assert fn in html, f"нет JS-функции {fn}"
