// ── Экран «Менеджер по продажам» (screens/sales_manager.js) ────────────────────
// Конструктор сущности-менеджера для бота: все настройки персоны + прайс + привязка
// к боту + заказы. Бэкенд — services/bot_sales_persona.py, роуты /api/miniapp/sales/*.
// Экран создаётся ДИНАМИЧЕСКИ; зависит от глобальных хелперов index.html
// (api, push, back, esc, toast). Всё по клику — хелперы уже загружены.
'use strict';

const SP_SELECTS = {
  tone: [['friendly', 'Дружелюбный'], ['professional', 'Деловой'],
         ['casual', 'Непринуждённый'], ['warm', 'Тёплый'], ['energetic', 'Энергичный']],
  formality: [['auto', 'Авто (под клиента)'], ['ty', 'На «ты»'], ['vy', 'На «вы»']],
  emoji_level: [['none', 'Без эмодзи'], ['low', 'Редко'], ['medium', 'Умеренно'], ['high', 'Часто']],
  msg_length: [['short', 'Коротко'], ['medium', 'Средне'], ['long', 'Развёрнуто']],
  gender: [['unspecified', 'Не указан'], ['female', 'Женский'], ['male', 'Мужской']],
};

// Схема формы: секции + поля. type: text|textarea|number|select|bool|list|channels
const SP_FORM = [
  { sec: '🧑‍💼 Личность' },
  { k: 'name', label: 'Имя менеджера', type: 'text', ph: 'Анна' },
  { k: 'role_title', label: 'Должность', type: 'text', ph: 'старший менеджер по продажам' },
  { k: 'gender', label: 'Пол', type: 'select' },
  { k: 'age', label: 'Возраст', type: 'number', ph: '28' },
  { k: 'avatar_emoji', label: 'Эмодзи-аватар', type: 'text', ph: '🧑‍💼' },
  { k: 'personality', label: 'Характер и манера общения', type: 'textarea',
    ph: 'Тёплая, внимательная, с лёгким юмором. Любит помочь подобрать идеальный вариант.' },

  { sec: '🎙 Стиль общения' },
  { k: 'tone', label: 'Тон', type: 'select' },
  { k: 'formality', label: 'Обращение', type: 'select' },
  { k: 'emoji_level', label: 'Эмодзи', type: 'select' },
  { k: 'msg_length', label: 'Длина ответов', type: 'select' },
  { k: 'humor_level', label: 'Уровень юмора (0–3)', type: 'number', ph: '1' },
  { k: 'language', label: 'Основной язык', type: 'text', ph: 'ru' },
  { k: 'mirror_language', label: 'Отвечать на языке клиента', type: 'bool' },

  { sec: '🏢 Компания и знание товара' },
  { k: 'company_name', label: 'Название компании', type: 'text' },
  { k: 'company_about', label: 'О компании', type: 'textarea' },
  { k: 'product_knowledge', label: 'Что продаём (общее описание)', type: 'textarea',
    ph: 'Свежеобжаренный кофе, аксессуары для заваривания, подписки.' },
  { k: 'pricing_policy', label: 'Политика цен и скидок', type: 'textarea' },
  { k: 'disclose_prices', label: 'Называть цены клиенту', type: 'bool' },
  { k: 'currency', label: 'Валюта', type: 'text', ph: 'USD' },

  { sec: '⚙️ Поведение' },
  { k: 'can_consult', label: 'Консультирует по товарам', type: 'bool' },
  { k: 'can_discuss_prefs', label: 'Узнаёт предпочтения клиента', type: 'bool' },
  { k: 'proactive_offers', label: 'Проактивно предлагает товары', type: 'bool' },
  { k: 'can_smalltalk', label: 'Поддерживает беседу на общие темы', type: 'bool' },
  { k: 'can_take_orders', label: 'Принимает заказы', type: 'bool' },
  { k: 'smalltalk_topics', label: 'Разрешённые темы для беседы', type: 'text',
    ph: 'кофе, утро, книги, путешествия' },
  { k: 'taboo_topics', label: 'Запретные темы (никогда не обсуждать)', type: 'text',
    ph: 'политика, религия' },

  { sec: '📦 Заказы' },
  { k: 'order_fields', label: 'Какие данные собирать (через запятую)', type: 'list',
    ph: 'Имя, Телефон, Адрес доставки' },
  { k: 'order_confirm_message', label: 'Сообщение при подтверждении заказа', type: 'textarea' },

  { sec: '🆘 Оператор и каналы' },
  { k: 'operator_username', label: 'Username оператора', type: 'text', ph: '@operator' },
  { k: 'operator_chat_id', label: 'Chat ID оператора (для уведомлений)', type: 'number' },
  { k: 'handoff_triggers', label: 'Слова-триггеры перевода на оператора', type: 'text',
    ph: 'оператор, возврат, жалоба' },
  { k: 'handoff_message', label: 'Что сказать перед переводом', type: 'textarea' },
  { k: 'channels', label: 'Ссылки на каналы (по строке: Название | https://ссылка)',
    type: 'channels' },

  { sec: '💬 Тексты' },
  { k: 'greeting', label: 'Приветствие', type: 'textarea' },
  { k: 'fallback', label: 'Фраза, когда не знает ответа', type: 'textarea' },
  { k: 'guardrails', label: 'Доп. правила честности/запреты', type: 'textarea' },

  { sec: '🧠 Модель ИИ' },
  { k: 'ai_provider', label: 'AI-провайдер (пусто = авто)', type: 'text' },
  { k: 'model', label: 'Модель (пусто = по умолчанию)', type: 'text' },
  { k: 'max_tokens', label: 'Макс. длина ответа (токенов)', type: 'number', ph: '400' },
  { k: 'temperature', label: 'Температура (0–2)', type: 'text', ph: '0.7' },
];

