"""D18 timezone + D10 confirm parity (batch of the 20-direction pass).

- saveSchedule раньше делал new Date(dateVal+'Z') — трактовал ЛОКАЛЬНЫЙ ввод как UTC
  (сдвиг на оффсет). Фикс: new Date(dateVal) (локальное) → localToUtcIso; preload —
  toLocalInput (локальное настенное), а не toISOString (UTC).
- self-promo blast теперь требует подтверждение (паритет с mass_publish/invite/report).
"""
from __future__ import annotations
import re
from pathlib import Path

HTML = (Path(__file__).resolve().parent.parent / "mini_app" / "index.html").read_text(encoding="utf-8")


def test_helpers_present():
    assert "function localToUtcIso(v)" in HTML and "function toLocalInput(d)" in HTML


def test_saveSchedule_uses_local_not_utc_z():
    m = re.search(r"async function saveSchedule\(\)\s*\{(.*?)\n\}", HTML, re.DOTALL)
    assert m, "saveSchedule не найден"
    body = m.group(1)
    assert "new Date(dateVal+'Z')" not in body, "не должен трактовать локальный ввод как UTC"
    assert "new Date(dateVal)" in body


def test_schedule_preload_is_local():
    # дефолт поля — локальное настенное (toLocalInput), не UTC toISOString
    assert "schDate').value = toLocalInput(d)" in HTML


def test_self_promo_launch_confirms():
    m = re.search(r"async function launchSelfPromo\(id,btn\)\s*\{(.*?)\n\}", HTML, re.DOTALL)
    assert m and "askConfirm" in m.group(1), "self-promo blast должен подтверждаться"
