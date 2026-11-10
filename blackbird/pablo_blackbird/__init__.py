"""Обёртка над Blackbird (OSINT-поиск аккаунтов по username/email).

Модуль изолирован от tg-manager: свой каталог, свой venv, свой процесс.
Код Blackbird лежит в ``blackbird/upstream`` без правок и запускается
отдельным процессом. Обёртка решает три задачи:

1. Upstream пишет ``data/``, ``logs/`` и ``results/`` рядом со своим кодом и
   ищет файлы относительно текущего каталога. В контейнере код принадлежит
   root, процесс работает от ``app`` — писать рядом с кодом нельзя. Поэтому
   upstream копируется в рабочий каталог (``BLACKBIRD_WORKDIR``, по умолчанию
   ``/tmp/blackbird``) и запускается оттуда.
2. Результат забирается из JSON-экспорта (``--json``) и отдаётся словарём.
3. Запуски сериализуются файловой блокировкой: общий каталог результатов и
   список сайтов WhatsMyName не рассчитаны на параллельную запись.

AI-режим upstream (``--ai``) намеренно не поддерживается: он отправляет данные
на сторонний сервер и ждёт подтверждения с клавиатуры.
"""

from __future__ import annotations

import fcntl
import glob
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parent.parent
UPSTREAM_DIR = MODULE_DIR / "upstream"
DEFAULT_WORKDIR = "/tmp/blackbird"
# Защита от зависания: upstream по умолчанию ждёт каждый сайт до 30 секунд,
# сайтов ~600, параллельно 30 — нормальный прогон укладывается в пару минут.
DEFAULT_RUN_TIMEOUT = 600

__all__ = ["BlackbirdError", "SearchResult", "search"]


class BlackbirdError(RuntimeError):
    """Blackbird не отработал: упал, завис или вернул нечитаемый результат."""


@dataclass
class SearchResult:
    query: str
    kind: str  # "username" | "email"
    accounts: list[dict] = field(default_factory=list)
    duration_sec: float = 0.0

    @property
    def found(self) -> list[dict]:
        return [a for a in self.accounts if a.get("status") == "FOUND"]

    def to_dict(self) -> dict:
        return {
            "query": self.query,
            "kind": self.kind,
            "found_count": len(self.found),
            "accounts": self.accounts,
            "duration_sec": round(self.duration_sec, 2),
        }


def _workdir() -> Path:
    return Path(os.environ.get("BLACKBIRD_WORKDIR") or DEFAULT_WORKDIR)


def _upstream_stamp(upstream: Path) -> str:
    """Отпечаток версии upstream: меняется, если код в образе обновили."""
    marker = upstream / "blackbird.py"
    st = marker.stat()
    return f"{st.st_size}:{int(st.st_mtime)}"


def _prepare_workdir(upstream: Path, workdir: Path) -> Path:
    """Копия upstream в каталоге на запись. Скачанный список сайтов сохраняется."""
    app = workdir / "app"
    stamp_file = workdir / ".upstream_stamp"
    stamp = _upstream_stamp(upstream)
    if app.is_dir() and stamp_file.is_file() and stamp_file.read_text() == stamp:
        return app
    cached_list = app / "data" / "wmn-data.json"
    saved = None
    if cached_list.is_file():
        saved = workdir / "wmn-data.json.keep"
        shutil.copy2(cached_list, saved)
    if app.exists():
        shutil.rmtree(app)
    shutil.copytree(
        upstream, app, ignore=shutil.ignore_patterns("__pycache__", "tests")
    )
    if saved is not None:
        shutil.move(str(saved), cached_list)
    stamp_file.write_text(stamp)
    return app


def _validate_query(value: str) -> str:
    value = (value or "").strip()
    if not value:
        raise ValueError("пустой запрос")
    if len(value) > 256 or value.startswith("-") or any(
        c in value for c in "/\\\x00\n\r"
    ):
        raise ValueError(f"недопустимый запрос: {value!r}")
    return value


def search(
    username: str | None = None,
    email: str | None = None,
    *,
    timeout: int = 30,
    max_concurrent_requests: int = 30,
    no_nsfw: bool = True,
    filter_expr: str | None = None,
    proxy: str | None = None,
    no_update: bool = False,
    run_timeout: int = DEFAULT_RUN_TIMEOUT,
    python: str | None = None,
    upstream_dir: Path | None = None,
) -> SearchResult:
    """Найти аккаунты по одному username ИЛИ одному email."""
    if bool(username) == bool(email):
        raise ValueError("нужен ровно один из параметров: username или email")
    kind = "username" if username else "email"
    query = _validate_query(username or email or "")

    upstream = Path(upstream_dir or UPSTREAM_DIR)
    workdir = _workdir()
    workdir.mkdir(parents=True, exist_ok=True)

    cmd = [
        python or sys.executable,
        "blackbird.py",
        f"--{kind}",
        query,
        "--json",
        "--timeout",
        str(int(timeout)),
        "--max-concurrent-requests",
        str(int(max_concurrent_requests)),
    ]
    if no_nsfw:
        cmd.append("--no-nsfw")
    if filter_expr:
        cmd += ["--filter", filter_expr]
    if proxy:
        cmd += ["--proxy", proxy]
    if no_update:
        cmd.append("--no-update")

    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    # rich иначе пытается рисовать «живой» прогресс в несуществующий терминал.
    env["TERM"] = "dumb"

    with open(workdir / ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        app = _prepare_workdir(upstream, workdir)
        results_dir = app / "results"
        shutil.rmtree(results_dir, ignore_errors=True)
        started = time.monotonic()
        try:
            proc = subprocess.run(
                cmd,
                check=False,
                cwd=app,
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=run_timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise BlackbirdError(f"Blackbird не уложился в {run_timeout} с") from exc
        duration = time.monotonic() - started
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip()[-2000:]
            raise BlackbirdError(f"Blackbird завершился с кодом {proc.returncode}: {tail}")

        # JSON пишется только если что-то найдено; нет файла — нет находок.
        accounts: list[dict] = []
        for path in sorted(glob.glob(str(results_dir / "*" / "*.json"))):
            try:
                with open(path, encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, ValueError) as exc:
                raise BlackbirdError(f"не удалось прочитать результат {path}") from exc
            if isinstance(data, list):
                accounts.extend(a for a in data if isinstance(a, dict))
        shutil.rmtree(results_dir, ignore_errors=True)

    return SearchResult(query=query, kind=kind, accounts=accounts, duration_sec=duration)
