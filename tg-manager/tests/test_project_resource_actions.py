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
unbound.groups[0][1][0][1]();
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
assert.deepEqual(resourceActionsFor({type:'bot',object_id_text:'52',is_owned:false}).secondary, []);
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
const askChoice = async () => choice;
const toast = () => {};
const api = async (path, options) => {
  if (!options) return {resource_menu:false,networks:[{id:7,name:'A'},{id:8,name:'B'}]};
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
  assert.deepEqual(calls, [['/api/miniapp/networks/8/nodes',
    {type:'group', object_id:'9007199254740993', label:'Группа'}]]);
})().catch(error => {console.error(error);process.exitCode = 1;});
"""
    subprocess.run(["node", "-"], input=source + "\n" + probe, text=True, check=True)


@pytest.mark.skipif(not shutil.which("node"), reason="нет node")
def test_card_menu_ignores_a_late_response_for_another_resource():
    source = js_function("prepareResourceCardMenu")
    probe = """
const assert = require('node:assert/strict');
const button = {hidden:false,onclick:() => assert.fail('старый обработчик')};
const document = {getElementById: () => button};
const calls = [];
const openCatalogResourceActions = (...args) => calls.push(args);
const old = prepareResourceCardMenu('menu','channel','44');
const current = prepareResourceCardMenu('menu','channel','9007199254740993');
old({title:'Старый ответ'});
assert.equal(button.hidden, true);
assert.equal(button.onclick, null);
const item = {title:'Канал'};
current(item);
assert.equal(button.hidden, false);
button.onclick();
assert.deepEqual(calls, [[item,'channel','9007199254740993',button,true]]);
"""
    subprocess.run(["node", "-"], input=source + "\n" + probe, text=True, check=True)
    for opener in ("openAccount", "openBot", "openChannel"):
        assert "prepareResourceCardMenu(" in js_function(opener)


@pytest.mark.skipif(not shutil.which("node"), reason="нет node")
def test_catalog_actions_follow_ownership_role_and_exact_id():
    source = "\n".join(js_function(name) for name in ("resourceActionsFor", "openCatalogResourceActions"))
    probe = """
const assert = require('node:assert/strict');
let menu;
const calls = [];
const showResourceActions = (_, actions) => menu = actions;
const openChDestructive = (...args) => calls.push(args);
const copyText = text => calls.push(text);
const find = label => [...menu.primary, ...menu.secondary, ...menu.groups.flatMap(([, items]) => items)]
  .find(([name]) => name === label);
openCatalogResourceActions({title:'Общий',username:'public_bot',is_owned:false,is_active:true}, 'bot', '52', {});
assert.equal(find('Добавить в проект'), undefined);
assert.equal(find('Команды бота'), undefined);
assert.equal(find('Убрать бота из Infragram'), undefined);
find('Копировать ссылку')[1]();
assert.equal(calls.pop(), 'https://t.me/public_bot');
openCatalogResourceActions({title:'Пауза',is_owned:true,is_active:false}, 'bot', '52', {});
assert.ok(find('Вернуть в работу'));
assert.equal(find('Изменить профиль'), undefined);
for (const role of ['member', 'admin', 'owner']) {
  openCatalogResourceActions({title:'Чат',type:'supergroup',acc_id:1,role,username:'evil/path'}, 'channel', '52', {}, true);
  assert.equal(!!find('Покинуть чат или канал'), role === 'member');
  assert.equal(!!find('Удалить канал или чат в Telegram'), role === 'owner');
  assert.equal(!!find('Изменить название или описание'), role !== 'member');
  assert.ok(find('Убрать из Infragram'));
  assert.equal(find('Копировать ссылку'), undefined);
}
find('Удалить канал или чат в Telegram')[1]();
assert.deepEqual(calls.pop(), ['delete',['52']]);
openCatalogResourceActions({title:'Чат',acc_id:1,role:'owner'}, 'channel', '9007199254740993', {}, true);
assert.equal(find('Удалить канал или чат в Telegram'), undefined);
find('Копировать ID')[1]();
assert.equal(calls.pop(), '9007199254740993');
"""
    subprocess.run(["node", "-"], input=source + "\n" + probe, text=True, check=True)


@pytest.mark.skipif(not shutil.which("node"), reason="нет node")
def test_selected_menu_rejects_changed_scope_and_inexact_ids():
    source = js_function("openSelectedResourceActions")
    probe = r"""