let _spBots = [];        // боты пользователя (для привязки)
let _spCurrent = null;   // редактируемая персона

function _spScreen(id, title, refreshFn) {
  let el = document.getElementById(id);
  if (el) return el;
  el = document.createElement('div');
  el.className = 'screen';
  el.id = id;
  el.innerHTML =
    '<div class="hdr">' +
      '<div class="hdr-burger" onclick="back()">‹</div>' +
      '<div class="hdr-title">' + esc(title) + '</div>' +
      (refreshFn ? '<div class="hdr-burger" onclick="' + refreshFn + '" title="Обновить">⟳</div>'
                 : '<div class="hdr-burger"></div>') +
    '</div>' +
    '<div class="sb"><div id="' + id + '-body" style="padding:6px 0">' +
      '<div style="text-align:center;color:var(--hint);padding:40px">Загрузка…</div>' +
    '</div></div>';
  document.body.appendChild(el);
  return el;
}

// ── Список персон ──────────────────────────────────────────────────────────────
async function openSalesManager() {
  _spScreen('s-sales', '🧑‍💼 Менеджеры продаж', 'openSalesManager()');
  push('s-sales');
  const body = document.getElementById('s-sales-body');
  body.innerHTML = '<div style="text-align:center;color:var(--hint);padding:40px">Загрузка…</div>';
  try {
    const [pers, bots] = await Promise.all([
      api('/api/miniapp/sales/personas'),
      api('/api/miniapp/bots').catch(() => ({ bots: [] })),
    ]);
    _spBots = bots.bots || [];
    const list = pers.personas || [];
    let h = '<div style="padding:0 4px 10px;font-size:12px;color:var(--hint)">' +
      'Живой ИИ-менеджер для бота: консультирует, называет цены из вашего прайса, ' +
      'принимает заказы, переводит на оператора и делится каналами.</div>' +
      '<button class="btn btn-p" style="width:100%;margin-bottom:12px" ' +
      'onclick="openPersonaEditor(0)">➕ Создать менеджера</button>';
    if (!list.length) {
      h += '<div style="text-align:center;color:var(--hint);padding:30px">Пока нет ни одного менеджера</div>';
    } else {
      h += list.map(p => {
        const bot = _spBots.find(b => String(b.bot_id) === String(p.bot_id));
        const assigned = p.bot_id
          ? '🤖 @' + esc((bot && bot.username) || String(p.bot_id))
          : '<span style="color:var(--hint)">не назначен боту</span>';
        return '<div class="row tap" onclick="openPersonaEditor(' + p.id + ')">' +
          '<div class="row-ico">' + esc(p.avatar_emoji || '🧑‍💼') + '</div>' +
          '<div class="row-body"><div class="row-name">' + esc(p.name) +
          '</div><div class="row-val">' + esc(p.role_title || '') + ' · ' + assigned +
          (p.is_active ? '' : ' · <span style="color:var(--red)">выключен</span>') +
          '</div></div><span class="chev">›</span></div>';
      }).join('');
    }
    body.innerHTML = h;
  } catch (e) {
    body.innerHTML = '<div style="color:var(--red);padding:20px">' + esc(e.message) + '</div>';
  }
}

