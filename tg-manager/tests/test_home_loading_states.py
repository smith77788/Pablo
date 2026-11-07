"""Главная не подменяет сбой обновления нулями или ложным успехом."""
import json
import shutil
import subprocess

from tests.miniapp_source import js_function, miniapp_html


def test_refresh_failure_keeps_last_successful_data():
    node = shutil.which('node')
    assert node, 'Для проверки поведения нужен Node.js'
    source = js_function('loadHome')
    probe = r'''
const rendered = [], states = [];
let fail = false;
const document = {getElementById: () => ({innerHTML:''})};
const modernizeIcons = () => {}, getCached = () => null, setCache = () => {};
const renderCards = d => rendered.push(d), renderAct = () => {};
const homeLoadState = (...a) => states.push(a);
const loadOnboarding = () => {}, loadOrganismPulse = () => {};
const renderFrequent = () => {};
const api = async () => {if(fail) throw Error('offline'); return {bots:0};};
const errRu = () => 'Нет связи', errHtml = () => 'error';
'''
    probe += source
    probe += r'''
(async () => {
  await loadHome(); fail = true; await loadHome();
  console.log(JSON.stringify({rendered,states,pending:loadHome.pending}));
})().catch(e => {console.error(e);process.exit(1);});
'''
    result = subprocess.run(
        [node, '-e', probe], capture_output=True, text=True, encoding='utf-8',
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data['rendered'] == [{'bots': 0}]
    assert data['pending'] is False
    assert data['states'][-1][2] is True
    assert 'устаревшими' in data['states'][-1][1]


def test_maintenance_only_lives_in_health():
    html = miniapp_html()
    home = html.split('id="s-home"', 1)[1].split('BOTS LIST', 1)[0]
    health = html.split('id="s-health"', 1)[1].split('id="s-shield"', 1)[0]
    assert 'id="fleetCleanBtn"' not in home
    assert 'id="fleetCleanBtn"' in health
    cleanup = js_function('fleetCleanupAll')
    assert 'preview:true' in cleanup and 'askConfirm(' in cleanup
    assert cleanup.index('askConfirm(') < cleanup.index('preview:false')


def test_refresh_does_not_claim_success_or_reset_scroll():
    assert "loadHome();toast('🔄 Обновлено')" not in miniapp_html()
    assert 'scrollTop' not in js_function('loadHome')
    assert 'd.queue_backlog||0' not in js_function('renderCards')
    assert 'd.ops_failed||0' not in js_function('renderCards')
