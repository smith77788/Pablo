"""
Deploy Notifier — уведомление админов о новом деплое при старте бота.

Вызывается при запуске main.py. Ждёт 60с (бот должен принимать пользователей
первым), затем сравнивает BUILD_VERSION с последней записью в deploy_log.

ВАЖНО: все subprocess.run() вызовы перенесены в asyncio.to_thread() чтобы
не блокировать event loop — без этого бот зависал на 10-15с после старта.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from datetime import datetime, timezone

import asyncpg
from aiogram import Bot

from services.logger import log_exc_swallow

log = logging.getLogger(__name__)

_REPO_ROOT: str | None = None


def _repo_root_sync() -> str:
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
        pass
    _REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return _REPO_ROOT


def _get_git_log_sync(prev_sha: str | None = None, max_commits: int = 15) -> str:
    try:
        log_range = f"{prev_sha}..HEAD" if prev_sha else f"-{max_commits}"
        result = subprocess.run(
            ["git", "log", log_range, "--oneline", "--no-decorate",
             "--max-count", str(max_commits)],
            capture_output=True, text=True, timeout=5, cwd=_repo_root_sync(),
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
    return ""


def _get_git_diff_summary_sync(prev_sha: str | None = None) -> str:
    try:
        log_range = f"{prev_sha}..HEAD" if prev_sha else "HEAD~1..HEAD"
        result = subprocess.run(
            ["git", "diff", "--stat", log_range, "--", "tg-manager/"],
            capture_output=True, text=True, timeout=5, cwd=_repo_root_sync(),
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
    return ""


def _get_current_sha_sync() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=3, cwd=_repo_root_sync(),
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return ""


async def _get_platform_stats(pool: asyncpg.Pool) -> dict:
    """Collect platform stats — parallel queries, no sequential loops."""
    stats: dict = {
        "accounts": 0, "bots": 0,
        "pressure_score": 0, "pressure_emoji": "🟢", "pressure_label": "Норма",
    }
    try:
        acc_val, bot_val = await asyncio.gather(
            pool.fetchval("SELECT COUNT(*) FROM tg_accounts WHERE is_active=TRUE"),
            pool.fetchval("SELECT COUNT(*) FROM managed_bots WHERE is_active=TRUE"),
            return_exceptions=True,
        )
        if not isinstance(acc_val, BaseException):
            stats["accounts"] = acc_val or 0
        if not isinstance(bot_val, BaseException):
            stats["bots"] = bot_val or 0
    except Exception:
        pass
    return stats


async def notify_deploy(pool: asyncpg.Pool, bot: Bot) -> None:
    """Check if a new deployment happened and notify admins.

    Delays 60s so the bot serves users before doing any heavy work.
    All subprocess calls run in asyncio.to_thread() — never blocks event loop.
    """
    # Let the bot start serving users first
    await asyncio.sleep(60)

    try:
        from bot.handlers.start import BUILD_VERSION
    except ImportError:
        log.warning("deploy_notifier: cannot import BUILD_VERSION")
        return

    try:
        last_deploy = await pool.fetchrow(
            "SELECT build, commit_sha, deployed_at FROM deploy_log ORDER BY id DESC LIMIT 1"
        )
    except Exception as e:
        log.warning("deploy_notifier: deploy_log table not ready: %s", e)
        return

    if last_deploy and last_deploy["build"] == BUILD_VERSION:
        log.info("deploy_notifier: build %s already notified, skipping", BUILD_VERSION)
        return

    previous_sha = last_deploy["commit_sha"] if last_deploy else None
    branch = os.getenv("RAILWAY_GIT_BRANCH", "")

    # All subprocess calls in thread pool — zero event loop blocking
    current_sha, git_log, diff_summary = await asyncio.gather(
        asyncio.to_thread(_get_current_sha_sync),
        asyncio.to_thread(_get_git_log_sync, previous_sha, 15),
        asyncio.to_thread(_get_git_diff_summary_sync, previous_sha),
    )

    git_sha = os.getenv("RAILWAY_GIT_COMMIT_SHA", "")[:7] or (
        current_sha[:7] if current_sha else "local"
    )

    platform_stats = await _get_platform_stats(pool)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    deployed_at_prev = (
        last_deploy["deployed_at"].strftime("%Y-%m-%d %H:%M UTC")
        if last_deploy else "—"
    )

    lines = [
        "🚀 <b>Новый деплой!</b>", "",
        f"📦 <b>Build:</b> <code>{BUILD_VERSION}</code>",
        f"🕐 <b>Время деплоя:</b> {now}",
    ]
    if last_deploy:
        lines.append(f"⏮ <b>Предыдущий деплой:</b> {deployed_at_prev}")
        lines.append(f"📋 <b>Версия до:</b> <code>{last_deploy['build']}</code>")
    if branch:
        lines.append(f"🌿 <b>Ветка:</b> <code>{branch}</code>")
    if git_sha:
        lines.append(f"🔖 <b>Commit:</b> <code>{git_sha}</code>")

    lines += ["", "<b>📊 Платформа сейчас:</b>",
              f"  🤖 Аккаунтов: <b>{platform_stats['accounts']}</b>",
              f"  🤖 Ботов: <b>{platform_stats['bots']}</b>"]

    if git_log:
        lines += ["", "📝 <b>Коммиты:</b>"]
        for line in git_log.split("\n")[:15]:
            safe = line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            lines.append(f"  <code>{safe[:200]}</code>")

    if diff_summary:
        diff_preview = diff_summary[:800].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        lines += ["", "📊 <b>Изменённые файлы:</b>", f"<pre>{diff_preview}</pre>"]

    try:
        resume_ops, warmup_plans = await asyncio.gather(
            pool.fetchval("SELECT COUNT(*) FROM operation_queue WHERE status IN ('pending','running')"),
            pool.fetchval("SELECT COUNT(*) FROM account_warmup_plans WHERE status='active'"),
            return_exceptions=True,
        )
        resume_ops = resume_ops if not isinstance(resume_ops, BaseException) else 0
        warmup_plans = warmup_plans if not isinstance(warmup_plans, BaseException) else 0
        if resume_ops or warmup_plans:
            lines.append("")
            lines.append("♻️ <b>Авто-возобновление:</b>")
            if resume_ops:
                lines.append(f"  📋 Операций: <b>{resume_ops}</b>")
            if warmup_plans:
                lines.append(f"  🌡 Планов прогрева: <b>{warmup_plans}</b>")
    except Exception:
        pass

    lines += ["", f"🤖 <i>BotMother OS — Build {BUILD_VERSION}</i>"]
    text = "\n".join(lines)

    admin_ids_raw = os.getenv("ADMIN_IDS", "")
    admin_ids = {int(x.strip()) for x in admin_ids_raw.split(",") if x.strip().isdigit()}

    if not admin_ids:
        log.warning("deploy_notifier: no ADMIN_IDS configured")
    else:
        notified = 0
        for admin_id in admin_ids:
            try:
                from database.db import get_notification_settings
                settings = await get_notification_settings(pool, admin_id)
                if not settings.get("deploy", True):
                    continue
            except Exception:
                pass
            try:
                await bot.send_message(admin_id, text, parse_mode="HTML")
                notified += 1
            except Exception as e:
                log.warning("deploy_notifier: failed to send to %d: %s", admin_id, e)
        log.info("deploy_notifier: notified %d admins about build %s", notified, BUILD_VERSION)

    try:
        await pool.execute(
            "INSERT INTO deploy_log(build, commit_sha, commit_msg, branch, notified)"
            " VALUES($1,$2,$3,$4,$5)",
            BUILD_VERSION,
            current_sha or None,
            git_log.split("\n")[0] if git_log else None,
            branch or None,
            bool(admin_ids),
        )
    except Exception as e:
        log.warning("deploy_notifier: failed to record deployment: %s", e)