const assert = require('node:assert/strict');
const ACC_SEL = new Set(), BOT_SEL = new Set([17, 23]), CH_SEL = new Set();
const ACC_SEL_ALL_FILTERED = true;
let ACC_FILTERED_TOTAL = 1200, filter = 'first', title, menu;
const calls = [];
const _massSel = () => ({select_all_filtered:true, filter});
const showResourceActions = (value, actions) => { title = value; menu = actions; };
const openBotBroadcast = () => calls.push('broadcast');
const openBotProfileEdit = () => {};
const bulkDeleteBots = () => {};
const openMassProfile = () => {}, openMassStage = () => {}, runAccPool = () => {};
const runAccMass = op => calls.push(op);
const toast = message => calls.push(message);
const copyText = text => calls.push(text);
openSelectedResourceActions('bot', {});
assert.equal(title, 'Выбрано: 2');
menu.primary[0][1]();
assert.equal(calls.pop(), '17\n23');
const broadcast = menu.groups[0][1][0][1];
broadcast();
assert.equal(calls.pop(), 'broadcast');
BOT_SEL.delete(17);
BOT_SEL.add(31);
broadcast();
assert.match(calls.pop(), /Выбор изменился/);
openSelectedResourceActions('account', {});
assert.equal(title, 'Весь срез: 1200');
assert.deepEqual(menu.primary.map(([label]) => label), ['Проверить аккаунты']);
const check = menu.primary[0][1];
check();
assert.equal(calls.pop(), 'check');
filter = 'second';
check();
assert.match(calls.pop(), /Выбор изменился/);
CH_SEL.add(9007199254740992);
openSelectedResourceActions('channel', {});
assert.match(calls.pop(), /нет точного ID/);
CH_SEL.clear();
openSelectedResourceActions('channel', {});
assert.match(calls.pop(), /Сначала отметьте/);
"""
    subprocess.run(["node", "-"], input=source + "\n" + probe, text=True, check=True)


@pytest.mark.skipif(not shutil.which("node"), reason="нет node")
def test_mass_post_keeps_the_targets_shown_in_confirmation():
    source = "\n".join(js_function(name) for name in ("openChMass", "submitChMass"))
    probe = r"""
const assert = require('node:assert/strict');
const CH_SEL = new Set([41, 42]);
let CHM_OP, CHM_TARGETS;
const fields = Object.fromEntries(['chmTitle', 'chmHint', 'chmValue', 'chmErr', 'chmBtn']
  .map(id => [id, {}]));
const document = {getElementById: id => fields[id]};
const showModal = () => {}, closeModal = () => {}, toggleChSelMode = () => {};
const toast = () => {}, plural = () => 'каналах';
let accept = false, requests = [];
const askConfirm = async message => {
  assert.match(message, /в 2 каналах/);
  CH_SEL.add(100);
  return accept;
};
const api = async (path, options) => {
  requests.push([path, JSON.parse(options.body)]);
  return {count:2};
};
(async () => {
  openChMass('post');
  fields.chmValue.value = 'Синтетический пост';
  CH_SEL.delete(41);
  await submitChMass();
  assert.deepEqual(requests, []);
  accept = true;
  await submitChMass();
  assert.deepEqual(requests, [['/api/miniapp/channels/mass',
    {op:'post', channel_ids:[41, 42], text:'Синтетический пост'}]]);
})().catch(error => {console.error(error); process.exitCode = 1;});
"""
    subprocess.run(["node", "-"], input=source + "\n" + probe, text=True, check=True)


@pytest.mark.skipif(not shutil.which("node"), reason="нет node")
def test_resource_menu_search_reveals_grouped_commands_and_runs_one_action():
    source = js_function("showResourceActions")
    probe = """
const assert = require('node:assert/strict');
const make = (tag = '') => ({
  tag, children: [], listeners: {}, classList: {toggle() {}}, hidden: false, open: false,
  append(...items) { this.children.push(...items); },
  replaceChildren(...items) { this.children = items; },
  querySelector(name) { return this.children.find(item => item.tag === name); },
  querySelectorAll(name) { return this.children.filter(item => item.tag === name); },
  addEventListener(name, handler) { this.listeners[name] = handler; },
  close() { this.open = false; this.listeners.close?.(); }, showModal() { this.open = true; },
});
const elements = {};
const document = {createElement: make, getElementById: id => elements[id] ??= make()};
let calls = 0;
const run = () => calls++;
const quick = ['Открыть карточку', 'Копировать ID', 'Копировать ссылку'].map(label => [label, run]);
const group = ['Снять тег', 'Поставить тег', 'Изменить статус', 'В пул'].map(label => [label, run]);
showResourceActions('<img>', {primary:quick, secondary:[], groups:[['Организация', group]]});
const search = elements.resourceActionsSearch;
const more = elements.resourceActionsMore;
assert.equal(elements.resourceActionsTitle.textContent, '<img>');
assert.equal(search.hidden, false);
assert.equal(more.open, false);
search.value = '  СНЯТЬ ';
search.oninput();
assert.equal(more.open, true);
assert.equal(elements.resourceActionsPrimary.hidden, true);
const buttons = elements.resourceActionsSecondary.children[0].querySelectorAll('button');
assert.deepEqual(buttons.filter(button => !button.hidden).map(button => button.textContent), ['Снять тег']);
search.value = 'Нет совпадений';
search.oninput();
assert.equal(elements.resourceActionsEmpty.hidden, false);
search.value = '';
search.oninput();
assert.equal(more.open, false);
assert.equal(elements.resourceActionsEmpty.hidden, true);
elements.resourceActionsPrimary.children[0].listeners.click();
assert.equal(elements.resourceActions.open, false);
assert.equal(calls, 1);
"""
    subprocess.run(["node", "-"], input=source + "\n" + probe, text=True, check=True)
