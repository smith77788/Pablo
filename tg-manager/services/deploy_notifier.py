"""
Deploy Notifier — уведомление админов о новом деплое при старте бота.

Вызывается при запуске main.py. Сравнивает BUILD_VERSION с последней записью
в deploy_log. Если версия изменилась — отправляет детальное уведомление
всем ADMIN_IDS с информацией о коммитах и изменениях.
"""
from __future__ import annotations

import logging
import os
import subprocess
from datetime import datetime, timezone

import asyncpg
from aiogram import Bot

from services.logger import log_exc_swallow

log = logging.getLogger(__name__)

_REPO_ROOT: str | None = None


def _repo_root() -> str:
    """Get the git repository root directory."""
    global _REPO_ROOT
    if _REPO_ROOT is not None:
        return _REPO_ROOT
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=3,
        )
        if result.returncode == 0:
            _REPO_ROOT = result.stdout.strip()
            return _REPO_ROOT
    except Exception:
        log_exc_swallow(log, "git rev-parse failed, using fallback root")
    # Fallback: assume we're in tg-manager/ subdirectory
    _REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return _REPO_ROOT


def _get_git_log(prev_sha: str | None = None, max_commits: int = 15) -> str:
    """Get formatted git log since a previous commit SHA.

    If prev_sha is None, returns the last max_commits commits.
    Returns empty string if git is not available.
    """
    try:
        if prev_sha:
            log_range = f"{prev_sha}..HEAD"
        else:
            log_range = f"-{max_commits}"

        result = subprocess.run(
            ["git", "log", log_range, "--oneline", "--no-decorate",
             "--max-count", str(max_commits)],
            capture_output=True, text=True, timeout=5,
            cwd=_repo_root(),
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        log_exc_swallow(log, "git log failed")
    return ""


def _get_git_diff_summary(prev_sha: str | None = None) -> str:
    """Get summary of changed files (--stat) since previous commit."""
    try:
        if prev_sha:
            log_range = f"{prev_sha}..HEAD"
        else:
            log_range = "HEAD~1..HEAD"

        result = subprocess.run(
            ["git", "diff", "--stat", log_range, "--", "tg-manager/"],
            capture_output=True, text=True, timeout=5,
            cwd=_repo_root(),
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        log_exc_swallow(log, "git diff --stat failed")
    return ""


def _get_current_sha() -> str:
    """Get current HEAD commit SHA."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=3,
            cwd=_repo_root(),
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        log_exc_swallow(log, "git rev-parse HEAD failed")
    return ""


async def notify_deploy(pool: asyncpg.Pool, bot: Bot) -> None:
    """Check if a new deployment happened and notify admins.

    Called once at bot startup. Compares current BUILD_VERSION with
    the last recorded deployment in deploy_log.
    """
    try:
        from bot.handlers.start import BUILD_VERSION
    except ImportError:
        log.warning("deploy_notifier: cannot import BUILD_VERSION")
        return

    # Get last recorded deployment
    last_deploy = await pool.fetchrow(
        "SELECT build, commit_sha, deployed_at FROM deploy_log ORDER BY id DESC LIMIT 1"
    )

    if last_deploy and last_deploy["build"] == BUILD_VERSION:
        log.info("deploy_notifier: build %s already notified, skipping", BUILD_VERSION)
        return

    previous_sha = last_deploy["commit_sha"] if last_deploy else None
    current_sha = _get_current_sha()
    branch = os.getenv("RAILWAY_GIT_BRANCH", "")

    # Get git info
    git_log = _get_git_log(previous_sha, max_commits=15) if previous_sha else _get_git_log(None, max_commits=15)
    diff_summary = _get_git_diff_summary(previous_sha) if previous_sha else ""

    # Build notification
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    deployed_at_prev = last_deploy["deployed_at"].strftime("%Y-%m-%d %H:%M UTC") if last_deploy else "—"

    lines = [
        "🚀 <b>Новый деплой!</b>",
        "",
        f"📦 <b>Build:</b> <code>{BUILD_VERSION}</code>",
        f"🕐 <b>Время деплоя:</b> {now}",
    ]

    if last_deploy:
        lines.append(f"⏮ <b>Предыдущий деплой:</b> {deployed_at_prev}")
        lines.append(f"📋 <b>Версия до:</b> <code>{last_deploy['build']}</code>")

    if branch:
        lines.append(f"🌿 <b>Ветка:</b> <code>{branch}</code>")
    if current_sha:
        lines.append(f"🔖 <b>HEAD:</b> <code>{current_sha[:12]}</code>")

    if git_log:
        lines.append("")
        lines.append("📝 <b>Коммиты:</b>")
        # Add each commit line, truncating long messages
        for line in git_log.split("\n")[:15]:
            safe_line = line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            if len(safe_line) > 200:
                safe_line = safe_line[:200] + "…"
            lines.append(f"  <code>{safe_line}</code>")

    if diff_summary:
        diff_preview = diff_summary[:800]
        diff_preview = diff_preview.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        lines.append("")
        lines.append("📊 <b>Изменённые файлы:</b>")
        lines.append(f"<pre>{diff_preview}</pre>")

    lines.append("")
    lines.append(f"🤖 <i>BotMother OS — Build {BUILD_VERSION}</i>")

    text = "\n".join(lines)

    # Send to all admins
    admin_ids_raw = os.getenv("ADMIN_IDS", "")
    admin_ids = {int(x.strip()) for x in admin_ids_raw.split(",") if x.strip().isdigit()}

    if not admin_ids:
        log.warning("deploy_notifier: no ADMIN_IDS configured, skipping notification")
        # Still record the deployment
        await pool.execute(
            "INSERT INTO deploy_log(build, commit_sha, commit_msg, branch, notified)"
            " VALUES($1,$2,$3,$4,true)",
            BUILD_VERSION, current_sha or None,
            git_log.split("\n")[0] if git_log else None,
            branch or None,
        )
        return

    notified = 0
    for admin_id in admin_ids:
        # Check per-user notification preference
        try:
            from database.db import get_notification_settings
            settings = await get_notification_settings(pool, admin_id)
            if not settings.get("deploy", True):
                log.info("deploy_notifier: admin %d opted out of deploy notifications", admin_id)
                continue
        except Exception:
            log_exc_swallow(log, "get_notification_settings failed, still trying to send", admin_id=admin_id)

        try:
            await bot.send_message(admin_id, text, parse_mode="HTML")
            notified += 1
        except Exception as e:
            log.warning("deploy_notifier: failed to send to admin %d: %s", admin_id, e)

    # Record deployment
    await pool.execute(
        "INSERT INTO deploy_log(build, commit_sha, commit_msg, branch, notified)"
        " VALUES($1,$2,$3,$4,$5)",
        BUILD_VERSION,
        current_sha or None,
        git_log.split("\n")[0] if git_log else None,
        branch or None,
        notified > 0,
    )

    log.info("deploy_notifier: notified %d/%d admins about build %s",
             notified, len(admin_ids), BUILD_VERSION)
