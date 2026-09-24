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
  sales_intensity: [['soft', 'Мягкий (консультирует)'], ['balanced', 'Сбалансированный'],
                    ['aggressive', 'Активный (дожимает)']],
};

// Пресеты персон — типовые заготовки под бизнес (заполняют поля формы).
const SP_PRESETS = {
  coffee: {
    label: '☕ Кофейня',
    fields: {
      name: 'Аня', role_title: 'бариста-консультант', tone: 'warm', emoji_level: 'high',
      msg_length: 'short', sales_intensity: 'balanced', greeting_by_time: true,
      company_about: 'Свежеобжаренный кофе и напитки навынос.',
      product_knowledge: 'Кофе, авторские напитки, десерты, зёрна домой.',
      smalltalk_topics: 'кофе, утро, книги', can_smalltalk: true,
    },
  },
  clothing: {
    label: '👗 Магазин одежды',
    fields: {
      name: 'Мария', role_title: 'персональный стилист-консультант', tone: 'friendly',
      emoji_level: 'medium', msg_length: 'medium', sales_intensity: 'balanced',
      can_discuss_prefs: true, proactive_offers: true,
      company_about: 'Стильная одежда для повседневности и особых случаев.',
      product_knowledge: 'Одежда, размеры S–XL, помощь с подбором образа.',
    },
  },
  services: {
    label: '🛠 Услуги',
    fields: {
      name: 'Дмитрий', role_title: 'менеджер по работе с клиентами', tone: 'professional',
      formality: 'vy', emoji_level: 'low', msg_length: 'medium', sales_intensity: 'soft',
      company_about: 'Профессиональные услуги под ключ.',
      product_knowledge: 'Консультация, расчёт стоимости, запись на услугу.',
      can_take_orders: true,
    },
  },
  electronics: {
    label: '📱 Электроника',
    fields: {
      name: 'Игорь', role_title: 'консультант по технике', tone: 'friendly',
      emoji_level: 'low', msg_length: 'medium', sales_intensity: 'balanced',
      can_consult: true, proactive_offers: true,
      company_about: 'Гаджеты и техника с гарантией.',
      product_knowledge: 'Смартфоны, ноутбуки, аксессуары; помощь с выбором и гарантией.',
    },
  },
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
  { k: 'sales_intensity', label: 'Стиль продаж', type: 'select' },
  { k: 'scope_guard', label: 'Только по нашим товарам/темам (не отвлекается)', type: 'bool' },
  { k: 'can_consult', label: 'Консультирует по товарам', type: 'bool' },
  { k: 'can_discuss_prefs', label: 'Узнаёт предпочтения клиента', type: 'bool' },
  { k: 'proactive_offers', label: 'Проактивно предлагает товары', type: 'bool' },
  { k: 'can_smalltalk', label: 'Поддерживает беседу на общие темы', type: 'bool' },
  { k: 'can_take_orders', label: 'Принимает заказы', type: 'bool' },
  { k: 'greeting_by_time', label: 'Приветствие по времени суток (утро/день/вечер)', type: 'bool' },
  { k: 'smalltalk_topics', label: 'Разрешённые темы для беседы', type: 'text',
    ph: 'кофе, утро, книги, путешествия' },
  { k: 'taboo_topics', label: 'Запретные темы (никогда не обсуждать)', type: 'text',
    ph: 'политика, религия' },

  { sec: '🕒 Часы работы' },
  { k: 'work_start', label: 'Начало (ЧЧ:ММ, пусто = круглосуточно)', type: 'text', ph: '09:00' },
  { k: 'work_end', label: 'Конец (ЧЧ:ММ)', type: 'text', ph: '21:00' },
  { k: 'work_days', label: 'Рабочие дни (1-5 = Пн–Пт, 1-7 = все)', type: 'text', ph: '1-7' },
  { k: 'tz_offset', label: 'Часовой пояс (смещение к UTC, Москва = 3)', type: 'number', ph: '3' },
  { k: 'offhours_message', label: 'Сообщение вне рабочих часов', type: 'textarea',
    ph: 'Сейчас нерабочее время, ответим утром 🙌' },

  { sec: '📦 Заказы' },
  { k: 'order_fields', label: 'Какие данные собирать (через запятую)', type: 'list',
    ph: 'Имя, Телефон, Адрес доставки' },
  { k: 'order_rules', label: 'Правила заказа и доставки', type: 'textarea',
    ph: 'Доставка от 2 единиц по каждой позиции. Самовывоз без ограничений.' },
  { k: 'min_order_total', label: 'Мин. сумма заказа (0 = без порога)', type: 'money', ph: '10.00' },
  { k: 'free_delivery_threshold', label: 'Бесплатная доставка от суммы (0 = выкл)', type: 'money', ph: '50.00' },
  { k: 'discount_max_percent', label: 'Макс. скидка без оператора, % (0 = не давать)', type: 'number', ph: '10' },
  { k: 'order_confirm_message', label: 'Сообщение при подтверждении заказа', type: 'textarea' },
  { k: 'payment_details', label: 'Реквизиты / инструкция по оплате', type: 'textarea',
    ph: 'Оплата на карту 0000 0000 0000 0000 (Тинькофф), после перевода пришлите чек.' },
  { k: 'payment_via_operator', label: 'Оплату принимает живой оператор (перевести на него)', type: 'bool' },
  { k: 'require_payment_proof', label: 'Требовать подтверждение оплаты (чек/скрин)', type: 'bool' },

  { sec: '🔔 Дожим (фоллоуап)' },
  { k: 'followup_enabled', label: 'Писать самому, если клиент замолчал', type: 'bool' },
  { k: 'followup_delay_min', label: 'Через сколько минут писать', type: 'number', ph: '60' },
  { k: 'followup_message', label: 'Текст дожима', type: 'textarea',
    ph: 'Всё ещё актуально? Помочь с выбором? 🙂' },

  { sec: '🛡 Безопасность' },
  { k: 'rate_limit_per_min', label: 'Лимит сообщений в минуту от клиента (0 = выкл)', type: 'number', ph: '20' },
  { k: 'require_age_confirm', label: 'Спрашивать подтверждение 18+', type: 'bool' },
  { k: 'age_confirm_message', label: 'Как спросить про возраст', type: 'text',
    ph: 'Подтвердите, пожалуйста, что вам есть 18 лет.' },

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
  { k: 'ai_provider', label: 'Провайдер ИИ (пусто — авто)', type: 'text' },
  { k: 'model', label: 'Модель (пусто = по умолчанию)', type: 'text' },
  { k: 'max_tokens', label: 'Макс. длина ответа (токенов)', type: 'number', ph: '400' },
  { k: 'temperature', label: 'Температура (0–2)', type: 'text', ph: '0.7' },
];

