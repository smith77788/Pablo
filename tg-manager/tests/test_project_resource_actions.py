"""Проверка применимости действий и передачи ID в существующие карточки."""
from pathlib import Path
import shutil
import subprocess

import pytest

from tests.miniapp_source import js_function


@pytest.mark.skipif(not shutil.which("node"), reason="нет node")
def test_project_menu_routes_only_applicable_actions():
    actions = js_function("resourceActionsFor") + "\n" + js_function("projectResourceActions")
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
const big = projectResourceActions({id: 7, type: 'channel', object_id: 9007199254740992,
  object_id_text: '9007199254740993'}, trigger, 13);
big.primary[1][1]();
assert.deepEqual(calls.pop(), ['publish', 13, '9007199254740993']);
big.primary[2][1]();
assert.deepEqual(calls.pop(), ['copy', '9007199254740993']);
assert.deepEqual(projectResourceActions({id: 7, type: 'channel', object_id: 52,
  resource_available: false}, trigger, 13).primary, []);
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
    assert "resourceMenuButton()" in rows
    assert "bindResourceActionRows(" in rows
    assert "openProjectResourceActions(" in rows
    assert "Ресурс останется в Telegram" in js_function("removeNetNode")


@pytest.mark.skipif(not shutil.which("node"), reason="нет node")
def test_catalog_menu_preserves_exact_id_and_suppresses_row_opening():
    script = Path("mini_app/screens/resource_actions.js").read_text(encoding="utf-8")
    probe = """
const assert = require('node:assert/strict');
const listeners = {};
const trigger = {};
const row = {dataset:{resourceNode:'9007199254740993'}, querySelector:() => trigger};
const target = {closest: selector => selector === '[data-resource-node]' ? row
  : selector === '[data-resource-menu]' ? trigger : null};
const calls = [];
let timer;
global.setTimeout = callback => { timer = callback; return 1; };
global.clearTimeout = () => { timer = null; };
const root = {contains: element => element === row,
  addEventListener(name, fn, capture) { assert(!listeners[name]); listeners[name] = {fn,capture}; }};
bindResourceActionRows(root, () => assert.fail('старый обработчик'));
bindResourceActionRows(root, (id, button) => calls.push([id, button]));
assert.equal(listeners.click.capture, true);
let stopped = 0;
const event = {target, preventDefault(){}, stopPropagation(){ stopped++; }};
listeners.click.fn(event);
listeners.contextmenu.fn(event);
listeners.keydown.fn({...event,key:'F10',shiftKey:true});
assert.equal(stopped, 1);
assert.deepEqual(calls, Array(3).fill(['9007199254740993',trigger]));
const rowTarget = {closest: selector => selector === '[data-resource-node]' ? row : null};
const touch = {...event, target:rowTarget, isPrimary:true, pointerType:'touch', clientX:0, clientY:0};
listeners.pointerdown.fn(touch);
listeners.pointermove.fn({...touch, clientY:20});
assert.equal(timer, null);
listeners.pointerdown.fn(touch);
timer();
listeners.click.fn({...event, target:rowTarget});
assert.equal(stopped, 2);
assert.equal(calls.length, 4);
listeners.pointerdown.fn(touch);
listeners.pointerup.fn(touch);
listeners.click.fn({...event, target:rowTarget});
assert.equal(stopped, 2);
assert.equal(calls.length, 4);
assert.equal(catalogResourceId({channel_id:9007199254740992}, 'channel'), '');
assert.equal(catalogResourceId({channel_id_text:'9007199254740993'}, 'channel'), row.dataset.resourceNode);
assert.deepEqual(resourceActionsFor({type:'bot',object_id_text:'52',is_active:false}).secondary, []);
assert.equal(resourceActionsFor({type:'channel',object_id_text:'52'}).primary.length, 2);
"""
    subprocess.run(["node", "-"], input=script + "\n" + probe, text=True, check=True)


def test_catalog_rows_bind_the_menu_outside_selection_mode():
    assert "bindCatalogResourceActions" in js_function("renderAccounts")
    assert "resourceMenuButton" in js_function("accRowHtml")
    assert "resourceMenuButton" in js_function("accTableHtml")
    assert "bindCatalogResourceActions" in js_function("renderBots")
    assert "bindCatalogResourceActions" in js_function("renderChannels")
    assert "!CH_SEL_MODE && catalogResourceMenus.channel" in js_function("renderChannels")


@pytest.mark.skipif(not shutil.which("node"), reason="нет node")
def test_add_to_project_submits_only_the_selected_project_and_exact_resource():
    source = js_function("addCatalogResourceToProject")
    probe = """
const assert = require('node:assert/strict');
const calls = [];
let choice = null;
let enabled = true;
const askChoice = async () => choice;
const toast = () => {};
const api = async (path, options) => {
  if (!options) return {resource_menu:enabled,networks:[{id:7,name:'A'},{id:8,name:'B'}]};
  calls.push([path,JSON.parse(options.body)]);
  return {already_linked:false};
};
(async () => {
  const add = () => addCatalogResourceToProject('group','9007199254740993','Группа');
  await add();
  choice = 999;
  await add();
  assert.deepEqual(calls, []);
  choice = 8;
  await add();
  enabled = false;
  await add();
  assert.deepEqual(calls, [['/api/miniapp/networks/8/nodes',
    {type:'group', object_id:'9007199254740993', label:'Группа'}]]);
})().catch(error => {console.error(error);process.exitCode = 1;});
"""
    subprocess.run(["node", "-"], input=source + "\n" + probe, text=True, check=True)
