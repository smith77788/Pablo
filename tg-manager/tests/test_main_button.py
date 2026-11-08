"""Главное действие мастеров — в системной кнопке Telegram (MainButton).

Проверяется:
* модуль подключён, главные кнопки форм и мастеров помечены data-main;
* в настоящем браузере MainButton берёт подпись кнопки верхнего слоя (окно
  поверх экрана), своя кнопка прячется, нажатие системной зовёт её onclick;
* заблокированная кнопка гасит MainButton, закрытое окно возвращает кнопку
  экрана, а без Telegram ничего не прячется.
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess

import pytest

from tests.miniapp_source import miniapp_html

ROOT = pathlib.Path(__file__).resolve().parents[1]
JS = ROOT / "mini_app" / "screens" / "main_button.js"

_CHROME = [p for p in (os.getenv("CHROME_BIN", ""),
                       "/opt/pw-browsers/chromium-1194/chrome-linux/chrome",
                       "/opt/pw-browsers/chromium/chrome-linux/chrome") if p and os.path.exists(p)]
_PW = [p for p in (os.getenv("PLAYWRIGHT_JS", ""),
                   "/opt/node22/lib/node_modules/playwright/index.mjs") if p and os.path.exists(p)]


def test_wired_and_marked():
    html = miniapp_html()
    assert '<script src="screens/main_button.js"></script>' in html
    assert ".mb-mirrored{display:none !important}" in html
    for btn in ('id="bbBtn"', 'id="arBtn"', 'id="massInviteBtn"', 'id="warmupBtn"',
                'onclick="saveSettings()"'):
        i = html.index(btn)
        tag = html[html.rindex("<button", 0, i):html.index(">", i)]
        assert "data-main" in tag, btn
    assert html.count("data-main") >= 60


_PROBE = r"""
const [pw, chrome, js, inTg] = process.argv.slice(1);
const { chromium } = await import(pw);
const browser = await chromium.launch({ executablePath: chrome, args: ['--no-sandbox'] });
const page = await browser.newPage();
await page.setContent(`
<style>.screen{display:none}.screen.show{display:block}.mbg{display:none}.mbg.show{display:block}
.mb-mirrored{display:none !important}</style>
<div class="screen show" id="s1"><button data-main id="save" onclick="window.hit='save'">💾 Сохранить</button></div>
<div class="mbg" id="mo"><div><button data-main id="go" onclick="window.hit='go'">🚀 Запустить рассылку</button></div></div>`);
await page.evaluate((inTg) => {
  window.mb = { params: null, hidden: false, cb: null,
    setParams(p){ this.params = p; this.hidden = false; }, hide(){ this.hidden = true; },
    onClick(f){ this.cb = f; }, showProgress(){}, hideProgress(){} };
  window.Telegram = { WebApp: { initData: inTg === '1' ? 'x' : '', MainButton: mb,
    isVersionAtLeast: () => true, HapticFeedback: { impactOccurred(){} } } };
}, inTg);
await page.addScriptTag({ path: js });
const st = () => page.evaluate(() => ({ p: mb.params, hidden: mb.hidden,
  save: getComputedStyle(document.getElementById('save')).display,
  go: getComputedStyle(document.getElementById('go')).display, hit: window.hit || null }));
const out = {};
await page.evaluate(() => syncMainButton()); out.screen = await st();
await page.evaluate(() => { document.getElementById('mo').classList.add('show'); syncMainButton(); });
out.modal = await st();
await page.evaluate(() => mb.cb && mb.cb()); out.click = await st();
await page.evaluate(() => { document.getElementById('go').disabled = true; syncMainButton(); });
out.disabled = await st();
await page.evaluate(() => { document.getElementById('mo').classList.remove('show'); syncMainButton(); });
out.closed = await st();
await page.evaluate(() => { document.getElementById('s1').classList.remove('show'); syncMainButton(); });
out.none = await st();
console.log(JSON.stringify(out));
await browser.close();
"""


def _run(in_tg: bool) -> dict:
    if not (shutil.which("node") and _CHROME and _PW):
        pytest.skip("нужны node, Chromium и Playwright")
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", _PROBE, _PW[0], _CHROME[0], str(JS),
         "1" if in_tg else "0"], capture_output=True, text=True, timeout=120)
    assert proc.stdout.strip(), proc.stderr[-800:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_main_button_follows_the_top_layer_in_a_browser():
    r = _run(True)
    assert r["screen"]["p"]["text"] == "Сохранить" and r["screen"]["save"] == "none"
    # Окно поверх экрана забирает системную кнопку; кнопка экрана возвращается.
    assert r["modal"]["p"]["text"] == "Запустить рассылку"
    assert r["modal"]["go"] == "none" and r["modal"]["save"] != "none"
    assert r["click"]["hit"] == "go"
    assert r["disabled"]["p"]["is_active"] is False
    assert r["closed"]["p"]["text"] == "Сохранить" and r["closed"]["go"] != "none"
    assert r["none"]["hidden"] is True


def test_outside_telegram_nothing_is_hidden():
    r = _run(False)
    assert r["screen"]["p"] is None and r["screen"]["save"] != "none"
