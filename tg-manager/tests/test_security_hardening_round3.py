"""Третий проход по плану безопасности: то, что осталось несделанным.

* Секреты в репозитории: шаблоны настоящих ключей (токены ботов, AWS, GitHub,
  OpenAI/Anthropic, Stripe, Google, Slack, приватные ключи) не должны попадать
  ни в один файл под git. В тестах разрешены только заведомо фейковые токены
  бота с id 123456789 / 1234567890.
* Bandit в CI: шаг есть и падает на высоких находках.
* Контейнер не от root: точка входа сбрасывает права через setpriv.
* Срок хранения собранных аудиторий: включается переменной, без неё ничего
  не удаляется; удаляет только старое.

Postgres-часть пропускается без INFRAGRAM_TEST_DSN (в CI база есть).
"""
from __future__ import annotations

import asyncio
import os
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
TG = REPO / "tg-manager"

_PATTERNS = {
    "telegram_bot_token": re.compile(r"\b(\d{8,10}):AA[A-Za-z0-9_-]{33}\b"),
    "aws_access_key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    # Тело ключа обязательно: заголовок с мусором внутри — тестовая заглушка.
    "private_key": re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----(?:\\n|\s)+[A-Za-z0-9+/=]{40,}"),
    "openai_key": re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{32,}\b"),
    "anthropic_key": re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}"),
    "github_token": re.compile(r"\b(?:ghp|gho|ghs|ghu|github_pat)_[A-Za-z0-9_]{30,}"),
    "stripe_live_key": re.compile(r"\b[sr]k_live_[A-Za-z0-9]{20,}"),
    "google_api_key": re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    "slack_token": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),
}
_FAKE_BOT_IDS = {"123456789", "1234567890"}
_BINARY = re.compile(
    r"\.(png|jpe?g|gif|webp|ico|apk|jar|keystore|woff2?|ttf|otf|pdf|zip|gz|lock)$", re.I)


def _tracked_files() -> list[Path]:
    out = subprocess.run(["git", "ls-files", "-z"], cwd=REPO, capture_output=True, check=True)
    return [REPO / p for p in out.stdout.decode().split("\0") if p and not _BINARY.search(p)]


def test_no_real_secrets_in_tracked_files():
    found = []
    for path in _tracked_files():
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except (OSError, IsADirectoryError):
            continue
        rel = path.relative_to(REPO).as_posix()
        in_tests = "/tests/" in f"/{rel}"
        for name, rx in _PATTERNS.items():
            for m in rx.finditer(text):
                if name == "telegram_bot_token" and in_tests and m.group(1) in _FAKE_BOT_IDS:
                    continue
                line = text.count("\n", 0, m.start()) + 1
                found.append(f"{rel}:{line} {name} {m.group()[:10]}…")
    assert not found, (
        "В репозитории похожие на настоящие секреты. Ключ из git уже утёк: "
        "отзовите его у провайдера и уберите из файла.\n" + "\n".join(found))


def test_secret_patterns_catch_real_shapes():
    tok = "7012345678:AA" + "b" * 33
    assert _PATTERNS["telegram_bot_token"].search(tok)
    assert _PATTERNS["aws_access_key"].search("AKIA" + "Q" * 16)
    assert _PATTERNS["private_key"].search(
        "-----BEGIN PRIVATE KEY-----\\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQ")
    assert not _PATTERNS["private_key"].search("-----BEGIN PRIVATE KEY-----\nмусор\n")


def test_ci_runs_bandit_on_high_findings():
    wf = (REPO / ".github/workflows/tests.yml").read_text(encoding="utf-8")
    assert "bandit" in wf
    step = wf[wf.index("bandit"):]
    assert "-lll" in step[:600] and "-iii" in step[:600], "Bandit должен падать на высоких находках"


def test_container_drops_root_before_starting_app():
    dockerfile = (REPO / "Dockerfile").read_text(encoding="utf-8")
    assert re.search(r"useradd[^\n]*\bapp\b", dockerfile)
    assert 'CMD ["bash", "/app/deploy-entrypoint.sh"]' in dockerfile
    ep = (REPO / "deploy-entrypoint.sh").read_text(encoding="utf-8")
    assert "setpriv --reuid=" in ep and "--no-new-privs" in ep
    assert "exec setpriv" in ep, "права сбрасываются через exec, root-процесс не остаётся"
    assert "RAILWAY_VOLUME_MOUNT_PATH" in ep, "том Railway отдаётся app до сброса прав"
    subprocess.run(["bash", "-n", str(REPO / "deploy-entrypoint.sh")], check=True)


# ── Срок хранения аудиторий ─────────────────────────────────────────────────
def test_audience_retention_is_off_by_default(monkeypatch):
    from services import db_maintenance as dm
    monkeypatch.delenv("AUDIENCE_RETENTION_DAYS", raising=False)
    assert dm.audience_retention_days() == 0
    monkeypatch.setenv("AUDIENCE_RETENTION_DAYS", "abc")
    assert dm.audience_retention_days() == 0
    monkeypatch.setenv("AUDIENCE_RETENTION_DAYS", "2")
    assert dm.audience_retention_days() == 7
    monkeypatch.setenv("AUDIENCE_RETENTION_DAYS", "180")
    assert dm.audience_retention_days() == 180


DSN = os.getenv("INFRAGRAM_TEST_DSN", "")


@pytest.mark.skipif(not DSN, reason="нужен живой Postgres: INFRAGRAM_TEST_DSN")
def test_audience_retention_deletes_only_old_rows():
    import asyncpg

    from services import db_maintenance as dm

    name = "aud_retention"
    db = re.sub(r"/[^/?]+(\?|$)", rf"/{name}\1", DSN, count=1)

    async def go():
        admin = await asyncpg.connect(DSN)
        await admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
        await admin.execute(f"CREATE DATABASE {name}")
        await admin.close()
        pool = await asyncpg.create_pool(db, min_size=1, max_size=2)
        try:
            await pool.execute(
                "CREATE TABLE parsed_audiences(id bigserial PRIMARY KEY, owner_id bigint NOT NULL,"
                " tg_user_id bigint NOT NULL, parsed_at timestamptz DEFAULT now())")
            await pool.execute(
                "INSERT INTO parsed_audiences(owner_id, tg_user_id, parsed_at) VALUES"
                " (1, 10, now() - interval '400 days'), (1, 11, now()),"
                " (2, 20, now() - interval '200 days'), (2, 21, now() - interval '10 days')")
            n, err = await dm._prune_parsed_audiences(pool, 180)
            assert err is None and n == 2
            left = sorted(r["tg_user_id"] for r in await pool.fetch(
                "SELECT tg_user_id FROM parsed_audiences"))
            assert left == [11, 21]
        finally:
            await pool.close()
            admin = await asyncpg.connect(DSN)
            await admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
            await admin.close()

    asyncio.run(go())