let _spBots = [];         // боты пользователя (для привязки)
let _spCurrent = null;    // редактируемая персона
let _spPreselectBot = null; // бот для предвыбора при создании (вход из карточки бота)

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
// botId (необязательно): вход из карточки бота — сразу открыть/создать менеджера
// именно для этого бота.
async function openSalesManager(botId) {
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
    // Вход из карточки бота: если у бота уже есть менеджер — открыть его, иначе
    // создать нового с предвыбранным ботом.
    if (botId) {
      const existing = list.find(p => String(p.bot_id) === String(botId));
      if (existing) { openPersonaEditor(existing.id); return; }
      _spPreselectBot = String(botId);
      openPersonaEditor(0); return;
    }
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
    let persona = {}, products = [], faqs = [], examples = [];
    if (id) {
      const d = await api('/api/miniapp/sales/persona/' + id);
      persona = d.persona || {}; products = d.products || [];
      faqs = d.faqs || []; examples = d.examples || [];
      _spCurrent = persona;
      _spCurrent._delivery = d.delivery || [];
      _spCurrent._promos = d.promos || [];
    } else {
      _spCurrent = persona;
      _spCurrent._delivery = [];
      _spCurrent._promos = [];
    }
    _spCurrent._products = products;
    _spCurrent._faqs = faqs;
    _spCurrent._examples = examples;
    body.innerHTML = _spRenderForm(persona, products);
    _spFillForm(persona);
    // предвыбор бота при создании из карточки бота
    if (!persona.id && _spPreselectBot) {
      const bs = document.getElementById('f_bot_id');
      if (bs) bs.value = _spPreselectBot;
      _spPreselectBot = null;
    }
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
  } else if (f.type === 'money') {
    inner = '<input type="number" step="0.01" min="0" id="' + id + '"' + ph + '>';
  } else {
    inner = '<input type="text" id="' + id + '"' + ph + '>';
  }
  return '<div class="field"><label>' + esc(f.label) + '</label>' + inner + '</div>';
}

