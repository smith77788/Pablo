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
    catalog = js_function("openRestrictionCleanup")
    assert "openOperationCleanup(String(report.op_id))" in catalog
    shield = js_function("loadDeadEntities")
    assert "openOpDetail(String(result.op_id))" in shield
    assert "openOperationCleanup(String(result.op_id))" in shield
    assert "/restrictions/remove_dead" not in shield


def test_cleanup_recovery_uses_existing_screens_and_exact_bot_id():
    source = js_function("openOperationCleanup")
    assert "openRemovedChannels()" in source
    assert "openRemovedChannels(String(item.id))" in source
    assert "openRemovedChannels(select.value)" in source
    assert "openBot(String(item.id))" in source
    assert "appendRecovery(body, applied.filter(item => item.outcome === 'applied'))" in source
    assert "item.can_restore === true" in source


def test_targeted_recovery_keeps_exact_id_and_discards_late_list():
    node = shutil.which("node")
    if not node:
        pytest.skip("нужен node")
    script = """
const assert = require('node:assert/strict');
const box = {};
const pending = [];
const document = {getElementById: () => box};
const showModal = () => {};
const empty = (...parts) => parts.join(' ');
const esc = String;
const api = path => new Promise(resolve => pending.push({path, resolve}));
(async () => {
  const first = openRemovedChannels();
  const second = openRemovedChannels('9007199254740993');
  assert.equal(pending[1].path, '/api/miniapp/channels/removed?channel_id=9007199254740993');
  pending[1].resolve({channels:[{channel_id_text:'9007199254740993', title:'Цель'}]});
  await second;
  assert.match(box.innerHTML, /data-restore-channel="9007199254740993"/);
  pending[0].resolve({channels:[]});
  await first;
  assert.match(box.innerHTML, /Цель/);
  const unavailable = openRemovedChannels('23');
  pending[2].resolve({channels:[{channel_id_text:'23', can_restore:false, reason:'gone'}]});
  await unavailable;
  assert.match(box.innerHTML, /Недоступен в Telegram/);
  assert.match(box.innerHTML, /Нет сохранённой привязки к аккаунту/);
  assert.doesNotMatch(box.innerHTML, /data-restore-channel=/);
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    subprocess.run([node, "-"], input=js_function("openRemovedChannels") + "\n" + script,
                   text=True, check=True)


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
