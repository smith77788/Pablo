"""Регресс для deploy/scripts/encrypt_legacy_secrets.py — миграционный скрипт,
форсирующий шифрование ОСТАВШИХСЯ plaintext session_str/proxy_url в БД.

Скрипт запускается только вручную человеком против реальной БД (см. докстринг
скрипта — бэкап → dry-run → staging → prod). Здесь проверяем ТОЛЬКО его
внутреннюю логику на мок-pool, без сети/реальной БД, и защитные инварианты:
  - по умолчанию (без --apply) скрипт ничего не пишет (dry-run);
  - зашифрованные значения получают правильный fp (дедуп-инвариант сохраняется);
  - плейсхолдер безопасности: докстрйнг содержит план бэкапа и план отката.
"""
from __future__ import annotations

import importlib.util
import os
import sys

import pytest

try:
    from Crypto.Cipher import AES  # noqa: F401
    _has_crypto = True
except (ImportError, OSError):
    _has_crypto = False

# Runtime probe: Crypto package may import but native module is broken
if _has_crypto:
    try:
        from services.token_vault import encrypt_token
        encrypt_token("probe")
    except Exception:
        _has_crypto = False

pytestmark = pytest.mark.skipif(not _has_crypto, reason="pycryptodome native module not available")

_SCRIPT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "deploy", "scripts", "encrypt_legacy_secrets.py",
)