// ── Редактор персоны ────────────────────────────────────────────────────────────
async function openPersonaEditor(id) {
  _spScreen('s-salesedit', id ? '✏️ Настройка менеджера' : '➕ Новый менеджер', '');
  push('s-salesedit');
  const body = document.getElementById('s-salesedit-body');
  body.innerHTML = '<div style="text-align:center;color:var(--hint);padding:40px">Загрузка…</div>';
  try {
    let persona = {}, products = [];
    if (id) {
      const d = await api('/api/miniapp/sales/persona/' + id);
      persona = d.persona || {}; products = d.products || [];
    }
    _spCurrent = persona;
    _spCurrent._products = products;
    body.innerHTML = _spRenderForm(persona, products);
    _spFillForm(persona);
  } catch (e) {
    body.innerHTML = '<div style="color:var(--red);padding:20px">' + esc(e.message) + '</div>';
  }
}

function _spField(f) {
  const id = 'f_' + f.k;
  const ph = f.ph ? ' placeholder="' + esc(f.ph) + '"' : '';
  let inner;
  if (f.type === 'textarea' || f.type === 'channels') {
    const rows = f.type === 'channels' ? 3 : 2;
    inner = '<textarea id="' + id + '" rows="' + rows + '"' + ph + '></textarea>';
  } else if (f.type === 'select') {
    const opts = (SP_SELECTS[f.k] || []).map(o =>
      '<option value="' + esc(o[0]) + '">' + esc(o[1]) + '</option>').join('');
    inner = '<select id="' + id + '">' + opts + '</select>';
  } else if (f.type === 'bool') {
    return '<div class="field" style="display:flex;align-items:center;gap:10px;justify-content:space-between">' +
      '<label style="margin:0">' + esc(f.label) + '</label>' +
      '<input type="checkbox" id="' + id + '"></div>';
  } else if (f.type === 'number') {
    inner = '<input type="number" id="' + id + '"' + ph + '>';
  } else {
    inner = '<input type="text" id="' + id + '"' + ph + '>';
  }
  return '<div class="field"><label>' + esc(f.label) + '</label>' + inner + '</div>';
}

function _spRenderForm(persona, products) {
  let h = '';
  let curSec = '';
  for (const f of SP_FORM) {
    if (f.sec) { h += '<div class="sec">' + esc(f.sec) + '</div>'; continue; }
    h += _spField(f);
  }
  // Прайс
  h += '<div class="sec">🏷 Прайс (реальные цены — менеджер не выдумывает)</div>';
  h += '<div id="spProducts">' + _spRenderProducts(products) + '</div>';
  if (persona.id) {
    h += '<div style="display:flex;gap:8px;margin:6px 0 12px">' +
      '<input type="text" id="np_name" placeholder="Товар" class="inp" style="flex:2">' +
      '<input type="number" id="np_price" placeholder="Цена" class="inp" style="flex:1">' +
      '<button class="btn btn-s" onclick="spAddProduct()">➕</button></div>';
  } else {
    h += '<div style="font-size:12px;color:var(--hint);padding:0 2px 10px">' +
      'Сохраните менеджера — затем можно добавить товары.</div>';
  }
  // Привязка к боту
  h += '<div class="sec">🤖 Бот</div>';
  const botOpts = ['<option value="">— не назначен —</option>'].concat(
    _spBots.map(b => '<option value="' + b.bot_id + '">@' +
      esc(b.username || b.bot_id) + '</option>')).join('');
  h += '<div class="field"><label>Назначить менеджера боту</label>' +
    '<select id="f_bot_id">' + botOpts + '</select>' +
    '<div class="field-note">Один активный менеджер на бота. Он будет отвечать в личке этого бота.</div></div>';
  // Активность
  h += _spField({ k: 'is_active', label: 'Менеджер включён', type: 'bool' });
  // Кнопки
  h += '<div style="display:flex;gap:8px;margin:14px 0 8px">' +
    '<button class="btn btn-p" style="flex:2" onclick="spSave()">💾 Сохранить</button>';
  if (persona.id) {
    h += '<button class="btn btn-s" style="flex:1" onclick="spOrders(' + persona.id + ')">📦 Заказы</button>';
  }
  h += '</div>';
  if (persona.id) {
    h += '<button class="btn btn-s" style="width:100%;color:var(--red);margin-bottom:20px" ' +
      'onclick="spDelete(' + persona.id + ')">🗑 Удалить менеджера</button>';
  }
  return h;
}

