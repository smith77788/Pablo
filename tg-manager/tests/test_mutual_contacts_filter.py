"""Mutual Contacts фильтр (раздел 8 паритета TE).

Contact.mutual вычислялся, но: (а) не было колонки is_mutual, (б) не было
фильтра «только взаимные». Колонка (schema_v152 + self-heal), persist в
sync_service (уже есть), и фильтр: repository.mutual_only → endpoint mutual=1 →
UI-чип «↔ Взаимные».
"""
from __future__ import annotations

import inspect

from services.contacts_hub import repository
from services import mini_app_api


def test_repository_supports_mutual_only():
    sig = inspect.signature(repository.get_contacts)
    assert "mutual_only" in sig.parameters
    # Условие вынесено в единый конструктор WHERE (общий для списка и сегмента).
    src = inspect.getsource(repository._segment_where)
    assert "is_mutual = TRUE" in src


def test_endpoint_accepts_mutual_param():
    src = inspect.getsource(mini_app_api)
    assert "request.query.get('mutual')" in src
    assert "mutual_only=mutual" in src


def test_schema_and_selfheal_add_is_mutual():
    from pathlib import Path
    root = Path(repository.__file__).resolve().parents[2]
    assert "ADD COLUMN IF NOT EXISTS is_mutual" in (root / "schema_v152.sql").read_text()
    assert "ADD COLUMN IF NOT EXISTS is_mutual" in (root / "main.py").read_text()


def test_ui_has_mutual_chip():
    from pathlib import Path
    html = (Path(repository.__file__).resolve().parents[2] / "mini_app" / "index.html").read_text(encoding="utf-8")
    assert "filterContacts('mutual'" in html
    assert "params.set('mutual', '1')" in html