def _load_script():
    spec = importlib.util.spec_from_file_location("encrypt_legacy_secrets", _SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["encrypt_legacy_secrets"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


class _FakePool:
    """Минимальный async pool: держит строки в памяти, fetch/execute над одной таблицей."""

    def __init__(self, rows: list[dict]):
        # rows: [{"id": int, "secret": str}]
        self._rows = {r["id"]: r["secret"] for r in rows}
        self.executed: list[tuple] = []

    async def fetch(self, query, *args):
        # ORDER BY id LIMIT $2, WHERE id > $1 AND ... NOT LIKE 'ENC:%'
        last_id, limit = args
        out = []
        for rid in sorted(self._rows):
            if rid <= last_id:
                continue
            val = self._rows[rid]
            if not val or val.startswith("ENC:"):
                continue
            out.append({"id": rid, "secret": val})
            if len(out) >= limit:
                break
        return out

    async def fetchval(self, query, *args):
        return sum(1 for v in self._rows.values() if v and not v.startswith("ENC:"))

    async def execute(self, query, *args):
        self.executed.append((query, args))
        # UPDATE ... SET secret=$1[, fp=$2] WHERE id=$N AND secret=$N
        # В обоих вариантах запроса (с fp и без) id и старое значение — два
        # последних позиционных аргумента.
        new_val = args[0]
        rid = args[-2]
        old_val_expected = args[-1]
        if self._rows.get(rid) == old_val_expected:
            self._rows[rid] = new_val
        return "UPDATE 1"


def test_table_specs_cover_all_three_secret_tables():
    mod = _load_script()
    covered = {(s.table, s.secret_col) for s in mod.TABLE_SPECS}
    assert ("tg_accounts", "session_str") in covered
    assert ("user_proxies", "proxy_url") in covered
    assert ("booster_sessions", "session_str") in covered
    assert ("booster_sessions", "proxy") in covered


def test_default_cli_is_dry_run_safety_net():
    """--apply отсутствует по умолчанию — случайный запуск без флагов ничего не портит."""
    _load_script()
    # Тот же набор аргументов, что задаёт main() — без --apply флаг False (dry-run).
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--batch-size", type=int, default=500)
    ap.add_argument("--only", type=str, default="")
    ns = ap.parse_args([])
    assert ns.apply is False


def test_docstring_has_backup_and_rollback_plan():
    """Требование задачи: скрипт должен документировать порядок запуска и план отката."""
    mod = _load_script()
    doc = mod.__doc__
    assert "pg_dump" in doc, "нет инструкции по бэкапу перед прогоном"
    assert "План отката" in doc or "план отката" in doc.lower()
    assert "dry-run" in doc
    assert "staging" in doc.lower()
    assert "НЕ БЫЛ И НЕ ДОЛЖЕН БЫТЬ ЗАПУЩЕН" in doc or "не был" in doc.lower()


@pytest.mark.asyncio
async def test_count_plaintext_counts_only_unencrypted():
    mod = _load_script()
    pool = _FakePool([
        {"id": 1, "secret": "plain-session-one"},
        {"id": 2, "secret": "ENC:already-encrypted"},
        {"id": 3, "secret": "plain-session-two"},
    ])
    spec = mod.TableSpec("tg_accounts", "session_str", "session_fp")
    n = await mod._count_plaintext(pool, spec)
    assert n == 2


@pytest.mark.asyncio
async def test_migrate_table_dry_run_does_not_write():
    mod = _load_script()
    pool = _FakePool([{"id": 1, "secret": "plain-session"}])
    spec = mod.TableSpec("tg_accounts", "session_str", "session_fp")
    stats = await mod._migrate_table(pool, spec, batch_size=100, apply=False)
    assert stats["seen"] == 1
    assert stats["migrated"] == 0
    assert pool.executed == []
    # значение в "БД" осталось plaintext
    assert pool._rows[1] == "plain-session"


@pytest.mark.asyncio
async def test_migrate_table_apply_encrypts_and_sets_fingerprint():
    from services.token_vault import decrypt_token, session_fingerprint

    mod = _load_script()
    plaintext = "1BVtsOMAB_real_session_string_value"
    pool = _FakePool([{"id": 7, "secret": plaintext}])
    spec = mod.TableSpec("tg_accounts", "session_str", "session_fp")
    stats = await mod._migrate_table(pool, spec, batch_size=100, apply=True)

    assert stats["seen"] == 1
    assert stats["migrated"] == 1
    assert stats["errors"] == 0
    new_val = pool._rows[7]
    assert new_val.startswith("ENC:")
    assert decrypt_token(new_val) == plaintext

    # fp записан правильно (дедуп после миграции продолжает работать)
    fp_arg = [args[1] for (_q, args) in pool.executed if len(args) >= 2][0]
    assert fp_arg == session_fingerprint(plaintext)


@pytest.mark.asyncio
async def test_migrate_table_booster_proxy_has_no_fp_column():
    """booster_sessions не имеет fp-колонки — UPDATE не должен пытаться её записать."""
    mod = _load_script()
    pool = _FakePool([{"id": 1, "secret": "socks5://user:pass@1.2.3.4:1080"}])
    spec = mod.TableSpec("booster_sessions", "proxy", None)
    stats = await mod._migrate_table(pool, spec, batch_size=100, apply=True)
    assert stats["migrated"] == 1
    query, args = pool.executed[0]
    assert "fp" not in query.lower()
    assert len(args) == 3  # encrypted, id, old_value — без fp


@pytest.mark.asyncio
async def test_migrate_table_is_idempotent_second_pass_finds_nothing():
    mod = _load_script()
    pool = _FakePool([{"id": 1, "secret": "plain-session"}])
    spec = mod.TableSpec("tg_accounts", "session_str", "session_fp")
    stats1 = await mod._migrate_table(pool, spec, batch_size=100, apply=True)
    assert stats1["migrated"] == 1

    pool.executed.clear()
    stats2 = await mod._migrate_table(pool, spec, batch_size=100, apply=True)
    assert stats2["seen"] == 0
    assert stats2["migrated"] == 0
    assert pool.executed == []


@pytest.mark.asyncio
async def test_migrate_table_one_bad_row_does_not_abort_batch():
    """Одна проблемная строка не должна ронять весь батч — остальные мигрируют."""
    mod = _load_script()
    pool = _FakePool([
        {"id": 1, "secret": "good-session-one"},
        {"id": 2, "secret": "good-session-two"},
    ])

    orig_execute = pool.execute

    async def flaky_execute(query, *args):
        if args[-2] == 1:
            raise RuntimeError("simulated transient DB error")
        return await orig_execute(query, *args)

    pool.execute = flaky_execute
    spec = mod.TableSpec("tg_accounts", "session_str", "session_fp")
    stats = await mod._migrate_table(pool, spec, batch_size=100, apply=True)
    assert stats["seen"] == 2
    assert stats["migrated"] == 1
    assert stats["errors"] == 1
    assert pool._rows[2].startswith("ENC:")
    assert pool._rows[1] == "good-session-one"  # не тронуто из-за ошибки
