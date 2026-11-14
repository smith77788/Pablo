// Подтверждение уборки с точным Telegram ID и серверным предпросмотром.
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { LAUNCH, chromiumOf } from './_chrome.mjs';

const chromium = await chromiumOf();
const browser = await chromium.launch(LAUNCH);
try {
  const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
  await page.setContent('<body></body>');
  await page.evaluate(() => {
    const item = { kind:'bot', id:'9007199254740993', title:'<script>Проверка</script>',
      status:'hidden', projects:[{id:7,name:'Проект'}], can_disable:false };
    const skipped = { kind:'bot', id:'9007199254740994', title:'Другой бот',
      status:'check_failed', projects:[{id:7,name:'Проект'},{id:8,name:'Другой проект'}], can_disable:false,
      outcome:'skipped', reason:'Изменились права' };
    window.cleanupCalls = [];
    window.openRemovedChannels = () => { window.cleanupRecovery = 'channels'; };
    window.api = async (path, options) => {
      if (!options) return {findings:[item, skipped, {kind:'channel', id:'9007199254740995',
        title:'Отключённый канал', status:'restricted', active:false, can_disable:false,
        disable_reason:'Ресурс уже отключён в Infragram'}], legacy:true, max_targets:2,
        history:[{action:'detach', target:'bot:9007199254740993 project:7'}]};
      const body = JSON.parse(options.body);
      window.cleanupCalls.push({path, body});
      return body.preview ? {items:[{...item, edge_ids:[1,2], outcome:'ready'}, skipped], token:'подтверждение'}
        : {items:[{...item, outcome:'applied'}, skipped]};
    };
    window.esc = value => String(value).replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    window.errRu = (error, fallback) => error.message || fallback;
  });
  await page.addScriptTag({ path: fileURLToPath(new URL('../../mini_app/screens/op_cleanup.js', import.meta.url)) });
  await page.evaluate(() => openOperationCleanup('42'));
  assert.equal(await page.getByText('Проверка сохранена в старом формате.', {exact:false}).count(), 1);
  assert.equal(await page.evaluate(() => document.activeElement.hasAttribute('data-cleanup-close')), true);
  await page.keyboard.press('Tab');
  assert.equal(await page.evaluate(() => document.activeElement.hasAttribute('data-cleanup-action')), true);
  const target = page.locator('[data-cleanup-target]').first();
  const preview = page.getByRole('button', { name: 'Проверить план' });
  assert.equal(await target.isChecked(), false);
  assert.equal(await preview.isEnabled(), false);
  assert.equal(await page.locator('[data-cleanup-action]').inputValue(), 'disable');
  assert.equal(await target.isEnabled(), false);
  await page.getByText('В этом срезе нет ресурсов, доступных для выбранного действия.').waitFor();
  await page.locator('[data-cleanup-action]').selectOption('detach');
  await page.getByText('Выберите проект, чтобы отметить его ресурсы.').waitFor();
  assert.equal(await target.isEnabled(), false);
  await page.locator('[data-cleanup-project]').selectOption('7');
  await page.getByText('Скрыт из глобального поиска', {exact:false}).waitFor();
  await page.getByRole('button', {name:'Выбрать доступные'}).click();
  assert.equal(await page.locator('[data-cleanup-target]:checked').count(), 2);
  await page.getByRole('button', {name:'Снять выбор'}).click();
  assert.equal(await page.locator('[data-cleanup-target]:checked').count(), 0);
  await page.getByText('История изменений', {exact:true}).click();
  await page.getByText('Убрать из проекта · bot:9007199254740993 project:7').waitFor();
  await page.locator('#opCleanupStatus').selectOption('hidden');
  await page.getByRole('button', {name:'Выбрать доступные'}).click();
  await page.locator('#opCleanupStatus').selectOption('check_failed');
  await page.getByText('Выбрано: 1 · вне фильтра: 1').waitFor();
  await page.getByRole('button', {name:'Выбрать доступные'}).click();
  await preview.click();
  await page.getByText('Отвязать от проекта · связей: 2').waitFor();
  assert.equal(await page.getByText('<script>Проверка</script>', {exact:true}).count(), 1);
  await page.getByText('Изменились права', {exact:true}).waitFor();
  assert.equal((await page.evaluate(() => window.cleanupCalls)).length, 1);
  await page.getByRole('button', { name: 'Подтвердить' }).click();
  await page.getByText('Применено', {exact:true}).waitFor();
  await page.getByText('Пропущено · Изменились права', {exact:true}).waitFor();
  const base = {action:'detach', project_id:'7',
    targets:['9007199254740993','9007199254740994'].map(id => ({kind:'bot', id}))};
  const path = '/api/miniapp/operations/42/cleanup';
  assert.deepEqual(await page.evaluate(() => window.cleanupCalls), [
    {path, body:{...base, preview:true}}, {path, body:{...base, token:'подтверждение'}},
  ]);
  await page.getByRole('button', {name:'Обновить список'}).click();
  await page.getByRole('button', {name:'Открыть список восстановления каналов и групп'}).click();
  assert.equal(await page.evaluate(() => window.cleanupRecovery), 'channels');
  assert.equal(await page.getByRole('dialog').count(), 0);
} finally {
  await browser.close();
}
