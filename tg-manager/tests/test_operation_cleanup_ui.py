"""Уборка требует выбрать цели и подтвердить серверный план."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from tests.miniapp_source import js_function, miniapp_html


def test_cleanup_opens_from_the_operation_card_and_shield_links_to_the_card():
    assert 'src="screens/op_cleanup.js"' in miniapp_html()
    details = js_function("openOpDetail")
    assert "o.cleanup_available" in details and "openOperationCleanup" in details
    shield = js_function("loadDeadEntities")
    assert "openOpDetail(String(result.op_id))" in shield
    assert "/restrictions/remove_dead" not in shield


def test_cleanup_recovery_uses_existing_screens_and_exact_bot_id():
    source = js_function("openOperationCleanup")
    assert "openRemovedChannels()" in source
    assert "openBot(String(item.id))" in source
    assert "appendRecovery(body, applied.filter(item => item.outcome === 'applied'))" in source
    assert "item.active === false" in source


def test_cleanup_confirms_exact_targets_in_chromium():
    node = shutil.which("node")
    if not node:
        pytest.skip("нужен node")
    script = Path(__file__).resolve().parents[1] / "deploy/scripts/check_operation_cleanup.mjs"
    run = subprocess.run([node, str(script)], check=False, capture_output=True, text=True, timeout=45)
    missing_browser = (
        "Executable doesn't exist" in run.stderr
        or "ERR_MODULE_NOT_FOUND" in run.stderr
        or "process_singleton_posix.cc" in run.stderr
        and "socket() failed: Operation not permitted" in run.stderr
    )
    if run.returncode and missing_browser:
        pytest.skip("Chromium или Playwright недоступен в окружении")
    assert run.returncode == 0, run.stderr
