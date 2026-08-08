from __future__ import annotations

import re
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_bulk_empty_state_does_not_use_dead_legacy_callbacks() -> None:
    source = (PROJECT_ROOT / "tg-manager/bot/handlers/bulk.py").read_text(
        encoding="utf-8"
    )

    assert 'callback_data="bots_list"' not in source
    assert 'callback_data="bm_main"' not in source
    assert 'BotCb(action="list"' in source
    assert 'BmCb(action="operations"' in source


def test_intent_navigation_uses_registered_targets() -> None:
    source = (PROJECT_ROOT / "tg-manager/bot/handlers/intent_engine.py").read_text(
        encoding="utf-8"
    )

    assert 'BmCb(action="accounts")' not in source
    assert 'BmCb(action="visibility")' not in source
    assert 'AccCb(action="menu")' in source
    assert 'VisCb(action="dashboard")' in source


def test_admin_buttons_are_covered_by_admin_dispatcher() -> None:
    project_files = (PROJECT_ROOT / "tg-manager").rglob("*.py")
    admin_callbacks: set[str] = set()
    for path in project_files:
        source = path.read_text(encoding="utf-8")
        for match in re.finditer(r"callback_data=f?[\"']adm:([^\"']+)[\"']", source):
            admin_callbacks.add(match.group(1))

    dispatcher = (PROJECT_ROOT / "tg-manager/bot/handlers/admin.py").read_text(
        encoding="utf-8"
    )
    exact_actions = set(re.findall(r'action == "([^"]+)"', dispatcher))
    for tuple_body in re.findall(r"action in \(([^)]*)\)", dispatcher):
        exact_actions.update(re.findall(r'"([^"]+)"', tuple_body))
    prefix_actions = set(re.findall(r'action\.startswith\("([^"]+)"\)', dispatcher))

    # Some admin callbacks bypass the generic action-string dispatcher and get
    # their own dedicated @router.callback_query(F.data == "adm:...") handler
    # instead (e.g. the subscription-gate feature). Those are equally valid
    # coverage — count them too.
    exact_actions.update(
        m[len("adm:"):]
        for m in re.findall(r'F\.data == "(adm:[^"]+)"', dispatcher)
    )
    prefix_actions.update(
        m[len("adm:"):]
        for m in re.findall(r'F\.data\.startswith\("(adm:[^"]+)"\)', dispatcher)
    )

    missing: list[str] = []
    for raw_action in sorted(admin_callbacks):
        if "{" in raw_action:
            action = raw_action.split("{", 1)[0]
            if action and action not in prefix_actions:
                missing.append(raw_action)
            continue
        if raw_action in exact_actions:
            continue
        if any(raw_action.startswith(prefix) for prefix in prefix_actions):
            continue
        missing.append(raw_action)

    assert missing == []


def test_botmother_buttons_are_covered_by_botmother_dispatcher() -> None:
    project_files = (PROJECT_ROOT / "tg-manager").rglob("*.py")
    callbacks: set[str] = set()
    for path in project_files:
        source = path.read_text(encoding="utf-8")
        for match in re.finditer(r"BmCb\(action=[\"']([^\"']+)[\"']", source):
            callbacks.add(match.group(1))

    dispatcher = (
        PROJECT_ROOT / "tg-manager/bot/handlers/botmother_menu.py"
    ).read_text(encoding="utf-8")
    handlers = set(
        re.findall(r"BmCb\.filter\(F\.action == [\"']([^\"']+)[\"']", dispatcher)
    )

    assert sorted(callbacks - handlers) == []
