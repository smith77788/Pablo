// Interactive smoke check for the Virtual Administrator's most fragile UI flows.
// Set PLAYWRIGHT_JS and CHROME_BIN when Playwright or Chromium isn't on the PATH.
import {createRequire} from 'node:module';
const require = createRequire(import.meta.url);
const {chromium} = process.env.PLAYWRIGHT_JS
  ? require(process.env.PLAYWRIGHT_JS) : await import('playwright');
const path = require('node:path');
const root = path.resolve('mini_app/index.html');
const browser = await chromium.launch(process.env.CHROME_BIN
  ? {executablePath: process.env.CHROME_BIN} : {});
const page = await browser.newPage({viewport: {width: 390, height: 844}});
const errors = [];
page.on('pageerror', error => errors.push(error.message));

await page.addInitScript(() => {
  window.Telegram = {WebApp: {
    ready(){}, expand(){}, initData: '', initDataUnsafe:{user:{id:1,first_name:'Test'}},
    colorScheme:'dark',themeParams:{},MainButton:{show(){},hide(){},setText(){},onClick(){}},
    BackButton:{show(){},hide(){},onClick(){}},
    HapticFeedback:{impactOccurred(){},notificationOccurred(){}},onEvent(){},offEvent(){},
    openTelegramLink(){},showAlert(){},showConfirm(){},close(){},setHeaderColor(){},setBackgroundColor(){}
  }};
  window.vaSmokeSaves = [];
  const channel = id => ({ok:true,channel:{id,title:id==='slow'?'Запоздавший ответ':'Канал '+id,
    username:'channel'+id},settings:{installed:true,enabled:true,setup_done:true,
    topic:'Тема',audience:'',tone:'',notes:'',posts_per_day:2,tz_offset:3,window_start:9,
    window_end:21,publish_mode:'review',auto_tune:true,project_info:'server',lead_contact:'',business:{}},
    pillars:[],plan:[],drafts:[],report:null,events:[],references:[]});
  window.fetch = async (url, options={}) => {
    const pathname = new URL(url, location.href).pathname;
    let result = {ok:true};
    if (pathname.endsWith('/api/miniapp/va/channels')) {
      const all = Array.from({length:61},(_,i)=>({channel_id:String(i+1),
        title:'Канал '+String(i+1).padStart(2,'0'),username:'name'+(i+1),installed:true,
        enabled:true,setup_done:true,topic:'Тема',pending_drafts:0}));
      const params = new URL(url, location.href).searchParams;
      const page = Number(params.get('page') || 0);
      const query = (params.get('q') || '').toLocaleLowerCase('ru');
      const state = params.get('state') || 'all';
      const filtered = all.filter(item => (!query || [item.title,item.username,item.topic]
        .some(value => value.toLocaleLowerCase('ru').includes(query))) &&
        (state !== 'active' || (item.installed && item.enabled && item.setup_done)));
      result = {ok:true,channels:filtered.slice(page*30,page*30+30),total:filtered.length,
        has_more:(page+1)*30<filtered.length,page,drafts:[],network:null};
    } else if (pathname.includes('/api/miniapp/va/channel/')) {
      const id = pathname.split('/').filter(Boolean).at(-1);
      if (options.method === 'PUT') {
        window.vaSmokeSaves.push(JSON.parse(options.body));
        result = channel(id);
        result.settings.project_info = window.vaSmokeSaves.at(-1).project_info || 'server';
      } else {
        if (id === 'slow') await new Promise(resolve => setTimeout(resolve,120));
        result = channel(id);
      }
    }
    return new Response(JSON.stringify(result),{status:200,headers:{'Content-Type':'application/json'}});
  };
});

try {
  await page.goto('file:///' + root.replaceAll('\\','/'));
  // The real app asks an unpaired browser to enter its Telegram pairing code.
  await page.evaluate(() => hidePairingOverlay());
  await page.evaluate(() => openVaAdmin());
  await page.waitForSelector('#vaChannelSearch');
  await page.locator('#vaChannelSearch').fill('Канал');
  await page.waitForFunction(() => _vaListTotal === 61 && document.querySelectorAll('#vaChannelResults [data-va-channel]').length === 30);
  if (await page.locator('#vaChannelResults [data-va-channel]').count() !== 30) throw new Error('Ограничение страницы в 30 каналов не сработало');
  await page.locator('#vaChannelLoadMore').click();
  try {
    await page.waitForFunction(() => document.querySelectorAll('#vaChannelResults [data-va-channel]').length === 60, {timeout:5000});
  } catch {
    const state = await page.evaluate(() => ({
      count:document.querySelectorAll('#vaChannelResults [data-va-channel]').length,
      page:_vaListPage, total:_vaListTotal, hasMore:_vaListHasMore, loading:_vaLoadingMore,
      error:_vaListError, button:document.querySelector('#vaChannelLoadMore')?.outerHTML
    }));
    throw new Error('Не загрузилась следующая страница: ' + JSON.stringify(state));
  }
  await page.locator('#vaChannelLoadMore').click();
  await page.waitForFunction(() => document.querySelectorAll('#vaChannelResults [data-va-channel]').length === 61);
  await page.locator('#vaChannelSearch').fill('ничего-нет');
  await page.waitForFunction(() => document.querySelectorAll('#vaChannelResults [data-va-channel]').length === 0);
  if (!(await page.locator('#vaChannelSearch').isVisible())) throw new Error('При пустом результате пропало поле поиска');
  await page.locator('#vaChannelSearch').fill('Канал 01');
  await page.waitForFunction(() => document.querySelectorAll('#vaChannelResults [data-va-channel]').length === 1);
  await page.locator('#vaChannelFilter').selectOption('active');
  await page.locator('#vaChannelResults [data-va-channel]').first().evaluate(el => openVaChannel(el.dataset.vaChannel));
  await page.waitForSelector('#vaTab-settings');
  await page.locator('#vaTab-settings').click();
  await page.locator('#vaProject').fill('Несохранённый текст');
  await page.locator('#vaTab-knowledge').click();
  await page.locator('#vaTab-settings').click();
  if (await page.locator('#vaProject').inputValue() !== 'Несохранённый текст') throw new Error('Вкладка потеряла черновик');
  await page.evaluate(() => _vaLoadChannel());
  if (await page.locator('#vaProject').inputValue() !== 'Несохранённый текст') throw new Error('Обновление экрана потеряло черновик');
  await page.locator('#vaSaveBtn').click();
  await page.waitForFunction(() => window.vaSmokeSaves.length === 1);
  if (await page.evaluate(() => window.vaSmokeSaves[0].project_info) !== 'Несохранённый текст') throw new Error('Сохранён неверный текст');
  await page.evaluate(async () => {
    const slow = openVaChannel('slow'), current = openVaChannel('2');
    await Promise.all([slow,current]);
  });
  if (!(await page.locator('#s-va-ch-body').innerText()).includes('Канал 2')) throw new Error('Поздний ответ заменил выбранный канал');
  const width = await page.evaluate(() => ({document:document.documentElement.scrollWidth,window:innerWidth}));
  if (width.document > width.window) throw new Error('Экран прокручивается по горизонтали: ' + JSON.stringify(width));
  if (errors.length) throw new Error('Ошибки браузера: ' + errors.join('; '));
  console.log('Готово: поиск, страницы по 30 каналов, фильтр, сохранение черновика, защита от устаревшего ответа и мобильная ширина.');
} finally {
  await browser.close();
}
