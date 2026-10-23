#!/usr/bin/env python3
"""Fail-closed workspace guard for Codex, Claude Code, and Git hooks."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_CANONICAL_REF = "origin/claude/telegram-bot-services-xfAh6"
STATE_FILE = "infragram-agent-base.json"
MANAGED_WORKTREE_MARKERS = ("/.codex/worktrees/", "/.claude/worktrees/")


class GuardError(RuntimeError):
    """A workspace is unsafe for agent writes."""


@dataclass(frozen=True)
class Repo:
    worktree: Path
    top_level: Path
    git_dir: Path
    common_dir: Path
    canonical_ref: str


def _run_git(
    cwd: Path,
    *args: str,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        check=False,
        encoding="utf-8",
        errors="replace",
        env=env,
        text=True,
    )
    if check and result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or "git завершился с ошибкой"
        raise GuardError(f"git {' '.join(args)}: {detail}")
    return result


def _resolve_git_path(command_cwd: Path, value: str) -> Path:
    path = Path(value.strip())
    return path.resolve() if path.is_absolute() else (command_cwd / path).resolve()


def discover_repo(worktree: Path, canonical_ref: str | None = None) -> Repo:
    worktree = worktree.resolve()
    top_level_raw = _run_git(worktree, "rev-parse", "--show-toplevel").stdout
    top_level = Path(top_level_raw.strip()).resolve()
    git_dir = _resolve_git_path(
        worktree, _run_git(worktree, "rev-parse", "--git-dir").stdout
    )
    common_dir = _resolve_git_path(
        worktree, _run_git(worktree, "rev-parse", "--git-common-dir").stdout
    )
    return Repo(
        worktree=worktree,
        top_level=top_level,
        git_dir=git_dir,
        common_dir=common_dir,
        canonical_ref=canonical_ref
        or os.environ.get("INFRAGRAM_AGENT_BASE_REF", DEFAULT_CANONICAL_REF),
    )


def _rev_parse(repo: Repo, ref: str) -> str:
    result = _run_git(repo.worktree, "rev-parse", "--verify", f"{ref}^{{commit}}", check=False)
    if result.returncode:
        raise GuardError(
            f"не найден канонический ref {ref!r}; сначала выполните bootstrap с fetch"
        )
    return result.stdout.strip()


def _canonical_remote(repo: Repo) -> tuple[str, str]:
    if "/" not in repo.canonical_ref:
        raise GuardError(
            "канонический ref должен иметь вид <remote>/<branch>, например "
            f"{DEFAULT_CANONICAL_REF}"
        )
    return repo.canonical_ref.split("/", 1)


def fetch_canonical(repo: Repo) -> None:
    remote, branch = _canonical_remote(repo)
    destination = f"refs/remotes/{remote}/{branch}"
    _run_git(
        repo.worktree,
        "fetch",
        "--no-tags",
        remote,
        f"refs/heads/{branch}:{destination}",
    )


def _is_dirty(repo: Repo) -> bool:
    return bool(
        _run_git(
            repo.worktree,
            "status",
            "--porcelain",
            "--untracked-files=normal",
        ).stdout.strip()
    )


def _ahead_behind(repo: Repo, left: str, right: str) -> tuple[int, int]:
    raw = _run_git(
        repo.worktree, "rev-list", "--left-right", "--count", f"{left}...{right}"
    ).stdout.split()
    if len(raw) != 2:
        raise GuardError("git вернул некорректный ahead/behind результат")
    return int(raw[0]), int(raw[1])


def _is_ancestor(repo: Repo, ancestor: str, descendant: str = "HEAD") -> bool:
    result = _run_git(
        repo.worktree,
        "merge-base",
        "--is-ancestor",
        ancestor,
        descendant,
        check=False,
    )
    if result.returncode not in (0, 1):
        raise GuardError(result.stderr.strip() or "не удалось сравнить историю Git")
    return result.returncode == 0


def _state_path(repo: Repo) -> Path:
    return repo.git_dir / STATE_FILE


def invalidate_registration(repo: Repo) -> None:
    _state_path(repo).unlink(missing_ok=True)


def register_workspace(repo: Repo) -> dict[str, str]:
    canonical_sha = _rev_parse(repo, repo.canonical_ref)
    head_sha = _rev_parse(repo, "HEAD")
    if not _is_ancestor(repo, canonical_sha):
        raise GuardError(
            f"HEAD {head_sha[:12]} не содержит каноническую базу "
            f"{canonical_sha[:12]} ({repo.canonical_ref})"
        )
    state = {
        "canonical_ref": repo.canonical_ref,
        "base_sha": canonical_sha,
        "registered_head": head_sha,
        "registered_at": datetime.now(UTC).isoformat(),
    }
    path = _state_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=True, indent=2), encoding="utf-8")
    temporary.replace(path)
    return state


def check_registered_workspace(repo: Repo) -> dict[str, str]:
    path = _state_path(repo)
    if not path.is_file():
        raise GuardError(
            "workspace не прошёл обязательный bootstrap; выполните "
            "python deploy/scripts/agent_workspace_guard.py --bootstrap"
        )
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise GuardError(f"повреждена регистрация workspace: {exc}") from exc
    base_sha = str(state.get("base_sha", ""))
    if not re.fullmatch(r"[0-9a-fA-F]{40,64}", base_sha):
        raise GuardError("регистрация workspace не содержит корректный base_sha")
    if state.get("canonical_ref") != repo.canonical_ref:
        raise GuardError("канонический ref изменился после bootstrap; повторите bootstrap")
    if not _is_ancestor(repo, base_sha):
        raise GuardError(
            f"текущий HEAD больше не содержит зарегистрированную базу {base_sha[:12]}"
        )
    return state


def sync_and_register(repo: Repo, *, fetch: bool) -> dict[str, str]:
    invalidate_registration(repo)
    if fetch:
        fetch_canonical(repo)
    _rev_parse(repo, repo.canonical_ref)
    ahead, behind = _ahead_behind(repo, "HEAD", repo.canonical_ref)
    if behind:
        if ahead:
            raise GuardError(
                f"история разошлась: локально +{ahead}, канон +{behind}; "
                f"сохраните изменения и выполните rebase на {repo.canonical_ref}"
            )
        if _is_dirty(repo):
            raise GuardError(
                f"workspace отстаёт на {behind} коммит(ов) и содержит изменения; "
                "автоматическое выравнивание запрещено"
            )
        branch = _run_git(
            repo.worktree, "symbolic-ref", "--quiet", "--short", "HEAD", check=False
        ).stdout.strip()
        if branch:
            _run_git(repo.worktree, "merge", "--ff-only", repo.canonical_ref)
        else:
            env = dict(os.environ)
            env["INFRAGRAM_GUARD_SYNCING"] = "1"
            _run_git(
                repo.worktree,
                "checkout",
                "--detach",
                repo.canonical_ref,
                env=env,
            )
    return register_workspace(repo)


def require_current_canonical(repo: Repo, *, fetch: bool) -> None:
    if fetch:
        fetch_canonical(repo)
    canonical_sha = _rev_parse(repo, repo.canonical_ref)
    if not _is_ancestor(repo, canonical_sha):
        ahead, behind = _ahead_behind(repo, "HEAD", repo.canonical_ref)
        raise GuardError(
            f"push запрещён: HEAD не содержит {repo.canonical_ref} "
            f"({canonical_sha[:12]}), локально +{ahead}, канон +{behind}; "
            "сначала rebase"
        )


def install_git_hooks(repo: Repo) -> Path:
    if repo.git_dir != repo.common_dir:
        raise GuardError("hooks устанавливаются только из основной рабочей копии")
    project_dir = Path(__file__).resolve().parents[2]
    hooks_dir = project_dir / ".githooks"
    if not hooks_dir.is_dir():
        raise GuardError(f"не найдена директория hooks: {hooks_dir}")
    _run_git(repo.worktree, "config", "core.hooksPath", str(hooks_dir))
    return hooks_dir


def _managed_worktree(path: Path) -> bool:
    normalized = f"/{path.resolve().as_posix().lower().strip('/')}/"
    custom = os.environ.get("INFRAGRAM_MANAGED_WORKTREE_ROOTS", "")
    markers = MANAGED_WORKTREE_MARKERS + tuple(
        f"/{Path(item).as_posix().lower().strip('/')}/"
        for item in custom.split(os.pathsep)
        if item.strip()
    )
    return any(marker in normalized for marker in markers)


def handle_post_checkout(repo: Repo) -> None:
    if os.environ.get("INFRAGRAM_GUARD_SYNCING") == "1":
        return
    if repo.git_dir == repo.common_dir or not _managed_worktree(repo.top_level):
        return
    sync_and_register(repo, fetch=False)


def audit_worktrees(repo: Repo, expected_base: str | None) -> list[str]:
    expected = _rev_parse(repo, expected_base or repo.canonical_ref)
    raw = _run_git(repo.worktree, "worktree", "list", "--porcelain").stdout
    failures: list[str] = []
    for block in raw.strip().split("\n\n"):
        fields = dict(
            line.split(" ", 1)
            for line in block.splitlines()
            if " " in line and line.split(" ", 1)[0] in {"worktree", "HEAD"}
        )
        path = fields.get("worktree")
        head = fields.get("HEAD")
        if not path or not head or not _managed_worktree(Path(path)):
            continue
        child = discover_repo(Path(path), repo.canonical_ref)
        if not _is_ancestor(child, expected):
            failures.append(f"{path}: HEAD {head[:12]} не содержит базу {expected[:12]}")
    return failures


def _safe_slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", value).strip(".-")
    return (slug or "agent")[:48]


def create_claude_worktree(payload: dict[str, Any], canonical_ref: str | None) -> Path:
    source = discover_repo(Path(str(payload.get("cwd") or Path.cwd())), canonical_ref)
    fetch_canonical(source)
    canonical_sha = _rev_parse(source, source.canonical_ref)
    session = _safe_slug(str(payload.get("session_id", "session")))[:8]
    name = _safe_slug(str(payload.get("name", "agent")))
    repository = _safe_slug(source.top_level.name)
    root = Path(
        os.environ.get(
            "INFRAGRAM_CLAUDE_WORKTREE_ROOT",
            str(Path.home() / ".claude" / "worktrees"),
        )
    ).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    target = (root / f"{repository}-{name}-{session}").resolve()
    if target.exists():
        raise GuardError(f"директория worktree уже существует: {target}")
    _run_git(source.worktree, "worktree", "add", "--detach", str(target), canonical_sha)
    child = discover_repo(target, source.canonical_ref)
    register_workspace(child)
    relative_cwd = source.worktree.resolve().relative_to(source.top_level)
    result = (target / relative_cwd).resolve()
    if not result.is_dir():
        raise GuardError(f"в новом worktree отсутствует рабочая директория: {result}")
    return result


def remove_claude_worktree(payload: dict[str, Any], canonical_ref: str | None) -> None:
    raw_path = payload.get("worktree_path")
    if not raw_path:
        raise GuardError("WorktreeRemove не передал worktree_path")
    requested = Path(str(raw_path)).resolve()
    child = discover_repo(requested, canonical_ref)
    allowed_root = Path(
        os.environ.get(
            "INFRAGRAM_CLAUDE_WORKTREE_ROOT",
            str(Path.home() / ".claude" / "worktrees"),
        )
    ).expanduser().resolve()
    try:
        child.top_level.relative_to(allowed_root)
    except ValueError as exc:
        raise GuardError(f"отказ удаления вне {allowed_root}: {child.top_level}") from exc
    if _is_dirty(child):
        raise GuardError("worktree содержит незакоммиченные изменения; удаление запрещено")
    require_current_canonical(child, fetch=True)
    unique = int(
        _run_git(
            child.worktree,
            "rev-list",
            "--count",
            f"{child.canonical_ref}..HEAD",
        ).stdout.strip()
    )
    if unique:
        raise GuardError(f"worktree содержит {unique} невлитых коммит(ов); удаление запрещено")
    _run_git(child.worktree, "worktree", "remove", str(child.top_level))


def _read_hook_payload() -> dict[str, Any]:
    try:
        value = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError) as exc:
        raise GuardError(f"hook получил некорректный JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise GuardError("hook payload должен быть JSON-объектом")
    return value


def _is_recovery_shell_command(payload: dict[str, Any]) -> bool:
    command = str(payload.get("tool_input", {}).get("command", "")).lower()
    if "agent_workspace_guard.py" in command:
        return True
    allowed = re.compile(
        r"^\s*git(?:\.exe)?\s+(status|fetch|rebase|merge|switch|checkout|stash|"
        r"rev-parse|branch|log|diff|worktree|config)\b"
    )
    segments = [part.strip() for part in re.split(r"&&|\|\||;|\r?\n", command)]
    return bool(segments) and all(allowed.match(part) for part in segments if part)


def handle_claude_pretool(repo: Repo, payload: dict[str, Any]) -> None:
    try:
        check_registered_workspace(repo)
    except GuardError:
        if payload.get("tool_name") in {"Bash", "PowerShell"} and _is_recovery_shell_command(
            payload
        ):
            return
        raise


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worktree", type=Path, default=Path.cwd())
    parser.add_argument("--canonical-ref")
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--bootstrap", action="store_true")
    actions.add_argument("--check", action="store_true")
    actions.add_argument("--pre-commit", action="store_true")
    actions.add_argument("--pre-push", action="store_true")
    actions.add_argument("--post-checkout", action="store_true")
    actions.add_argument("--audit-worktrees", action="store_true")
    actions.add_argument("--install-hooks", action="store_true")
    actions.add_argument("--claude-session", action="store_true")
    actions.add_argument("--claude-pretool", action="store_true")
    actions.add_argument("--claude-worktree-create", action="store_true")
    actions.add_argument("--claude-worktree-remove", action="store_true")
    parser.add_argument("--fetch", action="store_true")
    parser.add_argument("--expect-base")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.claude_worktree_create:
            target = create_claude_worktree(_read_hook_payload(), args.canonical_ref)
            print(target)
            return 0
        if args.claude_worktree_remove:
            remove_claude_worktree(_read_hook_payload(), args.canonical_ref)
            return 0

        repo = discover_repo(args.worktree, args.canonical_ref)
        if args.bootstrap:
            state = sync_and_register(repo, fetch=True)
            print(f"OK: workspace зарегистрирован на базе {state['base_sha'][:12]}")
        elif args.check or args.pre_commit:
            state = check_registered_workspace(repo)
            print(f"OK: зарегистрированная база {state['base_sha'][:12]}")
        elif args.pre_push:
            require_current_canonical(repo, fetch=True)
            print(f"OK: HEAD содержит свежий {repo.canonical_ref}")
        elif args.post_checkout:
            handle_post_checkout(repo)
        elif args.audit_worktrees:
            failures = audit_worktrees(repo, args.expect_base)
            if failures:
                raise GuardError("небезопасные agent worktree:\n" + "\n".join(failures))
            print("OK: все управляемые agent worktree содержат ожидаемую базу")
        elif args.install_hooks:
            hooks = install_git_hooks(repo)
            print(f"OK: Git hooks установлены из {hooks}")
        elif args.claude_session:
            try:
                state = sync_and_register(repo, fetch=True)
            except GuardError as exc:
                print(
                    "WORKSPACE ЗАБЛОКИРОВАН: "
                    f"{exc}. Не изменяйте файлы; устраните расхождение и повторите --bootstrap."
                )
            else:
                print(
                    "Workspace проверен: разрешена работа от канонической базы "
                    f"{state['base_sha'][:12]}."
                )
        elif args.claude_pretool:
            handle_claude_pretool(repo, _read_hook_payload())
        return 0
    except GuardError as exc:
        print(f"WORKSPACE GUARD: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