function _spRenderProducts(products) {
  if (!products || !products.length) {
    return '<div style="font-size:12px;color:var(--hint);padding:4px 2px">Товаров пока нет</div>';
  }
  return products.map(pr => {
    const price = ((pr.price_cents || 0) / 100).toFixed(2) + ' ' + (pr.currency || 'USD');
    return '<div class="row"><div class="row-body"><div class="row-name">' + esc(pr.name) +
      '</div><div class="row-val">' + price + (pr.in_stock ? '' : ' · нет в наличии') +
      '</div></div><button class="btn btn-s" style="color:var(--red);padding:5px 10px" ' +
      'onclick="spDelProduct(' + pr.id + ')">✕</button></div>';
  }).join('');
}

function _spFillForm(p) {
  for (const f of SP_FORM) {
    if (f.sec) continue;
    const el = document.getElementById('f_' + f.k);
    if (!el) continue;
    let v = p[f.k];
    if (f.type === 'bool') {
      el.checked = (v === undefined || v === null) ? _spDefaultBool(f.k) : !!v;
    } else if (f.type === 'list') {
      let arr = v;
      if (typeof arr === 'string') { try { arr = JSON.parse(arr); } catch (_) { arr = []; } }
      el.value = Array.isArray(arr) ? arr.join(', ') : (v || '');
    } else if (f.type === 'channels') {
      let arr = v;
      if (typeof arr === 'string') { try { arr = JSON.parse(arr); } catch (_) { arr = []; } }
      el.value = Array.isArray(arr) ? arr.map(c => (c.title || '') + ' | ' + (c.url || '')).join('\n') : '';
    } else {
      el.value = (v === undefined || v === null) ? '' : v;
    }
  }
  const act = document.getElementById('f_is_active');
  if (act) act.checked = (p.is_active === undefined) ? true : !!p.is_active;
  const bot = document.getElementById('f_bot_id');
  if (bot && p.bot_id) bot.value = String(p.bot_id);
}

function _spDefaultBool(k) {
  return ['mirror_language', 'disclose_prices', 'can_consult', 'can_discuss_prefs',
          'proactive_offers', 'can_smalltalk', 'can_take_orders'].includes(k);
}

function _spCollect() {
  const out = {};
  for (const f of SP_FORM) {
    if (f.sec) continue;
    const el = document.getElementById('f_' + f.k);
    if (!el) continue;
    if (f.type === 'bool') { out[f.k] = el.checked; continue; }
    const raw = (el.value || '').trim();
    if (f.type === 'number') {
      if (raw === '') continue;
      out[f.k] = f.k === 'temperature' ? parseFloat(raw) : parseInt(raw, 10);
    } else if (f.type === 'list') {
      out[f.k] = raw ? raw.split(',').map(s => s.trim()).filter(Boolean) : [];
    } else if (f.type === 'channels') {
      out[f.k] = raw ? raw.split('\n').map(line => {
        const parts = line.split('|');
        return { title: (parts[0] || '').trim(), url: (parts[1] || '').trim() };
      }).filter(c => c.url) : [];
    } else {
      out[f.k] = raw;
    }
  }
  const act = document.getElementById('f_is_active');
  if (act) out.is_active = act.checked;
  return out;
}

