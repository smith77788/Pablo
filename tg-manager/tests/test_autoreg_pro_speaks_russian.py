"""«Авторег Pro» показывает статус аккаунта по-русски.

Живой обход мини-аппа (03.11.2026): в списке пакетов и в истории регистрации
бейдж печатал `acc_status` как есть — владелец видел «session_expired»,
«banned», «frozen», «spamblock». Перевод статусов в приложении уже есть один —
accRu (screens/accounts_list.js), им и пользуемся.
"""
from __future__ import annotations

from tests.miniapp_source import js_function


def test_status_badges_are_translated():
    for fn in ("renderAutoregProList", "renderAutoregProHistory"):
        body = js_function(fn)
        assert "accRu(status)" in body, f"{fn}: статус аккаунта уходит на экран без перевода"
        assert "${status}</span>" not in body, f"{fn}: сырой статус в бейдже"