function _spRenderForm(persona, products) {
  let h = '';
  // Пресеты — быстрый старт: заполняют поля под типовой бизнес.
  h += '<div class="sec">🎨 Быстрый старт (пресет)</div>';
  const presetBtns = Object.keys(SP_PRESETS).map(k =>
    '<button class="btn btn-s" style="flex:1" onclick="spApplyPreset(\'' + k + '\')">' +
    esc(SP_PRESETS[k].label) + '</button>').join('');
  h += '<div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:6px">' + presetBtns + '</div>' +
    '<div style="font-size:12px;color:var(--hint);padding:0 2px 10px">' +
    'Заполнит настройки под типовой бизнес — потом отредактируйте под себя.</div>';
  let curSec = '';
  for (const f of SP_FORM) {
    if (f.sec) { h += '<div class="sec">' + esc(f.sec) + '</div>'; continue; }
    h += _spField(f);
  }
  // Прайс
  h += '<div class="sec">🏷 Прайс (реальные цены — менеджер не выдумывает)</div>';
  h += '<div id="spProducts">' + _spRenderProducts(products) + '</div>';
  if (persona.id) {
    h += '<div style="display:flex;gap:8px;margin:6px 0 4px">' +
      '<input type="text" id="np_name" placeholder="Товар" class="inp" style="flex:2">' +
      '<input type="number" id="np_price" placeholder="Цена" class="inp" style="flex:1">' +
      '<button class="btn btn-s" onclick="spAddProduct()">➕</button></div>';
    h += '<div style="display:flex;gap:8px;margin:0 0 4px">' +
      '<input type="number" id="np_min_qty" placeholder="Мин. заказ" class="inp" style="flex:1" min="1" value="1">' +
      '<input type="text" id="np_unit" placeholder="Ед. (шт, г, кг)" class="inp" style="flex:1" value="шт">' +
      '<input type="number" id="np_stock" placeholder="Остаток" class="inp" style="flex:1" min="0">' +
      '</div>' +
      '<div style="display:flex;gap:8px;margin:0 0 4px">' +
      '<input type="text" id="np_related" placeholder="С этим берут (через запятую)" class="inp" style="flex:1">' +
      '</div>' +
      '<div style="display:flex;gap:8px;margin:0 0 4px">' +
      '<input type="text" id="np_variants" placeholder="Варианты: S:5.00, M:6.00" class="inp" style="flex:1">' +
      '</div>' +
      '<div style="font-size:12px;color:var(--hint);padding:0 2px 12px">' +
      'Мин. заказ — минимум для заказа (напр. доставка от 2 г). Остаток пусто = ' +
      'неограниченно. Варианты — «название:цена» через запятую.</div>';
  } else {
    h += '<div style="font-size:12px;color:var(--hint);padding:0 2px 10px">' +
      'Сохраните менеджера — затем можно добавить товары.</div>';
  }
  // База знаний (FAQ)
  if (persona.id) {
    h += '<div class="sec">📚 База знаний (точные ответы на частые вопросы)</div>';
    h += '<div id="spFaqs">' + _spRenderFaqs(_spCurrent._faqs || []) + '</div>';
    h += '<input type="text" id="nf_q" placeholder="Вопрос (напр. Есть ли доставка?)" class="inp" style="width:100%;margin:6px 0 4px">' +
      '<textarea id="nf_a" placeholder="Точный ответ" class="inp" rows="2" style="width:100%;margin:0 0 4px"></textarea>' +
      '<div style="display:flex;gap:8px;margin:0 0 4px">' +
      '<input type="text" id="nf_kw" placeholder="Ключевые слова (через запятую)" class="inp" style="flex:2">' +
      '<input type="number" id="nf_prio" placeholder="Приоритет" class="inp" style="flex:1">' +
      '<button class="btn btn-s" onclick="spAddFaq()">➕</button></div>' +
      '<div style="font-size:12px;color:var(--hint);padding:0 2px 12px">' +
      'Менеджер отвечает по базе знаний с приоритетом над догадками — меньше выдумывает.</div>';

    // Эталонные примеры (few-shot)
    h += '<div class="sec">🎯 Примеры «как отвечать» (обучение стилю)</div>';
    h += '<div id="spExamples">' + _spRenderExamples(_spCurrent._examples || []) + '</div>';
    h += '<input type="text" id="ne_u" placeholder="Сообщение клиента" class="inp" style="width:100%;margin:6px 0 4px">' +
      '<textarea id="ne_a" placeholder="Идеальный ответ менеджера" class="inp" rows="2" style="width:100%;margin:0 0 4px"></textarea>' +
      '<div style="display:flex;gap:8px;margin:0 0 12px">' +
      '<button class="btn btn-s" onclick="spAddExample()">➕ Добавить пример</button></div>';

    // Способы доставки
    h += '<div class="sec">🚚 Способы доставки</div>';
    h += '<div id="spDelivery">' + _spRenderDelivery(_spCurrent._delivery || []) + '</div>';
    h += '<div style="display:flex;gap:8px;margin:6px 0 4px">' +
      '<input type="text" id="nd_name" placeholder="Способ (Курьер)" class="inp" style="flex:2">' +
      '<input type="number" step="0.01" min="0" id="nd_price" placeholder="Цена (0=беспл.)" class="inp" style="flex:1">' +
      '</div>' +
      '<div style="display:flex;gap:8px;margin:0 0 4px">' +
      '<input type="text" id="nd_eta" placeholder="Срок (1-2 дня)" class="inp" style="flex:1">' +
      '<input type="text" id="nd_zones" placeholder="Зоны (Москва, МО)" class="inp" style="flex:1">' +
      '<button class="btn btn-s" onclick="spAddDelivery()">➕</button></div>' +
      '<div style="font-size:12px;color:var(--hint);padding:0 2px 12px">' +
      'Менеджер называет только эти способы и цены — не выдумывает.</div>';

    // Промокоды
    h += '<div class="sec">🎟 Промокоды</div>';
    h += '<div id="spPromos">' + _spRenderPromos(_spCurrent._promos || []) + '</div>';
    h += '<div style="display:flex;gap:8px;margin:6px 0 4px">' +
      '<input type="text" id="np_code" placeholder="Код (SALE10)" class="inp" style="flex:2">' +
      '<input type="number" min="1" max="100" id="np_percent" placeholder="%" class="inp" style="flex:1">' +
      '<input type="number" step="0.01" min="0" id="np_mintotal" placeholder="От суммы" class="inp" style="flex:1">' +
      '<button class="btn btn-s" onclick="spAddPromo()">➕</button></div>' +
      '<div style="font-size:12px;color:var(--hint);padding:0 2px 12px">' +
      'Скидку по коду система применяет к заказу автоматически при совпадении.</div>';
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
    h += '<div style="display:flex;gap:8px;margin-bottom:8px">' +
      '<button class="btn btn-s" style="flex:1" onclick="spChat(' + persona.id + ')">💬 Тест-чат</button>' +
      '<button class="btn btn-s" style="flex:1" onclick="spStats(' + persona.id + ')">📊 Статистика</button></div>';
    h += '<div id="spStatsOut" style="font-size:13px;margin-bottom:10px"></div>';
    h += '<button class="btn btn-s" style="width:100%;margin-bottom:8px" ' +
      'onclick="spTest(' + persona.id + ')">🧪 Проверить ИИ (почему молчит?)</button>' +
      '<div id="spTestOut" style="font-size:13px;margin-bottom:10px"></div>';
  }
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
    const mq = parseInt(pr.min_qty || 1, 10);
    const minNote = (mq > 1) ? ' · от ' + mq + ' ' + esc(pr.unit || 'шт') : '';
    let stockNote = '';
    if (pr.stock_qty !== undefined && pr.stock_qty !== null) {
      const sq = parseInt(pr.stock_qty, 10);
      stockNote = (sq <= 0) ? ' · нет в наличии' : ' · остаток ' + sq;
    }
    let vs = pr.variants;
    if (typeof vs === 'string') { try { vs = JSON.parse(vs); } catch (_) { vs = []; } }
    const vNote = (Array.isArray(vs) && vs.length) ? ' · вариантов: ' + vs.length : '';
    return '<div class="row"><div class="row-body"><div class="row-name">' + esc(pr.name) +
      '</div><div class="row-val">' + price + (pr.in_stock ? '' : ' · нет в наличии') +
      minNote + stockNote + vNote + '</div></div><button class="btn btn-s" style="color:var(--red);padding:5px 10px" ' +
      'onclick="spDelProduct(' + pr.id + ')">✕</button></div>';
  }).join('');
}