async function spSave() {
  const data = _spCollect();
  if (!data.name) { toast('Укажите имя менеджера'); return; }
  const botSel = document.getElementById('f_bot_id');
  const botId = botSel ? botSel.value : '';
  try {
    let pid = _spCurrent && _spCurrent.id;
    if (pid) {
      await api('/api/miniapp/sales/persona/' + pid, { method: 'PATCH', body: JSON.stringify(data) });
    } else {
      const r = await api('/api/miniapp/sales/persona', { method: 'POST', body: JSON.stringify(data) });
      pid = r.persona && r.persona.id;
    }
    // привязка/отвязка бота
    if (pid) {
      if (botId) {
        await api('/api/miniapp/sales/persona/' + pid + '/assign',
          { method: 'POST', body: JSON.stringify({ bot_id: parseInt(botId, 10) }) });
      } else if (_spCurrent && _spCurrent.bot_id) {
        await api('/api/miniapp/sales/persona/' + pid + '/unassign', { method: 'POST', body: '{}' });
      }
    }
    toast('Сохранено ✅');
    openPersonaEditor(pid);
  } catch (e) {
    toast(e.message || 'Ошибка сохранения');
  }
}

async function spAddProduct() {
  const pid = _spCurrent && _spCurrent.id;
  if (!pid) return;
  const name = (document.getElementById('np_name').value || '').trim();
  const price = (document.getElementById('np_price').value || '').trim();
  if (!name) { toast('Название товара'); return; }
  try {
    await api('/api/miniapp/sales/persona/' + pid + '/product',
      { method: 'POST', body: JSON.stringify({ name: name, price: price || '0' }) });
    const d = await api('/api/miniapp/sales/persona/' + pid);
    document.getElementById('spProducts').innerHTML = _spRenderProducts(d.products || []);
    document.getElementById('np_name').value = '';
    document.getElementById('np_price').value = '';
  } catch (e) { toast(e.message || 'Ошибка'); }
}

async function spDelProduct(prid) {
  const pid = _spCurrent && _spCurrent.id;
  try {
    await api('/api/miniapp/sales/product/' + prid, { method: 'DELETE' });
    const d = await api('/api/miniapp/sales/persona/' + pid);
    document.getElementById('spProducts').innerHTML = _spRenderProducts(d.products || []);
  } catch (e) { toast(e.message || 'Ошибка'); }
}

async function spDelete(pid) {
  if (!confirm('Удалить менеджера? Заказы сохранятся.')) return;
  try {
    await api('/api/miniapp/sales/persona/' + pid, { method: 'DELETE' });
    toast('Удалён');
    openSalesManager();
  } catch (e) { toast(e.message || 'Ошибка'); }
}

async function spOrders(pid) {
  _spScreen('s-salesorders', '📦 Заказы менеджера', '');
  push('s-salesorders');
  const body = document.getElementById('s-salesorders-body');
  body.innerHTML = '<div style="text-align:center;color:var(--hint);padding:40px">Загрузка…</div>';
  try {
    const d = await api('/api/miniapp/sales/orders');
    const orders = d.orders || [];
    if (!orders.length) {
      body.innerHTML = '<div style="text-align:center;color:var(--hint);padding:30px">Заказов пока нет</div>';
      return;
    }
    const st = { new: '🆕 новый', confirmed: '✅ подтверждён', handoff: '🆘 оператору',
                 cancelled: '❌ отменён', done: '📦 выполнен' };
    body.innerHTML = orders.map(o => {
      let contact = o.contact;
      if (typeof contact === 'string') { try { contact = JSON.parse(contact); } catch (_) { contact = {}; } }
      const phone = (contact && contact.phone) ? ' · 📞 ' + esc(contact.phone) : '';
      const total = ((o.total_cents || 0) / 100).toFixed(2) + ' ' + (o.currency || 'USD');
      const who = o.customer_username ? '@' + esc(o.customer_username)
                : esc(o.customer_name || ('chat ' + o.customer_chat_id));
      return '<div class="row"><div class="row-body">' +
        '<div class="row-name">#' + o.id + ' · ' + who + '</div>' +
        '<div class="row-val">' + (st[o.status] || esc(o.status)) + ' · ' + total + phone + '</div>' +
        '</div></div>';
    }).join('');
  } catch (e) {
    body.innerHTML = '<div style="color:var(--red);padding:20px">' + esc(e.message) + '</div>';
  }
}
