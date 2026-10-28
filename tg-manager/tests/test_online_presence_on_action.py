"""Аккаунт действует ТОЛЬКО будучи онлайн + ведёт себя как живой пользователь.

Два анти-детект-правила владельца:
  1. действие из оффлайна — заметный триггер: боевые подключения помечают
     аккаунт онлайн ПЕРЕД работой (connect_client, send_dm, send_media);
  2. аккаунт, который делает только одно действие (только ЛС / только инвайт),
     отделяется от живого по СОСТАВУ вызовов — поэтому между действиями
     вплетается «живое поведение» (invite_behavior.humanize: онлайн/диалоги/
     чтение). У инвайта было, добавлено и в DM-кампанию.

Параллельность на ОДНОМ аккаунте невозможна (мьютекс сессии: две сессии с двух
коннектов = AUTH_KEY_DUPLICATED/бан). «Живая жизнь» достигается разнообразием
поведения во времени, а не одновременными задачами.
"""
from __future__ import annotations

import asyncio
import inspect

from services import account_manager, dm_engine


def test_mark_online_exists_and_is_best_effort():
    assert inspect.iscoroutinefunction(account_manager.mark_online)

    class _Boom:
        def __call__(self, *a, **k):
            raise RuntimeError("telegram down")

    # Никогда не бросает (присутствие — улучшение, не обязательство); на сбое False.
    assert asyncio.run(account_manager.mark_online(_Boom(), timeout=0.5)) is False


def test_connect_client_marks_online_on_capturing_connect():
    src = inspect.getsource(account_manager.connect_client)
    # Онлайн только на захватывающих (боевых) подключениях, не на пассивных пробах.
    assert "if retry_auth_dup:" in src
    assert "await mark_online(client)" in src
    # обе точки возврата (основная и прямой фолбэк) помечают онлайн
    assert src.count("await mark_online(client)") >= 2


def test_send_primitives_mark_online_before_sending():
    dm = inspect.getsource(account_manager.send_dm)
    assert "await mark_online(client)" in dm
    media = inspect.getsource(account_manager.send_media_via_account)
    assert "await mark_online(client)" in media


def test_dm_campaign_interleaves_live_behavior():
    src = inspect.getsource(dm_engine.run_campaign)
    assert "invite_behavior" in src and "humanize(acc)" in src


def test_humanize_repertoire_is_read_only():
    # Контракт, на который опирается вплетение в DM: репертуар строго read-only
    # (онлайн/диалоги/чтение), без отправок/вступлений.
    from services import invite_behavior
    actions = {name for name, _ in invite_behavior._ACTIONS}
    assert actions == {"presence", "dialogs", "read"}
    assert set(invite_behavior._HANDLERS) == {"presence", "dialogs", "read"}
    assert inspect.iscoroutinefunction(invite_behavior.humanize)