function _spRenderFaqs(faqs) {
  if (!faqs || !faqs.length) {
    return '<div class="empty-inline">Пока пусто</div>';
  }
  return faqs.map(f => {
    const q = esc((f.question || '').slice(0, 80));
    const a = esc((f.answer || '').slice(0, 120));
    return '<div class="row"><div class="row-body"><div class="row-name">' + (q || '—') +
      '</div><div class="row-val" style="white-space:normal">' + a + '</div></div>' +
      '<button class="btn btn-s" style="color:var(--red);padding:5px 10px" ' +
      'onclick="spDelFaq(' + f.id + ')">✕</button></div>';
  }).join('');
}

function _spRenderExamples(examples) {
  if (!examples || !examples.length) {
    return '<div class="empty-inline">Пока пусто</div>';
  }
  return examples.map(e => {
    const u = esc((e.user_msg || '').slice(0, 70));
    const a = esc((e.assistant_msg || '').slice(0, 120));
    return '<div class="row"><div class="row-body"><div class="row-name">👤 ' + u +
      '</div><div class="row-val" style="white-space:normal">🧑‍💼 ' + a + '</div></div>' +
      '<button class="btn btn-s" style="color:var(--red);padding:5px 10px" ' +
      'onclick="spDelExample(' + e.id + ')">✕</button></div>';
  }).join('');
}

async function spAddFaq() {
  const pid = _spCurrent && _spCurrent.id;
  if (!pid) return;
  const q = (document.getElementById('nf_q').value || '').trim();
  const a = (document.getElementById('nf_a').value || '').trim();
  const kw = (document.getElementById('nf_kw').value || '').trim();
  const prio = parseInt((document.getElementById('nf_prio') || {}).value || '0', 10) || 0;
  if (!q && !a) { toast('Заполните вопрос или ответ'); return; }
  try {
    await api('/api/miniapp/sales/persona/' + pid + '/faq',
      { method: 'POST', body: JSON.stringify({ question: q, answer: a, keywords: kw, priority: prio }) });
    const d = await api('/api/miniapp/sales/persona/' + pid);
    _spCurrent._faqs = d.faqs || [];
    document.getElementById('spFaqs').innerHTML = _spRenderFaqs(_spCurrent._faqs);
    ['nf_q', 'nf_a', 'nf_kw', 'nf_prio'].forEach(id => {
      const el = document.getElementById(id); if (el) el.value = '';
    });
  } catch (e) { toast(e.message || 'Ошибка'); }
}

