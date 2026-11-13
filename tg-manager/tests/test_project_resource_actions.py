"""Проверка применимости действий и передачи ID в существующие карточки."""
from pathlib import Path
import shutil
import subprocess

import pytest

from tests.miniapp_source import js_function


@pytest.mark.skipif(not shutil.which("node"), reason="нет node")
def test_project_menu_routes_only_applicable_actions():
    actions = js_function("projectResourceActions")
    check = """
const assert = require('node:assert/strict');
const calls = [];
const openAccount = id => calls.push(['account', id]);
const openBot = id => calls.push(['bot', id]);
const openChannel = id => calls.push(['channel', id]);
const openMassPub = (projectId, id) => calls.push(['publish', projectId, id]);
const openAccountDialogs = id => calls.push(['dialogs', id]);
const openBotCommands = id => calls.push(['commands', id]);
const copyText = value => calls.push(['copy', value]);
const removeNetNode = (id, trigger) => calls.push(['remove', id, trigger]);
const trigger = {};
for (const type of ['account', 'bot', 'channel', 'group', 'chat']) {
  const menu = projectResourceActions({id: 7, type, object_id: 52}, trigger);
  menu.primary[0][1]();
  assert.deepEqual(calls.pop(), [['group', 'chat'].includes(type) ? 'channel' : type, 52]);
  menu.primary[1][1]();
  assert.deepEqual(calls.pop(), ['copy', '52']);
}
for (const [type, action] of [['account', 'dialogs'], ['bot', 'commands']]) {
  projectResourceActions({id: 7, type, object_id: 52}, trigger).secondary[0][1]();
  assert.deepEqual(calls.pop(), [action, 52]);
}
const channel = projectResourceActions({id: 7, type: 'channel', object_id: 52}, trigger, 13);
channel.primary[1][1]();
assert.deepEqual(calls.pop(), ['publish', 13, 52]);
assert.equal(projectResourceActions({id: 7, type: 'group', object_id: 52}, trigger, 13).primary.length, 2);
const unbound = projectResourceActions({id: 7, type: 'channel', object_id: null}, trigger);
assert.deepEqual(unbound.primary, []);
unbound.secondary[0][1]();
assert.deepEqual(calls.pop(), ['remove', 7, trigger]);
assert.equal(projectResourceActions({type: 'constructor', object_id: 52}).primary.length, 1);
"""
    subprocess.run(["node", "-"], input=actions + "\n" + check, text=True, check=True)


def test_project_list_binds_context_menu():
    html = (Path(__file__).resolve().parents[1] / "mini_app/index.html").read_text()
    assert 'src="screens/resource_actions.js"' in html
    rows = js_function("renderNetNodesEdges")
    assert "data-resource-menu" in rows
    assert "bindResourceActionRows(" in rows
    assert "openProjectResourceActions(" in rows
    assert "Ресурс останется в Telegram" in js_function("removeNetNode")
