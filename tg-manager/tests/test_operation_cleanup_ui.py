"""Уборка требует выбрать цели и подтвердить серверный план."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from tests.miniapp_source import js_function, miniapp_html, source_of


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
    assert "canRecover:payload.action === 'disable' && !result.replayed" in source
    assert "item.can_restore === true" in source


def test_context_opens_only_latest_read_and_ignores_stale_menu_responses():
    node = shutil.which("node")
    if not node:
        pytest.skip("нужен node")
    script = """
const assert = require('node:assert/strict');
let latestResourceCleanupRequest = 0;
const trigger = {isConnected:true};
const document = {activeElement:trigger};
const pending = [];
const calls = [];
const toast = text => calls.push(text);
const errRu = error => error.message;
const openOperationCleanup = (...args) => calls.push(args);
const api = path => {
  assert.equal(path, '/api/miniapp/restrictions/dead?summary=1');
  return new Promise((resolve, reject) => pending.push({resolve, reject}));
};
(async () => {
  await openLatestResourceCleanup('account', '51');
  await openLatestResourceCleanup('channel', '../51');
  assert.equal(pending.length, 0);
  const stale = openLatestResourceCleanup('group', '51');
  const current = openLatestResourceCleanup('channel', '9007199254740993', 7);
  pending[0].resolve({cleanup_available:true, op_id:'12'});
  pending[1].resolve({cleanup_available:true, op_id:'13'});
  await Promise.all([stale, current]);
  assert.deepEqual(calls.splice(0), [['13', {kind:'channel', id:'9007199254740993', projectId:7}]]);
  const moved = openLatestResourceCleanup('bot', '51');
  document.activeElement = {};
  pending[2].resolve({cleanup_available:true, op_id:'13'});
  await moved;
  assert.equal(calls.length, 0);
  document.activeElement = trigger;
  const empty = openLatestResourceCleanup('bot', '51');
  pending[3].resolve({op_id:null});
  await empty;
  const unpiloted = openLatestResourceCleanup('bot', '51');
  pending[4].resolve({op_id:'13', cleanup_available:false});
  await unpiloted;
  const failed = openLatestResourceCleanup('bot', '51');
  pending[5].reject(new Error('Временный сбой'));
  await failed;
  assert.deepEqual(calls, ['Пока нет завершённой проверки ресурсов',
    'Очистка по результатам проверки недоступна', 'Временный сбой']);
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    subprocess.run([node, "-"], input=js_function("openLatestResourceCleanup") + "\n" + script,
                   text=True, check=True)


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
  const stale = openRemovedChannels('24');
  pending[3].resolve({channels:[{channel_id_text:'24', can_restore:false,
    reason:'owner', restore_reason:'Привязанный аккаунт больше не доступен'}]});
  await stale;
  assert.match(box.innerHTML, /Привязанный аккаунт больше не доступен/);
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


def test_stopped_publication_card_and_banner_offer_project_instead_of_retry():
    node = shutil.which("node")
    if not node:
        pytest.skip("нужен node")
    script = r"""
const assert = require('node:assert/strict');
const elements = {};
const banner = {classList:{add(){}}, style:{}};
const document = {getElementById: () => banner};
const window = {};
const txt = (id, value) => { elements[id] = value; };
const push = () => {};
const esc = value => String(value).replace(/</g, '&lt;');
const num = String;
const stb = () => ['red', 'Ошибка'];
const opRu = String;
const _opLogQuery = () => '/log';
const _qaButtons = () => '';
const _opNextBtn = () => '';
const hideOpDone = () => {};
const clearTimeout = () => {};
const setTimeout = () => {};
let OPLOG_ID, OPLOG_FILTER, OPLOG_Q, OPLOG_ROWS, _opDoneTimer;
let openedProject;
const openNetworkBuilder = id => { openedProject = id; };
let result = {id: 13, op_type: 'mass_publish', status: 'failed', project_review_required: true,
  project_review_message: '<проверить>', review_project_id: 42};
const api = async path => path === '/log' ? {logs: []} : result;
(async () => {
  await openOpDetail(13);
  const card = elements.opdetBody;
  assert.match(card, /&lt;проверить>/);
  assert.doesNotMatch(card, /retryOp|openOpClone|openMassPub/);
  const action = card.match(/onclick="(openNetworkBuilder\(42\))"/);
  assert.ok(action);
  eval(action[1]);
  assert.equal(openedProject, 42);
  showOpComplete(result);
  assert.doesNotMatch(banner.innerHTML, /retryOp/);
  assert.match(banner.innerHTML, /Открыть детали/);

  result = {...result, review_project_id: undefined, project_review_message: 'Проект недоступен'};
  await openOpDetail(13);
  assert.match(elements.opdetBody, /Проект недоступен/);
  assert.doesNotMatch(elements.opdetBody, /openNetworkBuilder|retryOp|openOpClone/);

  result = {id:13, status:'failed'};
  await openOpDetail(13);
  assert.match(elements.opdetBody, /retryOp/);
  assert.match(elements.opdetBody, /openOpClone/);
  showOpComplete(result);
  assert.match(banner.innerHTML, /retryOp/);
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    # _opEtaSpan/_opEta/_opDuration — оценка «сколько ещё ждать» в карточке
    # операции. Берём НАСТОЯЩИЕ функции, а не заглушки: заглушка здесь
    # означала бы, что стенд перестаёт видеть, что карточка их зовёт.
    source = "\n".join(js_function(name) for name in (
        "opCanRetry", "_opDuration", "_opEta", "_opEtaSpan", "openOpDetail",
        "showOpComplete"))
    subprocess.run([node, "-"], input=source + "\n" + script, text=True, check=True)


def test_cleanup_search_and_paging_keep_exact_selection_on_large_report():
    node = shutil.which("node")
    if not node:
        pytest.skip("нужен node")
    source = Path(__file__).resolve().parents[1] / "mini_app/screens/op_cleanup.js"
    script = r"""
const assert = require('node:assert/strict');
const makeElement = () => ({
  value: '', childNodes: [], dataset: {}, querySelectorAll: () => [],
  focus() { focused = this; }, append() {}, replaceChildren() {}, setAttribute() {},
  click() { this.onclick?.(); },
});
let focused;
const overlay = {...makeElement(), addEventListener() {}, style: {}, isConnected: true, remove() {}};
const body = makeElement();
const notice = makeElement();
const close = makeElement();
const controls = new Map();
const control = name => {
  if (!controls.has(name)) controls.set(name, makeElement());
  return controls.get(name);
};
overlay.querySelector = selector => ({
  '[data-cleanup-body]': body, '[data-cleanup-notice]': notice, '[data-cleanup-close]': close,
})[selector];
let bodyMarkup = '';
Object.defineProperty(body, 'innerHTML', {get: () => bodyMarkup, set(markup) {
  bodyMarkup = markup;
  control('[data-cleanup-apply]').disabled = false;
}});
body.querySelector = name => name === '[data-cleanup-apply]' && !bodyMarkup.includes('data-cleanup-apply')
  ? null : control(name);
control('[data-cleanup-review]').querySelector = control;
control('[data-cleanup-findings]').querySelector = control;
control('[data-cleanup-action]').querySelector = control;
control('[data-cleanup-action]').value = 'detach';
const project = control('[data-cleanup-project]');
project.value = '7';
const document = {querySelector: () => null, createElement: () => overlay, body: {appendChild(){}}};
const esc = String;
const errRu = e => e.message;
let recoveryId;
const openRemovedChannels = id => { recoveryId = id; };
let resumedBotId;
const openBot = id => { resumedBotId = id; };
let openedProject;
const openNetworkBuilder = id => { openedProject = id; };
const firstId = 9007199254740993n;
const ids = Array.from({length:1000}, (_, i) => String(firstId + BigInt(i)));
const findings = ids.map((id, i) => ({id, kind:'channel', title:'Ресурс ' + i,
  status: i % 2 ? 'hidden' : 'restricted', projects: [{id: i === 1 ? 8 : 7, name:'Проект'}],
  active:true, can_disable:true}));
const submissions = [];
let reviewItems;
let replayed = false;
let responseError, reportError;
let reportFindings = findings;
let readQuery = '';
let reportTarget;
const api = async (path, options) => {
  assert.equal(path, '/api/miniapp/operations/42/cleanup' + (options ? '' : readQuery));
  if (!options) {
    if (reportError) throw reportError;
    return {findings:reportFindings, target:reportTarget, max_targets:1000};
  }
  const request = JSON.parse(options.body);
  submissions.push(request);
  if (!request.preview && responseError) throw responseError;
  if (reviewItems) return request.preview ? {items:reviewItems, token:'test'}
    : {items:reviewItems.map(item => ({...item, outcome:item.outcome === 'ready' ? 'applied' : 'skipped'})),
      replayed};
  throw new Error('Синтетическая ошибка предпросмотра');
};
(async () => {
  await openOperationCleanup(42);
  const list = control('[data-cleanup-findings]');
  const page = control('[data-cleanup-page]');
  const next = control('[data-cleanup-next]');
  const search = control('#opCleanupSearch');
  const verdict = control('#opCleanupStatus');
  const selectAll = control('[data-cleanup-select-all]');
  assert.equal((list.innerHTML.match(/data-cleanup-target/g) || []).length, 40);
  assert.equal(page.textContent, 'Страница 1 из 25');
  next.onclick();
  assert.equal(page.textContent, 'Страница 2 из 25');
  assert.match(list.innerHTML, new RegExp(ids[40]));

  search.value = '#' + ids[999];
  search.oninput();
  assert.equal((list.innerHTML.match(/data-cleanup-target/g) || []).length, 1);
  assert.match(list.innerHTML, new RegExp(ids[999]));
  selectAll.onclick();
  search.value = 'РЕСУРС 0';
  search.oninput();
  assert.equal(notice.textContent, 'Выбрано: 1 · вне фильтра: 1');
  assert.doesNotMatch(list.innerHTML, new RegExp(ids[999]));
  selectAll.onclick();
  search.value = ids[1];
  search.oninput();
  assert.equal(selectAll.disabled, true);
  assert.match(list.innerHTML, /Проекты: Проект · Нет связи с выбранным проектом/);
  const selectedOnly = control('[data-cleanup-selected]');
  selectedOnly.onclick();
  assert.equal(search.disabled, true);
  assert.match(list.innerHTML, new RegExp(ids[999]));
  assert.equal((list.innerHTML.match(/data-cleanup-target/g) || []).length, 2);
  list.onchange({target:{matches:() => true, value:'999', checked:false}});
  assert.doesNotMatch(list.innerHTML, new RegExp(ids[999]));
  assert.equal((list.innerHTML.match(/data-cleanup-target/g) || []).length, 1);
  await control('[data-cleanup-preview]').onclick();
  assert.deepEqual(submissions.shift().targets, [{kind:'channel', id:ids[0]}]);
  assert.equal(search.disabled, true);
  selectedOnly.onclick();
  assert.equal(search.disabled, false);
  assert.equal(search.value, ids[1]);
  search.value = ids[999];
  search.oninput();
  selectAll.onclick();
  search.value = ids[1];
  search.oninput();
  await control('[data-cleanup-preview]').onclick();
  assert.deepEqual(submissions[0], {action:'detach', project_id:'7', targets:[
    {kind:'channel', id:ids[0]}, {kind:'channel', id:ids[999]},
  ], preview:true});

  project.value = '8';
  project.onchange();
  assert.equal(control('[data-cleanup-preview]').disabled, true);
  project.value = '7';
  project.onchange();
  search.value = '';
  search.oninput();
  verdict.value = 'hidden';
  verdict.onchange();
  selectAll.onclick();
  assert.equal(notice.textContent, 'Выбрано: 499');
  await control('[data-cleanup-preview]').onclick();
  assert.deepEqual(submissions[1].targets.map(target => target.id),
    ids.filter((id,i) => i % 2 === 1 && i !== 1));

  control('[data-cleanup-action]').value = 'disable';
  control('[data-cleanup-action]').onchange();
  verdict.value = '';
  verdict.onchange();
  selectAll.onclick();
  reviewItems = findings.map((item,i) => ({...item, outcome:i % 2 ? 'ready' : 'skipped',
    reason:i % 2 ? '' : 'Идёт публикация', finding_reason:'Диагностика'}));
  await control('[data-cleanup-preview]').onclick();
  const reviewList = control('[data-cleanup-review-list]');
  const reviewPage = control('[data-cleanup-review-page]');
  const reviewSearch = control('#opCleanupReviewSearch');
  const outcome = control('#opCleanupOutcome');
Object.defineProperty(control('[data-cleanup-review]'), 'innerHTML', {set() {
  outcome.value = '';
  reviewSearch.value = '';
}});
  assert.equal(focused, control('[data-cleanup-summary]'));
  assert.match(body.innerHTML, /500 к применению · 500 пропущено/);
  assert.equal((reviewList.innerHTML.match(/class="row-name"/g) || []).length, 40);
  assert.equal(reviewPage.textContent, 'Страница 1 из 25');
  control('[data-cleanup-review-next]').onclick();
  assert.equal(reviewPage.textContent, 'Страница 2 из 25');
  outcome.value = 'no';
  outcome.onchange();
  assert.equal(reviewPage.textContent, 'Страница 1 из 13');
  assert.match(reviewList.innerHTML, /Ограничен Telegram · Диагностика · Идёт публикация/);
  assert.doesNotMatch(reviewList.innerHTML, /Скрыт из глобального поиска|data-cleanup-recover/);
  reviewSearch.value = '#' + ids[999];
  reviewSearch.oninput();
  assert.equal(reviewList.textContent, 'В этом срезе нет ресурсов.');
  await control('[data-cleanup-apply]').onclick();
  assert.deepEqual(submissions[3], {action:'disable', targets:ids.map(id => ({id, kind:'channel'})), token:'test'});
  assert.equal((reviewList.innerHTML.match(/class="row-name"/g) || []).length, 40);
  assert.match(reviewList.innerHTML, /Пропущено · Идёт публикация/);
  outcome.value = 'yes';
  outcome.onchange();
  assert.doesNotMatch(reviewList.innerHTML, /Идёт публикация/);
  assert.match(reviewList.innerHTML, /Отключён в Infragram · Скрыт из глобального поиска/);
  reviewSearch.value = ids[999];
  reviewSearch.oninput();
  assert.match(reviewList.innerHTML, /data-cleanup-recover="999"/);
  reviewList.onclick({target:{closest:() => ({dataset:{cleanupRecover:'999'}})}});
  assert.equal(recoveryId, ids[999]);

  outcome.value = '';
  reviewSearch.value = '';
  await control('[data-cleanup-preview]').onclick();
  replayed = true;
  await control('[data-cleanup-apply]').onclick();
  assert.match(body.innerHTML, /Сохранённый результат/);
  assert.doesNotMatch(reviewList.innerHTML, /data-cleanup-recover/);
  reviewItems = reviewItems.map(item => ({...item, outcome:'skipped'}));
  await control('[data-cleanup-preview]').onclick();
  assert.equal(body.querySelector('[data-cleanup-apply]'), null);
  assert.match(body.innerHTML, /0 к применению · 1000 пропущено/);

  const missingId = String(firstId + 1000n);
  findings.push({id:missingId, kind:'channel', title:'Старая находка', status:'check_failed',
    reason:'Причина проверки', projects:[], active:false, can_disable:false,
    disable_reason:'Ресурс отсутствует в вашем каталоге'});
  await openOperationCleanup(42);
  search.value = '#' + missingId;
  search.oninput();
  assert.match(list.innerHTML, /data-cleanup-target value="1000" disabled/);
  assert.match(list.innerHTML, /Не удалось проверить · Причина проверки · Ресурс отсутствует в вашем каталоге/);
  assert.equal(selectAll.disabled, true);
  control('[data-cleanup-action]').value = 'detach';
  control('[data-cleanup-action]').onchange();
  assert.match(list.innerHTML, /data-cleanup-target value="1000" disabled/);
  assert.equal(control('[data-cleanup-preview]').disabled, true);

  const pausedBotId = String(firstId + 1001n);
  findings.push({id:pausedBotId, kind:'bot', title:'Бот на паузе', status:'restricted',
    active:false, can_disable:false, can_resume:true, projects:[]});
  await openOperationCleanup(42);
  search.value = '#' + pausedBotId;
  search.oninput();
  assert.match(list.innerHTML, /data-cleanup-target value="1001" disabled/);
  assert.match(list.innerHTML, /data-cleanup-resume="1001"/);
  list.onclick({target:{closest:selector => selector === '[data-cleanup-resume]'
    ? {dataset:{cleanupResume:'1001'}} : null}});
  assert.equal(resumedBotId, pausedBotId);

  findings.push({id:String(firstId + 1002n), kind:'channel', title:'Канал со старой связью', status:'hidden',
    active:true, can_disable:false, projects:[], conflicting_projects:[{id:14, name:'Старая схема'}]});
  await openOperationCleanup(42);
  search.value = 'Канал со старой связью';
  search.oninput();
  assert.match(list.innerHTML, /Открыть проект · Старая схема/);
  const projectLink = id => ({target:{closest:() => ({dataset:{
    cleanupConflictItem:'1002', cleanupConflictProject:String(id),
  }})}});
  list.onclick(projectLink(7));
  assert.equal(openedProject, undefined);
  list.onclick(projectLink(14));
  assert.equal(openedProject, 14);

  const action = control('[data-cleanup-action]');
  action.value = 'detach';
  project.value = '7';
  verdict.value = '';
  search.value = '';
  await openOperationCleanup(42);
  // Одноимённый ресурс другого типа и новый сосед не входят в старый выбор.
  for (const index of [0, 2, 3]) {
    list.onchange({target:{matches:() => true, value:String(index), checked:true}});
  }
  reviewItems = [0, 2, 3].map(index => ({...findings[index], outcome:'ready'}));
  await control('[data-cleanup-preview]').onclick();
  const apply = control('[data-cleanup-apply]');
  const reload = control('[data-cleanup-reload]');
  responseError = new Error('Ответ потерян');
  await apply.onclick();
  assert.equal(apply.hidden, false);
  assert.equal(apply.disabled, false);
  assert.equal(apply.textContent, 'Повторить то же подтверждение');
  responseError = Object.assign(new Error('План изменился'), {status:409});
  await apply.onclick();
  assert.equal(apply.hidden, true);
  assert.equal(apply.disabled, true);
  assert.match(notice.textContent, /нужен новый план/);
  assert.equal(focused, reload);
  assert.deepEqual(submissions.at(-1), submissions.at(-2));

  reportFindings = [
    {...findings[2], projects:[{id:8, name:'Чужая связь'}]},
    {...findings[3], kind:'group'},
    findings[4],
    findings[0],
  ];
  reportError = new Error('Список временно недоступен');
  await reload.onclick();
  assert.equal(focused, control('button'));
  assert.match(notice.textContent, /Список временно недоступен/);
  reportError = null;
  await control('button').onclick();
  assert.equal(project.value, '7');
  assert.match(notice.textContent, /Снято с выбора: 2/);
  assert.equal((list.innerHTML.match(/ checked/g) || []).length, 1);
  assert.equal(focused, control('[data-cleanup-preview]'));
  const countBeforePreview = submissions.length;
  reviewItems = [{...findings[0], outcome:'ready'}];
  await control('[data-cleanup-preview]').onclick();
  assert.equal(submissions.length, countBeforePreview + 1);
  assert.deepEqual(submissions.at(-1), {action:'detach', project_id:'7',
    targets:[{kind:'channel', id:ids[0]}], preview:true});
  readQuery = '?kind=channel&resource_id=' + ids[0];
  reportFindings = [findings[0], findings[2]];
  await openOperationCleanup(42, {kind:'channel', id:ids[0]});
  assert.match(notice.textContent, /Сервер не подтвердил выбранный ресурс/);
  reportTarget = {kind:'channel', id:ids[0]};
  await openOperationCleanup(42, {kind:'channel', id:ids[0], projectId:8});
  assert.equal(project.value, '');
  assert.equal(control('[data-cleanup-preview]').disabled, true);
  assert.doesNotMatch(list.innerHTML, / checked/);
  assert.doesNotMatch(list.innerHTML, new RegExp(ids[2]));
  await openOperationCleanup(42, {kind:'channel', id:ids[0], projectId:7});
  assert.equal(project.value, '7');
  assert.equal(control('[data-cleanup-preview]').disabled, true);
  reportFindings = [];
  await openOperationCleanup(42, {kind:'channel', id:ids[0]});
  assert.match(body.innerHTML, /Этого ресурса нет среди находок последней проверки/);
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    subprocess.run([node, "-"], input=source.read_text() + "\n" + script, text=True, check=True)


def test_pin_post_keeps_channel_id_and_opens_result():
    node = shutil.which("node")
    if not node:
        pytest.skip("нужен node")
    assert "pinLastPost('${escJs(pinChannelId)}')" in js_function("buildChDetail")
    script = r"""
const assert = require('node:assert/strict');
const requests = [];
const results = [];
let confirm = false;
function askConfirm(question) {
  assert.match(question, /#9007199254740993/);
  return confirm;
}
async function api(path) { requests.push(path); return {op_id: 44}; }
function toast() {}
function pollOpResult(id) { results.push(id); }
const tg = null;
(async () => {
  const id = '9007199254740993';
  await pinLastPost(Number(id));
  await pinLastPost('../other');
  await pinLastPost(id);
  assert.deepEqual(requests, []);
  confirm = true;
  await pinLastPost(id);
  assert.deepEqual(requests, ['/api/miniapp/channel/9007199254740993/pin']);
  assert.deepEqual(results, [44]);
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    subprocess.run([node, "-"], input=js_function("pinLastPost") + "\n" + script,
                   text=True, check=True)


def test_saved_cleanup_result_reopens_without_resubmitting_confirmation():
    node = shutil.which("node")
    if not node:
        pytest.skip("нужен node")
    source = Path(__file__).resolve().parents[1] / "mini_app/screens/op_cleanup.js"
    script = r"""
const assert = require('node:assert/strict');
let focused;
function element() {
  const node = {children:[], controls:new Map(), value:'', textContent:'', dataset:{}, style:{},
    focus() { focused = this; }, addEventListener(){}, setAttribute(){},
    append(child) { this.children.push(child); }, remove(){},
    querySelector(key) {
      if (!this.controls.has(key)) this.controls.set(key, element());
      return this.controls.get(key);
    },
    contains(child) { return this.children.includes(child) || this.children.some(n => n.contains(child)); }};
  Object.defineProperty(node, 'innerHTML', { get() { return this.markup || ''; }, set(value) {
    this.markup = value; this.children.length = 0; this.controls.clear();
  }});
  return node;
}
let overlay;
const document = {querySelector:() => null, createElement:element, body:{appendChild(node) {
  overlay = node; overlay.isConnected = true;
}}};
const esc = String;
const errRu = error => error.message;
const calls = [];
const receipt = 'a'.repeat(64);
const path = '/api/miniapp/operations/42/cleanup';
const api = async (url, options) => {
  assert.equal(options, undefined);
  calls.push(url);
  if (url === path) return { findings:[], receipts:[{id:receipt, action:'disable', changed:1, total:2}] };
  assert.equal(url, path + '?receipt=' + receipt);
  if (calls.length === 2) throw new Error('Синтетический обрыв чтения');
  return {replayed:true, items:[
    {kind:'channel', id:'9007199254740993', status:'hidden', outcome:'applied'},
    {kind:'channel', id:'99', status:'check_failed', outcome:'skipped', reason:'Отсутствует в каталоге'},
  ]};
};
(async () => {
  await openOperationCleanup(42);
  const body = overlay.querySelector('[data-cleanup-body]');
  const button = body.children[0].children[1];
  assert.match(button.textContent, /Изменено 1 из 2/);
  await button.onclick();
  assert.equal(focused, button);
  assert.match(overlay.querySelector('[data-cleanup-notice]').textContent, /обрыв/);
  await Promise.all([button.onclick(), button.onclick()]);
  assert.equal(calls.length, 3);
  assert.match(body.innerHTML, /Сохранённый результат/);
  const items = body.querySelector('[data-cleanup-review]').querySelector('[data-cleanup-review-list]').innerHTML;
  assert.match(items, /9007199254740993/);
  assert.match(items, /Отсутствует в каталоге/);
  assert.doesNotMatch(items, /data-cleanup-recover/);
  assert.equal(focused, body.querySelector('[data-cleanup-summary]'));
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    subprocess.run([node, "-"], input=source.read_text(encoding="utf-8") + "\n" + script,
                   text=True, check=True)


def test_cleanup_can_distinguish_http_conflict_from_missing_response():
    node = shutil.which("node")
    if not node:
        pytest.skip("нужен node")
    script = r"""
const assert = require('node:assert/strict');
const TK = 'synthetic';
const FETCH_TIMEOUT_MS = 500;
const BOOT_RETRY_MAX = 1;
const _readRetryDelay = async () => null;
const _serverBooting = async () => false;
const _reAuth = async () => {};
let status = 409;
let dropped = false;
let requests = 0;
const fetchT = async () => {
  requests++;
  if (dropped) throw new Error('Связь прервалась');
  return {ok:false, status, json:async () => ({error:'Изменился план'})};
};
(async () => {
  const request = () => _apiRaw('/api/miniapp/operations/42/cleanup', {method:'POST', body:'{}'});
  await assert.rejects(request, error => error.status === 409 && error.message === 'Изменился план');
  assert.equal(requests, 1);
  status = 401;
  await assert.rejects(request, error => error.status === 401);
  assert.equal(requests, 3);
  dropped = true;
  await assert.rejects(request, error => error.status === undefined && /Связь/.test(error.message));
  assert.equal(requests, 4);
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    source = source_of("_apiRaw")
    start = source.index("async function _apiRaw(")
    end = source.index("// Через сколько мс повторить", start)
    subprocess.run([node, "-"], input=source[start:end] + "\n" + script, text=True, check=True)