async function spDelFaq(fid) {
  const pid = _spCurrent && _spCurrent.id;
  // Вопрос и ответ человек писал руками, а 🗑 стоит в строке списка: промах
  // пальцем стирал их без вопроса и без отмены.
  if (!await askConfirm('Удалить этот вопрос и ответ?')) return;
  try {
    await api('/api/miniapp/sales/faq/' + fid, { method: 'DELETE' });
    const d = await api('/api/miniapp/sales/persona/' + pid);
    _spCurrent._faqs = d.faqs || [];
    document.getElementById('spFaqs').innerHTML = _spRenderFaqs(_spCurrent._faqs);
  } catch (e) { toast(e.message || 'Ошибка'); }
}

async function spAddExample() {
  const pid = _spCurrent && _spCurrent.id;
  if (!pid) return;
  const u = (document.getElementById('ne_u').value || '').trim();
  const a = (document.getElementById('ne_a').value || '').trim();
  if (!u || !a) { toast('Нужны и сообщение клиента, и ответ'); return; }
  try {
    await api('/api/miniapp/sales/persona/' + pid + '/example',
      { method: 'POST', body: JSON.stringify({ user_msg: u, assistant_msg: a }) });
    const d = await api('/api/miniapp/sales/persona/' + pid);
    _spCurrent._examples = d.examples || [];
    document.getElementById('spExamples').innerHTML = _spRenderExamples(_spCurrent._examples);
    document.getElementById('ne_u').value = '';
    document.getElementById('ne_a').value = '';
  } catch (e) { toast(e.message || 'Ошибка'); }
}

async function spDelExample(eid) {
  const pid = _spCurrent && _spCurrent.id;
  if (!await askConfirm('Удалить этот пример диалога?')) return;
  try {
    await api('/api/miniapp/sales/example/' + eid, { method: 'DELETE' });
    const d = await api('/api/miniapp/sales/persona/' + pid);
    _spCurrent._examples = d.examples || [];
    document.getElementById('spExamples').innerHTML = _spRenderExamples(_spCurrent._examples);
  } catch (e) { toast(e.message || 'Ошибка'); }
}

function _spRenderDelivery(items) {
  if (!items || !items.length) {
    return '<div class="empty-inline">Пока пусто</div>';
  }
  return items.map(d => {
    const price = (d.price_cents ? ((d.price_cents / 100).toFixed(2) + ' ' +
      (_spCurrent.currency || 'USD')) : 'бесплатно');
    const extra = [d.eta, d.zones].filter(Boolean).map(esc).join(' · ');
    return '<div class="row"><div class="row-body"><div class="row-name">' + esc(d.name) +
      '</div><div class="row-val">' + price + (extra ? ' · ' + extra : '') + '</div></div>' +
      '<button class="btn btn-s" style="color:var(--red);padding:5px 10px" ' +
      'onclick="spDelDelivery(' + d.id + ')">✕</button></div>';
  }).join('');
}

function _spRenderPromos(items) {
  if (!items || !items.length) {
    return '<div class="empty-inline">Пока пусто</div>';
  }
  return items.map(p => {
    const mt = p.min_total_cents ? (' · от ' + (p.min_total_cents / 100).toFixed(2)) : '';
    return '<div class="row"><div class="row-body"><div class="row-name">' + esc(p.code) +
      '</div><div class="row-val">−' + parseInt(p.percent, 10) + '%' + mt + '</div></div>' +
      '<button class="btn btn-s" style="color:var(--red);padding:5px 10px" ' +
      'onclick="spDelPromo(' + p.id + ')">✕</button></div>';
  }).join('');
}

async function spAddDelivery() {
  const pid = _spCurrent && _spCurrent.id;
  if (!pid) return;
  const name = (document.getElementById('nd_name').value || '').trim();
  const price = (document.getElementById('nd_price').value || '').trim();
  const eta = (document.getElementById('nd_eta').value || '').trim();
  const zones = (document.getElementById('nd_zones').value || '').trim();
  if (!name) { toast('Название способа'); return; }
  try {
    await api('/api/miniapp/sales/persona/' + pid + '/delivery',
      { method: 'POST', body: JSON.stringify({ name: name, price: price || '0', eta: eta, zones: zones }) });
    const d = await api('/api/miniapp/sales/persona/' + pid);
    _spCurrent._delivery = d.delivery || [];
    document.getElementById('spDelivery').innerHTML = _spRenderDelivery(_spCurrent._delivery);
    ['nd_name', 'nd_price', 'nd_eta', 'nd_zones'].forEach(id => {
      const el = document.getElementById(id); if (el) el.value = '';
    });
  } catch (e) { toast(e.message || 'Ошибка'); }
}

