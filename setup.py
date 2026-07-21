#!/usr/bin/env python3
"""Install the agent operating-system templates into a target repository.

Zero dependencies (stdlib only). Copies:

    templates/CLAUDE.md              -> <project>/CLAUDE.md
    templates/AGENT_PROTOCOL.md      -> <project>/docs/AGENT_PROTOCOL.md
    templates/AUDIT_LEDGER.md        -> <project>/docs/AUDIT_LEDGER.md
    templates/PRODUCT_DEPTH_PLAN.md  -> <project>/docs/PRODUCT_DEPTH_PLAN.md

Existing files are never overwritten unless --force is given. After install, edit the
<<ANGLE-BRACKET>> placeholders in each file for your project.

Usage:
    python3 setup.py                              # install into the current directory
    python3 setup.py --project /path/to/repo      # install into a specific repo
    python3 setup.py --force                      # overwrite existing files
    python3 setup.py --dry-run                    # show what would happen, do nothing
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TEMPLATES = HERE / "templates"

# (template filename, destination relative to the project root)
MAPPING = [
    ("CLAUDE.md", "CLAUDE.md"),
    ("AGENT_PROTOCOL.md", "docs/AGENT_PROTOCOL.md"),
    ("AUDIT_LEDGER.md", "docs/AUDIT_LEDGER.md"),
    ("PRODUCT_DEPTH_PLAN.md", "docs/PRODUCT_DEPTH_PLAN.md"),
]


def main() -> int:
    ap = argparse.ArgumentParser(description="Install agent OS templates into a repo.")
    ap.add_argument("--project", default=".", help="target repo root (default: cwd)")
    ap.add_argument("--force", action="store_true", help="overwrite existing files")
    ap.add_argument("--dry-run", action="store_true", help="print actions, change nothing")
    args = ap.parse_args()

    project = Path(args.project).resolve()
    if not project.is_dir():
        print(f"error: {project} is not a directory", file=sys.stderr)
        return 2
    if not TEMPLATES.is_dir():
        print(f"error: templates dir not found at {TEMPLATES}", file=sys.stderr)
        return 2

    installed = skipped = 0
    for src_name, dest_rel in MAPPING:
        src = TEMPLATES / src_name
        dest = project / dest_rel
        if not src.is_file():
            print(f"skip: missing template {src}", file=sys.stderr)
            continue
        if dest.exists() and not args.force:
            print(f"skip (exists): {dest_rel}  — use --force to overwrite")
            skipped += 1
            continue
        action = "would write" if args.dry_run else "write"
        print(f"{action}: {dest_rel}")
        if not args.dry_run:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest)
        installed += 1

    print(f"\ndone: {installed} written, {skipped} skipped"
          + (" (dry-run)" if args.dry_run else ""))
    if installed and not args.dry_run:
        print("next: edit the <<ANGLE-BRACKET>> placeholders in the installed files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
