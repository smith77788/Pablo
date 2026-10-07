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
  window.vaSmokeListCalls = [];
  const channel = id => ({ok:true,channel:{id,title:id==='slow'?'Запоздавший ответ':'Канал '+id,
    username:'channel'+id},settings:{installed:true,enabled:true,setup_done:true,
    topic:'Тема',audience:'',tone:'',notes:'',posts_per_day:2,tz_offset:3,window_start:9,
    window_end:21,publish_mode:'review',auto_tune:true,project_info:'server',lead_contact:'',business:{}},
    pillars:[],plan:[],drafts:[],report:null,events:[],references:[]});
  window.fetch = async (url, options={}) => {
    const pathname = new URL(url, location.href).pathname;
    let result = {ok:true};
    if (pathname.endsWith('/api/miniapp/va/channels')) {
      window.vaSmokeListCalls.push(url.toString());
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
    } else if (/\/media\/\d+$/.test(pathname)) {
      if (options.method === 'DELETE') window.vaSmokeMediaRemoved = true;
      else result = {ok:true,image:document.createElement('canvas').toDataURL('image/jpeg')};
    } else if (pathname.endsWith('/media')) {
      result = {ok:true,items:window.vaSmokeMediaRemoved ? [] : [{id:12,description:'Гости деловой конференции'}]};
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
  await page.waitForFunction(() => window.vaSmokeListCalls.length > 0);
  if (!new URL(await page.evaluate(() => window.vaSmokeListCalls[0]), 'https://app.invalid').searchParams.has('summary'))
    throw new Error('Первое открытие не загрузило сводные данные');
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
  if (await page.evaluate(() => window.vaSmokeListCalls.slice(1)
    .some(url => new URL(url, 'https://app.invalid').searchParams.get('summary') !== '0')))
    throw new Error('Поиск и пагинация повторно загрузили сводные данные');
  await page.locator('#vaChannelResults [data-va-channel]').first().evaluate(el => openVaChannel(el.dataset.vaChannel));
  await page.waitForSelector('#vaTab-settings');
  await page.locator('#vaTab-settings').click();
  await page.locator('#vaProject').fill('Несохранённый текст');
  await page.locator('details').filter({has:page.locator('#vaGeography')}).locator('summary').click();
  await page.locator('#vaGeography').fill('Киев');
  await page.locator('#vaServiceLimits').fill('Только сопровождение мероприятий');
  await page.locator('#vaTab-knowledge').click();
  await page.locator('#vaTab-settings').click();
  if (await page.locator('#vaProject').inputValue() !== 'Несохранённый текст') throw new Error('Вкладка потеряла черновик');
  await page.evaluate(() => _vaLoadChannel());
  if (await page.locator('#vaProject').inputValue() !== 'Несохранённый текст') throw new Error('Обновление экрана потеряло черновик');
  await page.locator('#vaSaveBtn').click();
  await page.waitForFunction(() => window.vaSmokeSaves.length === 1);
  if (await page.evaluate(() => window.vaSmokeSaves[0].project_info) !== 'Несохранённый текст') throw new Error('Сохранён неверный текст');
  if (await page.evaluate(() => window.vaSmokeSaves[0].business.geography) !== 'Киев') throw new Error('Город не сохранён');
  if (await page.evaluate(() => window.vaSmokeSaves[0].business.service_limits) !== 'Только сопровождение мероприятий') throw new Error('Границы услуг не сохранены');
  await page.locator('#vaTab-knowledge').click();
  await page.getByText('Изображения для постов', {exact:true}).click();
  await page.getByRole('button', {name:'Открыть медиатеку',exact:true}).click();
  await page.getByRole('button', {name:'Показать фото',exact:true}).click();
  await page.waitForFunction(() => document.querySelector('#vaMediaList img')?.naturalWidth > 0);
  if (process.env.VA_SCREENSHOT) await page.screenshot({path:process.env.VA_SCREENSHOT,fullPage:true});
  await page.evaluate(() => { window.askConfirm = async () => true; });
  await page.getByRole('button', {name:'Отключить',exact:true}).click();
  await page.waitForFunction(() => window.vaSmokeMediaRemoved && document.querySelector('#vaMediaList').textContent.includes('Пока нет'));
  await page.evaluate(async () => {
    const slow = openVaChannel('slow'), current = openVaChannel('2');
    await Promise.all([slow,current]);
  });
  if (!(await page.locator('#s-va-ch-body').innerText()).includes('Канал 2')) throw new Error('Поздний ответ заменил выбранный канал');
  const width = await page.evaluate(() => ({document:document.documentElement.scrollWidth,window:innerWidth}));
  if (width.document > width.window) throw new Error('Экран прокручивается по горизонтали: ' + JSON.stringify(width));
  if (errors.length) throw new Error('Ошибки браузера: ' + errors.join('; '));
  await page.setViewportSize({width:1280,height:900});
  if (await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)) throw new Error('Горизонтальный скролл на десктопе');
  console.log('Готово: поиск, пагинация, сохранение города и границ услуг, предпросмотр и отключение фото, защита от устаревшего ответа, мобильная и десктопная ширина.');
} finally {
  await browser.close();
}
