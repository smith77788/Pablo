"""Состояние экрана VA: гонки запросов и несохранённые изменения."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node") or shutil.which("node.exe")


def test_ui_loads_state_controller_before_screen():
    html = (ROOT / "mini_app" / "index.html").read_text(encoding="utf-8")
    workspace = html.index('src="screens/va_workspace.js"')
    screen = html.index('src="screens/va_admin.js"')
    assert workspace < screen


def test_channel_list_is_filterable_and_bounded():
    js = (ROOT / "mini_app" / "screens" / "va_admin.js").read_text(encoding="utf-8")
    assert "function vaFilterChannels()" in js
    assert "function vaChannelPage(delta)" in js
    assert "const size = 30" in js
    assert "toLocaleLowerCase('ru')" in js


@pytest.mark.skipif(not NODE, reason="Для проверки поведения состояния нужен Node.js")
def test_state_controller_preserves_drafts_and_rejects_stale_responses():
    script = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(process.argv[1], 'utf8') + '\nglobalThis.VaWorkspace = VaWorkspace;';
const box = {Map, Set, Object};
vm.runInNewContext(source, box);
const state = new box.VaWorkspace();
const initial = state.mount('channel:1', {project: 'server', enabled: false});
if (initial.project !== 'server') throw new Error('initial form value missing');
state.track('channel:1', {project: 'local edit', enabled: true});
const restored = state.mount('channel:1', {project: 'server', enabled: false});
if (restored.project !== 'local edit' || restored.enabled !== true || !state.dirty('channel:1'))
  throw new Error('unsaved form values were lost');
state.acknowledge('channel:1', {project: 'local edit', enabled: true});
if (state.dirty('channel:1')) throw new Error('saved values stayed dirty');
state.track('channel:1', {project: 'new edit', enabled: true});
state.acknowledge('channel:1', {project: 'local edit', enabled: true});
if (!state.dirty('channel:1') || state.mount('channel:1', {project: 'local edit', enabled: true}).project !== 'new edit')
  throw new Error('edit made during save was discarded');
const older = state.issue('channel', 'channel:1');
const newer = state.issue('channel', 'channel:2');
if (state.current(older) || !state.current(newer)) throw new Error('stale response was accepted');
if (!state.lock('channel:1') || state.lock('channel:1')) throw new Error('duplicate operation was not blocked');
state.unlock('channel:1');
"""
    path = ROOT / "mini_app" / "screens" / "va_workspace.js"
    result = subprocess.run([NODE, "-e", script, str(path)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
