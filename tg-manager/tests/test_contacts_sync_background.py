"""Синхронизация контактов флота — фоновая операция, а не инлайн в HTTP-запросе.

СИМПТОМ. Пользователь добавил 28 аккаунтов (у каждого есть контакты), но хаб
показывал «Нет контактов». Причина — не хранение и не чтение (оба через
unified_contacts, скоуп по owner_id), а то, что `uch_sync` выполнял
sync_all_accounts ИНЛАЙН в запросе: 28 Telethon-подключений не укладывались в
таймаут шлюза, клиент отключался, aiohttp отменял обработчик на середине — в БД
попадала лишь часть контактов или ноль.

ФИКС. uch_sync ставит операцию contacts_sync в operation_bus и сразу отвечает;
op_worker обрабатывает весь флот без таймаута и пишет контакты по мере обработки.
Гейт держит эту архитектуру: инлайн-синхронизации в эндпойнте быть не должно.
"""
from __future__ import annotations

import re
from pathlib import Path
from services import op_worker

ROOT = Path(__file__).resolve().parents[1]
API = (ROOT / "services" / "mini_app_api.py").read_text(encoding="utf-8")
BUS = (ROOT / "services" / "operation_bus.py").read_text(encoding="utf-8")
WORKER = (ROOT / "services" / "op_worker.py").read_text(encoding="utf-8")
HTML = (ROOT / "mini_app" / "index.html").read_text(encoding="utf-8")


def test_op_type_registered():
    assert '"contacts_sync"' in BUS, "contacts_sync не зарегистрирован в OP_REGISTRY"


def test_worker_routes_and_implements():
    assert op_worker.handler_for("contacts_sync") is not None, "нет ветки диспетчера op_worker"
    assert "async def _exec_contacts_sync(" in WORKER, "нет исполнителя"
    m = re.search(r"async def _exec_contacts_sync\(.*?\n\n\nasync def ", WORKER, re.S)
    assert m, "не удалось выделить тело исполнителя"
    body = m.group(0)
    assert "sync_all_accounts(" in body, "исполнитель не запускает синхронизацию флота"


def test_endpoint_enqueues_not_inline():
    """uch_sync ставит операцию и НЕ синхронизирует инлайн (иначе снова таймаут)."""
    m = re.search(r"async def uch_sync\(.*?\n    async def ", API, re.S)
    assert m, "uch_sync не найден"
    body = m.group(0)
    assert 'operation_bus.submit' in body, "uch_sync не ставит операцию в шину"
    assert '"contacts_sync"' in body, "uch_sync ставит не тот тип операции"
    # Ключевой инвариант: НИКАКОГО инлайн-прохода флота в самом запросе.
    assert "await sync_all_accounts(" not in body, (
        "uch_sync снова синхронизирует инлайн — 20+ аккаунтов оборвутся по таймауту"
    )


def test_endpoint_returns_queued_and_front_handles_it():
    assert '"queued": True' in API, "эндпойнт не сообщает о постановке в очередь"
    m = re.search(r"async function syncContacts\(\)\s*\{.*?\n\}", HTML, re.S)
    assert m, "syncContacts не найден"
    body = m.group(0)
    assert "d.queued" in body, "фронт не обрабатывает фоновый ответ"
    # После постановки — дозагрузка списка по мере обработки (не разовый показ 0).
    assert "loadContacts()" in body and "setTimeout(" in body, "нет дозагрузки контактов"
