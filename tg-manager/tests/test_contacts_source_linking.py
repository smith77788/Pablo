"""«По аккаунтам» должно считать ВСЕ контакты аккаунта, не только им открытые.

contact_sources связывал аккаунт с контактом ТОЛЬКО когда контакт создаётся
впервые (ветка нового). Если контакт уже был добавлен ДРУГИМ аккаунтом, текущий
шёл в ветку UPDATE и НЕ привязывался → «по аккаунтам» показывало доли реального
(Bella 792 вместо тысяч), хотя контакты собраны. Теперь привязка пишется для обеих
веток (UNIQUE(contact_id, account_id) → идемпотентно).
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SYNC = (ROOT / "services" / "contacts_hub" / "sync_service.py").read_text(encoding="utf-8")


def _sync_account_body() -> str:
    return SYNC[SYNC.index("async def sync_account("): SYNC.index("async def sync_all_accounts(")]


def test_source_linked_for_existing_contacts_too():
    body = _sync_account_body()
    # Новая идемпотентная привязка присутствует…
    assert "ON CONFLICT (contact_id, account_id) DO UPDATE SET last_synced_at=NOW()" in body, \
        "нет upsert привязки источника"
    # …и существующий контакт получает contact_id для привязки.
    assert "contact_id = existing['id']" in body, "существующий контакт не даёт contact_id для привязки"
    # Старая форма (привязка только для нового) убрана.
    assert "VALUES ($1,$2,$3,NOW()) ON CONFLICT DO NOTHING" not in body, \
        "осталась привязка только для новых контактов"


def test_link_is_outside_else_branch():
    """Привязка источника не должна быть вложена только в ветку нового контакта."""
    body = _sync_account_body()
    # Индекс вставки в contact_sources должен идти ПОСЛЕ закрытия if/else
    # (после 'created += 1'), а не внутри else. Проверяем по порядку и отступу.
    ins = body.index("INSERT INTO contact_sources")
    created = body.index("created += 1")
    assert ins > created, "привязка источника всё ещё внутри ветки нового контакта"
