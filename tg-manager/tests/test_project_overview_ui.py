"""Кнопки обзора используют существующие маршруты Mini App."""
import subprocess
from pathlib import Path

from tests.miniapp_source import js_function, miniapp_html


def test_project_overview_opens_exact_targets_and_clears_revoked_data():
    assert 'id="homeProjects" hidden' in miniapp_html()
    assert "loadHomeProjects()" in js_function("loadHome")
    script = Path("mini_app/screens/project_overview.js").read_text(encoding="utf-8")
    harness = r"""
const assert = require('node:assert/strict');
function element(tag) {
  return {tag, children: [], style: {}, hidden: true,
    append(...items) { this.children.push(...items); },
    replaceChildren(...items) { this.children = items; },
    setAttribute() {}};
}
const section = element('section');
const document = {getElementById: () => section, createElement: element};
const actions = [];
const openNetworkBuilder = id => actions.push(['project', id]);
const openOps = id => actions.push(['journal', id]);
const openNetBuilderCreateModal = () => actions.push(['create']);
const openOpDetail = id => actions.push(['operation', id]);
let reply;
const api = () => reply;
"""
    probe = r"""
(async () => {
  const name = '<img src=x onerror=alert(1)>';
  const data = {enabled: true, total: 8, issue_days: 7, projects: [
    {id: 42, name, node_count: 2, unavailable: 1, in_flight: 1,
     failed: 1, issue_id: '9007199254740993'}
  ]};
  reply = Promise.resolve(data);
  await loadHomeProjects();
  const row = section.children[1].children[0];
  assert.equal(row.children[0].textContent, name);
  row.children[0].onclick();
  row.children[2].children.forEach(button => button.onclick());
  assert.deepEqual(actions, [['project', 42], ['operation', '9007199254740993'], ['journal', 42]]);
  let finish;
  reply = new Promise(resolve => { finish = resolve; });
  const old = loadHomeProjects();
  reply = Promise.resolve({enabled: false});
  await loadHomeProjects();
  finish(data);
  await old;
  assert.equal(section.hidden, true);
  assert.deepEqual(section.children, []);
  reply = Promise.resolve(data);
  await loadHomeProjects();
  reply = Promise.reject(Error('offline'));
  await loadHomeProjects();
  assert.equal(section.children[0].textContent, 'Не удалось загрузить проекты');
  assert.equal(section.children[1].textContent, 'Повторить');
  reply = Promise.resolve({...data, total: 0, projects: []});
  await loadHomeProjects();
  section.children[1].children[0].onclick();
  assert.deepEqual(actions.slice(-2), [['project', undefined], ['create']]);
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    result = subprocess.run(["node", "-e", harness + script + probe], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
