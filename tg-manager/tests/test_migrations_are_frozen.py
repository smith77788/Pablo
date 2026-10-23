"""Храповик: применённую миграцию нельзя изменить или удалить.

Раннер ведёт журнал `schema_migrations` и уже применённый файл второй раз не
проигрывает — это правильно (иначе каждый старт прокатывал бы 230+ файлов), но
у этого есть цена: **правка старого `schema_vN.sql` на прод не приедет
никогда.** У работающей базы файл помечен применённым и пропускается, а новое
окружение выполнит уже новый текст. Схема прода и схема кода расходятся молча;
всплывает это недели спустя «column does not exist» в проде при зелёных тестах.

Правило записано в CLAUDE.md («не удалять и не менять существующие миграции —
только ADD COLUMN вперёд»), но держалось на одной договорённости, и с июля
2026 его нарушали одиннадцать раз. Теперь его держит контрольная сумма:
`schema_checksums.txt` фиксирует содержимое каждого файла схемы.

Что делать, если тест упал:

* **добавили миграцию** — впишите её: `python deploy/scripts/lock_migrations.py`;
* **изменили старую** — так нельзя, заведите новый `schema_vN.sql` с нужным
  `ALTER`. Если файл заведомо ещё не уезжал в прод (создан в этой же ветке и
  не задеплоен), правьте его строку в манифесте руками — осознанно;
* **удалили миграцию** — верните: у прода она уже применена, а новое окружение
  без неё получит другую схему.
"""
from __future__ import annotations

import os

from database.migration_manifest import (
    collect_migration_checksums,
    migration_checksum,
    parse_checksum_manifest,
)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MANIFEST = os.path.join(_ROOT, "schema_checksums.txt")

_КАК_ЧИНИТЬ = (
    "Новый файл вписывается командой "
    "`python deploy/scripts/lock_migrations.py`. "
    "Изменение или удаление уже применённой миграции — запрещено: "
    "заведите новый schema_vN.sql."
)


def _locked() -> dict[str, str]:
    with open(_MANIFEST, encoding="utf-8") as f:
        return parse_checksum_manifest(f.read())


def test_манифест_на_месте_и_не_пустой():
    assert os.path.exists(_MANIFEST), "потерян schema_checksums.txt — храповик снят"
    assert len(_locked()) > 200, "манифест подозрительно короткий"


def test_миграции_не_менялись():
    на_диске = collect_migration_checksums(_ROOT)
    изменены = sorted(
        name for name, digest in _locked().items()
        if name in на_диске and на_диске[name] != digest
    )
    assert not изменены, (
        "изменены уже применённые миграции: "
        + ", ".join(изменены)
        + ". Прод их не переиграет — схема прода разойдётся с кодом. "
        + _КАК_ЧИНИТЬ
    )


def test_миграции_не_удалялись():
    на_диске = collect_migration_checksums(_ROOT)
    пропали = sorted(name for name in _locked() if name not in на_диске)
    assert not пропали, (
        "удалены миграции: " + ", ".join(пропали)
        + ". У прода они применены, у нового окружения их не будет. "
        + _КАК_ЧИНИТЬ
    )


def test_новые_миграции_вписаны_в_манифест():
    locked = _locked()
    новые = sorted(name for name in collect_migration_checksums(_ROOT) if name not in locked)
    assert not новые, (
        "миграции не зафиксированы в schema_checksums.txt: "
        + ", ".join(новые)
        + ". " + _КАК_ЧИНИТЬ
    )


def test_сумма_считается_от_того_же_текста_что_применяет_раннер():
    # Раннер читает файл и делает .strip() — хвостовой перевод строки не
    # должен выглядеть правкой миграции, иначе храповик будет красным на ровном
    # месте и его снимут.
    sql = "ALTER TABLE t ADD COLUMN IF NOT EXISTS c TEXT;"
    assert migration_checksum(sql) == migration_checksum("\n" + sql + "\n\n  ")
    assert migration_checksum(sql) != migration_checksum(sql + " -- правка")


def test_манифест_переживает_комментарии_и_пустые_строки():
    текст = "# заголовок\n\nabc123  schema.sql\n  def456  schema_v2.sql  \n#ffff  schema_v3.sql\n"
    assert parse_checksum_manifest(текст) == {"schema.sql": "abc123", "schema_v2.sql": "def456"}
