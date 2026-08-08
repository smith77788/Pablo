"""Массовые операции теряли поля, от которых зависит транспорт аккаунта.

ЧТО БЫЛО СЛОМАНО. Запрос аккаунтов был скопирован по `op_worker` ДЕВЯТЬ раз, и во
всех копиях не хватало трёх полей, которые читает `account_manager._make_client`:

  * `proxy_id` — «привязан ли аккаунт к прокси». Ключевой момент: если прокси
    назначен, но сейчас НЕАКТИВЕН, JOIN отдаёт `proxy_url=NULL`. Без `proxy_id`
    аккаунт выглядит свободным, политика разрешает подключить его НАПРЯМУЮ — а он
    привязан к IP того прокси. Telegram отвечает AUTH_KEY_DUPLICATED и УБИВАЕТ
    сессию. Аккаунт после этого не работает вообще, и снаружи это ровно
    «операция ничего не делает». Об этой ловушке прямо предупреждает комментарий
    в самом `account_manager`, но массовые исполнители поле не выбирали.
  * `cf_relay_url` — транспорт через Cloudflare-релей. Без него аккаунт на релее
    терял транспорт и уходил напрямую с адреса сервера.
  * `owner_id` — по нему `_make_client` находит ПОЛИТИКУ прокси владельца
    (`_OWNER_PROXY_POLICY`). Без него строгая политика, включённая пользователем,
    в массовых операциях молча не применялась.

Для слоя anti-detection это дороже обычного бага: потеря изоляции и убитые сессии.
Проверено прогоном по настоящей базе — все три поля теперь доезжают до Telethon.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKER = (ROOT / "services" / "op_worker.py").read_text(encoding="utf-8")


def test_single_source_of_truth_exists():
    assert "_ACC_COLS" in WORKER, "запрос аккаунтов обязан быть одним текстом"
    m = re.search(r"_ACC_COLS = \((.*?)\n\)", WORKER, re.DOTALL)
    assert m, "константа не найдена"


def test_all_transport_fields_are_selected():
    m = re.search(r"_ACC_COLS = \((.*?)\n\)", WORKER, re.DOTALL)
    cols = m.group(1)
    for field in ("a.proxy_id", "a.cf_relay_url", "a.owner_id", "proxy_url",
                  "a.session_str", "a.device_model"):
        assert field in cols, f"{field} не выбирается — транспорт аккаунта неполный"


def test_no_copies_left_behind():
    """Девять копий — это девять мест, где поля могут разойтись снова."""
    stale = re.findall(
        r'"SELECT a\.id, a\.session_str, a\.device_model[^"]*"\s*\n\s*"a\.app_version[^"]*proxy_url "',
        WORKER)
    assert not stale, f"осталась копия запроса вместо _ACC_COLS: {len(stale)} шт."
    assert WORKER.count("_ACC_COLS,") >= 9, (
        "все прежние копии должны использовать общую константу"
    )


def test_isolation_query_keeps_active_proxy_filter():
    """`is_active=TRUE` в JOIN — намеренно: неактивный прокси НЕ должен подставиться
    как рабочий. Именно поэтому и нужен отдельный proxy_id."""
    m = re.search(r"_ACC_COLS = \((.*?)\n\)", WORKER, re.DOTALL)
    assert "p.is_active=TRUE" in m.group(1)


def test_client_actually_reads_these_fields():
    """Предпосылка: если `_make_client` перестанет их читать, выборка станет
    лишней — тест должен это заметить, а не тихо устареть."""
    am = (ROOT / "services" / "account_manager.py").read_text(encoding="utf-8")
    for key in ('device.get("proxy_id")', 'device.get("owner_id")'):
        assert key in am, f"{key} больше не читается — перечитать необходимость поля"
    assert "cf_relay_url" in am


def test_invite_uses_the_shared_query():
    m = re.search(r"async def _exec_mass_invite\(.*?(?=\nasync def )", WORKER, re.DOTALL)
    assert "_ACC_COLS" in m.group(0), (
        "инвайт — самая баноопасная операция, он обязан получать полный транспорт"
    )