async function spDelDelivery(did) {
  const pid = _spCurrent && _spCurrent.id;
  if (!await askConfirm('Удалить способ доставки?')) return;
  try {
    await api('/api/miniapp/sales/delivery/' + did, { method: 'DELETE' });
    const d = await api('/api/miniapp/sales/persona/' + pid);
    _spCurrent._delivery = d.delivery || [];
    document.getElementById('spDelivery').innerHTML = _spRenderDelivery(_spCurrent._delivery);
  } catch (e) { toast(e.message || 'Ошибка'); }
}

async function spAddPromo() {
  const pid = _spCurrent && _spCurrent.id;
  if (!pid) return;
  const code = (document.getElementById('np_code').value || '').trim();
  const percent = parseInt((document.getElementById('np_percent') || {}).value || '0', 10) || 0;
  const minTotal = (document.getElementById('np_mintotal').value || '').trim();
  if (!code) { toast('Код промокода'); return; }
  if (percent < 1 || percent > 100) { toast('Процент 1–100'); return; }
  try {
    await api('/api/miniapp/sales/persona/' + pid + '/promo',
      { method: 'POST', body: JSON.stringify({ code: code, percent: percent, min_total: minTotal || '0' }) });
    const d = await api('/api/miniapp/sales/persona/' + pid);
    _spCurrent._promos = d.promos || [];
    document.getElementById('spPromos').innerHTML = _spRenderPromos(_spCurrent._promos);
    ['np_code', 'np_percent', 'np_mintotal'].forEach(id => {
      const el = document.getElementById(id); if (el) el.value = '';
    });
  } catch (e) { toast(e.message || 'Ошибка'); }
}

async function spDelPromo(prid) {
  const pid = _spCurrent && _spCurrent.id;
  if (!await askConfirm('Удалить промокод? Он перестанет действовать сразу.')) return;
  try {
    await api('/api/miniapp/sales/promo/' + prid, { method: 'DELETE' });
    const d = await api('/api/miniapp/sales/persona/' + pid);
    _spCurrent._promos = d.promos || [];
    document.getElementById('spPromos').innerHTML = _spRenderPromos(_spCurrent._promos);
  } catch (e) { toast(e.message || 'Ошибка'); }
}

