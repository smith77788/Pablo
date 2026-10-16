from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "deploy"
    / "scripts"
    / "agent_workspace_guard.py"
)
SPEC = importlib.util.spec_from_file_location("agent_workspace_guard", SCRIPT)
assert SPEC and SPEC.loader
guard = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = guard
SPEC.loader.exec_module(guard)


def test_versioned_hooks_use_portable_python_launcher():
    project = Path(__file__).resolve().parents[1]
    launcher = (project / ".githooks" / "run-workspace-guard.sh").read_text(
        encoding="utf-8"
    )

    assert ".venv/Scripts/python.exe" in launcher
    assert ".venv/bin/python" in launcher
    assert 'exec "$PYTHON"' in launcher
    for hook in ("pre-commit", "pre-push", "post-checkout"):
        content = (project / ".githooks" / hook).read_text(encoding="utf-8")
        assert "run-workspace-guard.sh" in content
        assert "exec python " not in content


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        check=True,
        text=True,
    )
    return result.stdout.strip()


def commit(repo: Path, message: str, content: str) -> str:
    (repo / "tracked.txt").write_text(content, encoding="utf-8")
    git(repo, "add", "tracked.txt")
    git(repo, "commit", "-m", message)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repository(tmp_path: Path) -> tuple[Path, str, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.email", "guard@example.test")
    git(repo, "config", "user.name", "Workspace Guard")
    first = commit(repo, "first", "one")
    second = commit(repo, "second", "two")
    git(
        repo,
        "update-ref",
        "refs/remotes/origin/claude/telegram-bot-services-xfAh6",
        second,
    )
    return repo, first, second


def test_clean_stale_worktree_is_fast_forwarded_and_registered(repository):
    path, first, second = repository
    git(path, "checkout", "--detach", first)
    repo = guard.discover_repo(path)

    state = guard.sync_and_register(repo, fetch=False)

    assert git(path, "rev-parse", "HEAD") == second
    assert state["base_sha"] == second
    assert guard.check_registered_workspace(repo)["base_sha"] == second


def test_diverged_history_is_blocked_without_losing_local_commit(repository):
    path, first, _second = repository
    git(path, "checkout", "--detach", first)
    local = commit(path, "local", "local")
    repo = guard.discover_repo(path)

    with pytest.raises(guard.GuardError, match="история разошлась"):
        guard.sync_and_register(repo, fetch=False)

    assert git(path, "rev-parse", "HEAD") == local
    assert not (repo.git_dir / guard.STATE_FILE).exists()


def test_unregistered_workspace_blocks_edits_but_allows_recovery(repository):
    path, _first, _second = repository
    repo = guard.discover_repo(path)

    with pytest.raises(guard.GuardError, match="bootstrap"):
        guard.handle_claude_pretool(
            repo,
            {"tool_name": "Edit", "tool_input": {"file_path": "tracked.txt"}},
        )

    guard.handle_claude_pretool(
        repo,
        {"tool_name": "PowerShell", "tool_input": {"command": "git status"}},
    )


def test_registration_is_invalid_after_checkout_before_base(repository):
    path, first, second = repository
    repo = guard.discover_repo(path)
    guard.register_workspace(repo)
    git(path, "checkout", "--detach", first)

    with pytest.raises(guard.GuardError, match="базу"):
        guard.check_registered_workspace(repo)

    assert second != first


def test_claude_and_git_hooks_keep_guard_enabled():
    project = Path(__file__).resolve().parents[1]
    settings = json.loads((project / ".claude" / "settings.json").read_text())
    hooks = settings["hooks"]

    assert {"SessionStart", "PreToolUse", "WorktreeCreate", "WorktreeRemove"} <= set(
        hooks
    )
    assert "Edit|Write|NotebookEdit|Bash|PowerShell" == hooks["PreToolUse"][0][
        "matcher"
    ]
    for name in ("pre-commit", "pre-push", "post-checkout"):
        text = (project / ".githooks" / name).read_text(encoding="utf-8")
        assert "run-workspace-guard.sh" in text

    launcher = (project / ".githooks" / "run-workspace-guard.sh").read_text(
        encoding="utf-8"
    )
    assert "agent_workspace_guard.py" in launcher


def test_git_common_dir_is_resolved_from_command_cwd(repository):
    path, _first, _second = repository
    nested = path / "nested" / "project"
    nested.mkdir(parents=True)

    repo = guard.discover_repo(nested)

    assert repo.git_dir == repo.common_dir == (path / ".git").resolve()
