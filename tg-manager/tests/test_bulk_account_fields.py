"""Массовые операции теряли поля, от которых зависит транспорт аккаунта.

ЧТО БЫЛО СЛОМАНО. Запрос аккаунтов был скопирован по `op_worker` девять раз, и во
всех копиях не хватало полей, которые читает `account_manager._make_client`
(`proxy_id`, `cf_relay_url`, `owner_id`). Потом копии свели к одной константе
`_ACC_COLS`, а в шаге №1 аудита («одна дверь») — к единому флуд-осознанному
выбору `resource_selector.select_all_active`: он и отдаёт полный транспорт, и
фильтрует cooldown/мёртвые статусы. Здесь проверяется, что единая точка цела и
несёт все поля, от которых зависит анти-детект.

Почему поля критичны:
  * `proxy_id` — «привязан ли аккаунт к прокси». Если прокси назначен, но
    неактивен, JOIN отдаёт `proxy_url=NULL`; без `proxy_id` аккаунт выглядит
    свободным и уходит НАПРЯМУЮ — Telegram отвечает AUTH_KEY_DUPLICATED и убивает
    сессию.
  * `cf_relay_url` — транспорт через Cloudflare-релей; без него аккаунт на релее
    уходит напрямую с адреса сервера.
  * `owner_id` — по нему `_make_client` находит политику прокси владельца.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKER = (ROOT / "services" / "op_worker.py").read_text(encoding="utf-8")
SELECTOR = (ROOT / "services" / "resource_selector.py").read_text(encoding="utf-8")


def _select_all_active_body() -> str:
    m = re.search(r"async def select_all_active\(.*?(?=\nasync def )", SELECTOR, re.DOTALL)
    assert m, "select_all_active не найдена — единая точка выбора аккаунтов пропала"
    return m.group(0)


def test_single_source_of_truth_exists():
    """Единая точка выбора аккаунтов для операций — select_all_active."""
    assert "async def select_all_active" in SELECTOR


def test_all_transport_fields_are_selected():
    body = _select_all_active_body()
    for field in ("a.proxy_id", "a.cf_relay_url", "a.owner_id", "p.proxy_url",
                  "a.session_str", "a.device_model"):
        assert field in body, f"{field} не выбирается — транспорт аккаунта неполный"


def test_no_copies_left_behind():
    """Массовый выбор аккаунтов в op_worker идёт через единую дверь.

    Тонкие детали покрывает ратчет tests/test_account_selection_single_door
    (он умеет отличать выбор-из-многих от легитимной загрузки одного по id).
    Здесь — грубая проверка, что дверь вообще используется в op_worker.
    """
    assert "resource_selector.select_all_active" in WORKER, (
        "op_worker обязан выбирать аккаунты через единую дверь")
    # bulk-исполнители используют её многократно (9 бывших копий _ACC_COLS).
    assert WORKER.count("resource_selector.select_all_active") >= 9


def test_isolation_query_keeps_active_proxy_filter():
    """`is_active = TRUE` в JOIN — намеренно: неактивный прокси НЕ должен
    подставиться как рабочий. Именно поэтому и нужен отдельный proxy_id."""
    assert "p.is_active = TRUE" in _select_all_active_body()


def test_selector_filters_cooldown_and_dead_status():
    """Единая дверь обязана быть строго безопаснее сырого выбора: не отдавать
    аккаунт в кулдауне или мёртвый по статусу (иначе миграция ослабила бы защиту)."""
    body = _select_all_active_body()
    assert "cooldown_until" in body
    assert "banned" in body and "session_expired" in body


def test_client_actually_reads_these_fields():
    """Предпосылка: если `_make_client` перестанет их читать, выборка станет
    лишней — тест должен это заметить, а не тихо устареть."""
    am = (ROOT / "services" / "account_manager.py").read_text(encoding="utf-8")
    for key in ('device.get("proxy_id")', 'device.get("owner_id")'):
        assert key in am, f"{key} больше не читается — перечитать необходимость поля"
    assert "cf_relay_url" in am


def test_invite_uses_the_shared_door():
    m = re.search(r"async def _exec_mass_invite\(.*?(?=\nasync def )", WORKER, re.DOTALL)
    assert "select_all_active" in m.group(0), (
        "инвайт — самая баноопасная операция, он обязан получать аккаунты через "
        "единую флуд-осознанную дверь"
    )