function _spFillForm(p) {
  for (const f of SP_FORM) {
    if (f.sec) continue;
    const el = document.getElementById('f_' + f.k);
    if (!el) continue;
    let v = p[f.k];
    if (f.type === 'bool') {
      el.checked = (v === undefined || v === null) ? _spDefaultBool(f.k) : !!v;
    } else if (f.type === 'money') {
      el.value = (v === undefined || v === null || v === 0 || v === '') ? '' : (parseInt(v, 10) / 100);
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
    } else if (f.type === 'money') {
      out[f.k] = raw === '' ? 0 : Math.round(parseFloat(raw) * 100);
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
  const minQ = parseInt((document.getElementById('np_min_qty') || {}).value || '1', 10);
  const unit = ((document.getElementById('np_unit') || {}).value || 'шт').trim() || 'шт';
  const stockRaw = ((document.getElementById('np_stock') || {}).value || '').trim();
  const related = ((document.getElementById('np_related') || {}).value || '').trim();
  const variantsRaw = ((document.getElementById('np_variants') || {}).value || '').trim();
  if (!name) { toast('Название товара'); return; }
  // варианты «name:price, name:price» → [{name, price_cents}]
  const variants = variantsRaw ? variantsRaw.split(',').map(s => {
    const parts = s.split(':');
    const nm = (parts[0] || '').trim();
    const pc = Math.round(parseFloat((parts[1] || '0').trim()) * 100) || 0;
    return nm ? { name: nm, price_cents: pc } : null;
  }).filter(Boolean) : [];
  const body = { name: name, price: price || '0', min_qty: (minQ > 0 ? minQ : 1),
    unit: unit, related_skus: related, variants: variants };
  if (stockRaw !== '') body.stock_qty = parseInt(stockRaw, 10);
  try {
    await api('/api/miniapp/sales/persona/' + pid + '/product',
      { method: 'POST', body: JSON.stringify(body) });
    const d = await api('/api/miniapp/sales/persona/' + pid);
    document.getElementById('spProducts').innerHTML = _spRenderProducts(d.products || []);
    ['np_name', 'np_price', 'np_stock', 'np_related', 'np_variants'].forEach(id => {
      const el = document.getElementById(id); if (el) el.value = '';
    });
    if (document.getElementById('np_min_qty')) document.getElementById('np_min_qty').value = '1';
    if (document.getElementById('np_unit')) document.getElementById('np_unit').value = 'шт';
  } catch (e) { toast(e.message || 'Ошибка'); }
}

async function spDelProduct(prid) {
  const pid = _spCurrent && _spCurrent.id;
  if (!await askConfirm('Удалить товар из каталога менеджера?')) return;
  try {
    await api('/api/miniapp/sales/product/' + prid, { method: 'DELETE' });
    const d = await api('/api/miniapp/sales/persona/' + pid);
    document.getElementById('spProducts').innerHTML = _spRenderProducts(d.products || []);
  } catch (e) { toast(e.message || 'Ошибка'); }
}

async function spDelete(pid) {
  // window.confirm во встроенном браузере Telegram часто блокируется: диалог не
  // показывался, вызов возвращал false — и кнопка «Удалить» просто не работала.
  // askConfirm идёт через нативный диалог Telegram и падает на confirm только
  // там, где нативного нет.
  if (!await askConfirm('Удалить менеджера? Заказы сохранятся.')) return;
  try {
    await api('/api/miniapp/sales/persona/' + pid, { method: 'DELETE' });
    toast('Удалён');
    openSalesManager();
  } catch (e) { toast(e.message || 'Ошибка'); }
}

async function spTest(pid) {
  const out = document.getElementById('spTestOut');
  if (out) out.innerHTML = '<span style="color:var(--hint)">Проверяю ИИ…</span>';
  try {
    const r = await api('/api/miniapp/sales/persona/' + pid + '/test',
      { method: 'POST', body: '{}' });
    if (!out) return;
    const provs = (r.providers && r.providers.length)
      ? r.providers.join(', ') : '— нет —';
    if (r.ok) {
      out.innerHTML = '<div style="color:var(--accent)">✅ ИИ работает (' +
        esc(r.provider || '') + '/' + esc(r.model || '') + ')</div>' +
        '<div style="color:var(--hint);margin-top:4px">Провайдеры: ' + esc(provs) + '</div>' +
        '<div style="margin-top:6px;padding:8px;background:var(--bg2);border-radius:8px">' +
        esc(r.reply || '') + '</div>';
    } else {
      out.innerHTML = '<div style="color:var(--red)">⚠️ ИИ не отвечает</div>' +
        '<div style="color:var(--hint);margin-top:4px">Провайдеры: ' + esc(provs) + '</div>' +
        '<div style="margin-top:6px;padding:8px;background:rgba(248,113,113,.08);' +
        'border-radius:8px;color:var(--red)">' + esc(r.error || 'неизвестная ошибка') + '</div>';
    }
  } catch (e) {
    if (out) out.innerHTML = '<div style="color:var(--red)">' + esc(e.message) + '</div>';
  }
}

let _spOrdersCache = [];

function spExportOrdersCsv() {
  const out = document.getElementById('spCsvOut');
  const rows = [['id', 'status', 'customer', 'phone', 'items', 'total', 'currency', 'promo']];
  (_spOrdersCache || []).forEach(o => {
    let contact = o.contact;
    if (typeof contact === 'string') { try { contact = JSON.parse(contact); } catch (_) { contact = {}; } }
    let items = o.items;
    if (typeof items === 'string') { try { items = JSON.parse(items); } catch (_) { items = []; } }
    const itemsStr = (Array.isArray(items) ? items : []).map(it =>
      (it.name || '') + (it.qty > 1 ? ' x' + it.qty : '')).join('; ');
    rows.push([o.id, o.status || '',
      o.customer_username ? '@' + o.customer_username : (o.customer_name || o.customer_chat_id),
      (contact && contact.phone) || '', itemsStr,
      ((o.total_cents || 0) / 100).toFixed(2), o.currency || 'USD', o.promo_code || '']);
  });
  const csv = rows.map(r => r.map(c => {
    const s = String(c == null ? '' : c);
    return /[",\n;]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
  }).join(',')).join('\n');
  if (out) {
    out.innerHTML = '<div style="font-size:12px;color:var(--hint);margin-bottom:4px">' +
      'Скопируйте CSV:</div><textarea class="inp" rows="6" style="width:100%;font-family:monospace;font-size:11px" ' +
      'onclick="this.select()">' + esc(csv) + '</textarea>';
  }
}

function spApplyPreset(key) {
  const preset = SP_PRESETS[key];
  if (!preset) return;
  for (const k in preset.fields) {
    const el = document.getElementById('f_' + k);
    if (!el) continue;
    const v = preset.fields[k];
    if (el.type === 'checkbox') el.checked = !!v;
    else el.value = v;
  }
  toast('Пресет применён — проверьте поля и сохраните');
}

async function spStats(pid) {
  const out = document.getElementById('spStatsOut');
  if (out) out.innerHTML = '<span style="color:var(--hint)">Считаю…</span>';
  try {
    const r = await api('/api/miniapp/sales/persona/' + pid + '/stats');
    const s = r.stats || {};
    const rev = ((s.revenue_cents || 0) / 100).toFixed(2);
    const avg = ((s.avg_order_cents || 0) / 100).toFixed(2);
    if (!out) return;
    const cell = (label, val) =>
      '<div style="flex:1;min-width:90px;padding:8px;background:var(--bg2);border-radius:8px">' +
      '<div style="font-size:18px;font-weight:600">' + esc(String(val)) + '</div>' +
      '<div style="font-size:11px;color:var(--hint)">' + esc(label) + '</div></div>';
    out.innerHTML = '<div style="display:flex;gap:8px;flex-wrap:wrap">' +
      cell('Диалогов', s.dialogs_total || 0) +
      cell('Заказов', s.orders_confirmed || 0) +
      cell('Конверсия', (s.conversion_pct || 0) + '%') +
      cell('Выручка', rev) +
      cell('Средний чек', avg) +
      cell('К оператору', s.handoffs || 0) +
      '</div>';
  } catch (e) {
    if (out) out.innerHTML = '<div style="color:var(--red)">' + esc(e.message) + '</div>';
  }
}

let _spChatHistory = [];
let _spChatPid = null;

function spChat(pid) {
  _spChatPid = pid;
  _spChatHistory = [];
  _spScreen('s-saleschat', '💬 Тест-чат с менеджером', '');
  push('s-saleschat');
  const body = document.getElementById('s-saleschat-body');
  body.innerHTML =
    '<div style="font-size:12px;color:var(--hint);padding:0 2px 8px">' +
    'Тестовый диалог — как увидит клиент. Реальные заказы не создаются.</div>' +
    '<div id="spChatLog" style="min-height:200px;max-height:50vh;overflow-y:auto;' +
    'padding:8px;background:var(--bg2);border-radius:10px;margin-bottom:8px"></div>' +
    '<div style="display:flex;gap:8px">' +
    '<input type="text" id="spChatInput" placeholder="Написать сообщение…" class="inp" style="flex:1" ' +
    'onkeydown="if(event.key===\'Enter\')spChatSend()">' +
    '<button class="btn btn-p" onclick="spChatSend()">▶</button></div>';
}

function _spChatRender() {
  const log = document.getElementById('spChatLog');
  if (!log) return;
  log.innerHTML = _spChatHistory.map(m => {
    const mine = m.role === 'user';
    return '<div style="display:flex;justify-content:' + (mine ? 'flex-end' : 'flex-start') + ';margin:4px 0">' +
      '<div style="max-width:80%;padding:7px 10px;border-radius:12px;white-space:pre-wrap;' +
      'background:' + (mine ? 'var(--accent)' : 'var(--bg)') + ';color:' + (mine ? '#fff' : 'var(--fg)') + '">' +
      esc(m.content) + '</div></div>';
  }).join('');
  log.scrollTop = log.scrollHeight;
}

async function spChatSend() {
  const inp = document.getElementById('spChatInput');
  if (!inp) return;
  const text = (inp.value || '').trim();
  if (!text) return;
  inp.value = '';
  _spChatHistory.push({ role: 'user', content: text });
  _spChatRender();
  const log = document.getElementById('spChatLog');
  const typing = document.createElement('div');
  typing.style.cssText = 'color:var(--hint);font-size:12px;padding:4px';
  typing.textContent = 'печатает…';
  if (log) { log.appendChild(typing); log.scrollTop = log.scrollHeight; }
  try {
    const r = await api('/api/miniapp/sales/persona/' + _spChatPid + '/chat',
      { method: 'POST', body: JSON.stringify({ text: text, history: _spChatHistory.slice(0, -1) }) });
    _spChatHistory.push({ role: 'assistant', content: r.reply || '—' });
    _spChatRender();
  } catch (e) {
    if (typing) typing.remove();
    toast(e.message || 'Ошибка');
  }
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
    _spOrdersCache = orders;
    body.innerHTML = '<button class="btn btn-s" style="width:100%;margin-bottom:10px" ' +
      'onclick="spExportOrdersCsv()">⬇️ Экспорт CSV</button>' +
      '<div id="spCsvOut"></div>' + orders.map(o => {
      let contact = o.contact;
      if (typeof contact === 'string') { try { contact = JSON.parse(contact); } catch (_) { contact = {}; } }
      let items = o.items;
      if (typeof items === 'string') { try { items = JSON.parse(items); } catch (_) { items = []; } }
      const phone = (contact && contact.phone) ? ' · 📞 ' + esc(contact.phone) : '';
      const total = ((o.total_cents || 0) / 100).toFixed(2) + ' ' + (o.currency || 'USD');
      const who = o.customer_username ? '@' + esc(o.customer_username)
                : esc(o.customer_name || ('chat ' + o.customer_chat_id));
      const itemsLine = (Array.isArray(items) && items.length)
        ? '<div class="row-val">🛒 ' + esc(items.map(it =>
            (it.name || '') + (it.qty > 1 ? ' ×' + it.qty : '')).join(', ')) + '</div>'
        : '';
      return '<div class="row"><div class="row-body">' +
        '<div class="row-name">#' + o.id + ' · ' + who + '</div>' +
        '<div class="row-val">' + (st[o.status] || esc(o.status)) + ' · ' + total + phone + '</div>' +
        itemsLine +
        '</div></div>';
    }).join('');
  } catch (e) {
    body.innerHTML = '<div style="color:var(--red);padding:20px">' + esc(e.message) + '</div>';
  }
}
