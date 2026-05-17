'use strict';

/**
 * Document generation utilities — БЛОК 28
 * Generates HTML contracts and invoices for printing / browser-based PDF export.
 * No heavy PDF libraries (puppeteer / pdfkit) — browser window.print() is used instead.
 */

const PRINT_BUTTON = `
  <div class="no-print" style="background:#f5f5f5;border-bottom:1px solid #ddd;padding:12px 20px;margin-bottom:0;display:flex;align-items:center;gap:12px">
    <button onclick="window.print()" style="background:#4a90d9;color:#fff;border:none;padding:8px 18px;border-radius:5px;font-size:14px;cursor:pointer">
      🖨️ Печать / Сохранить PDF
    </button>
    <span style="color:#666;font-size:12px">Чтобы сохранить как PDF — при печати выберите «Сохранить как PDF»</span>
  </div>`;

/**
 * Format a date string to Russian locale dd.mm.yyyy
 * @param {string|null} dateStr
 * @returns {string}
 */
function fmtDate(dateStr) {
  if (!dateStr) return '«__» ________ ____';
  try {
    return new Date(dateStr).toLocaleDateString('ru-RU', { day: '2-digit', month: '2-digit', year: 'numeric' });
  } catch {
    return dateStr;
  }
}

/**
 * Escape HTML special characters to prevent XSS in generated documents.
 * @param {*} val
 * @returns {string}
 */
function esc(val) {
  return String(val ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

/**
 * Shared <head> styles for documents.
 */
function docStyles() {
  return `
    <style>
      *, *::before, *::after { box-sizing: border-box; }
      body {
        font-family: Arial, Helvetica, sans-serif;
        font-size: 14px;
        line-height: 1.6;
        color: #222;
        margin: 0;
        padding: 0;
      }
      .doc-wrap {
        max-width: 800px;
        margin: 0 auto;
        padding: 40px 40px 60px;
      }
      h1 { text-align: center; font-size: 20px; margin-bottom: 4px; }
      h2 { font-size: 16px; margin-top: 28px; margin-bottom: 8px; border-bottom: 1px solid #ccc; padding-bottom: 4px; }
      p { margin: 6px 0; }
      .center { text-align: center; }
      .field { margin: 8px 0; }
      .label { font-weight: bold; }
      table { width: 100%; border-collapse: collapse; margin-top: 8px; }
      table th, table td { border: 1px solid #aaa; padding: 8px 10px; text-align: left; font-size: 14px; }
      table th { background: #f0f0f0; font-weight: bold; }
      .total-row td { font-weight: bold; background: #f9f9f9; }
      .sig-block { display: flex; gap: 60px; margin-top: 40px; }
      .sig-col { flex: 1; }
      .sig-line { border-bottom: 1px solid #333; margin-top: 40px; margin-bottom: 4px; }
      .sig-hint { font-size: 11px; color: #666; }
      @media print {
        .no-print { display: none !important; }
        body { font-size: 12px; }
        .doc-wrap { padding: 20px 20px 40px; }
      }
    </style>`;
}

/**
 * Generate an HTML contract for an order.
 * @param {Object} order  — row from orders table (joined with model name if needed)
 * @returns {string}      — full HTML document
 */
function generateContractHTML(order) {
  const companyName = esc(process.env.COMPANY_NAME || 'ИП Иванова Н.В.');
  const companyInn = esc(process.env.COMPANY_INN || '770100000000');
  const siteUrl = (process.env.SITE_URL || 'https://nevesty-models.ru').replace(/\/$/, '');

  const orderNum = esc(order.order_number || String(order.id));
  const orderDate = fmtDate(order.created_at);
  const clientName = esc(order.client_name || '');
  const clientPhone = esc(order.client_phone || '');
  const clientEmail = esc(order.client_email || '');
  const eventType = esc(order.event_type || '');
  const eventDate = esc(order.event_date || 'по согласованию');
  const eventDur = order.event_duration ? `${esc(String(order.event_duration))} ч.` : 'по согласованию';
  const location = esc(order.location || 'по согласованию');
  const budget = esc(order.budget || 'по согласованию');
  const modelName = esc(order.model_name || 'по согласованию');
  const comments = esc(order.comments || '');

  return `<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Договор ${orderNum} — ${companyName}</title>
  ${docStyles()}
</head>
<body>
${PRINT_BUTTON}
<div class="doc-wrap">

  <h1>ДОГОВОР ОКАЗАНИЯ УСЛУГ</h1>
  <p class="center">№ ${orderNum} от ${orderDate}</p>

  <h2>1. Стороны договора</h2>
  <p>
    <span class="label">Исполнитель:</span> ${companyName}, ИНН ${companyInn},
    сайт: <a href="${siteUrl}">${siteUrl}</a>
  </p>
  <p>
    <span class="label">Заказчик:</span> ${clientName}
    ${clientPhone ? `, тел.: ${clientPhone}` : ''}
    ${clientEmail ? `, e-mail: ${clientEmail}` : ''}
  </p>
  <p>
    Вместе именуемые «Стороны», заключили настоящий договор о нижеследующем.
  </p>

  <h2>2. Предмет договора</h2>
  <p>
    Исполнитель обязуется оказать услуги по предоставлению модели (ведущей, хостес)
    для участия в мероприятии Заказчика на условиях, указанных ниже.
  </p>

  <h2>3. Параметры мероприятия</h2>
  <table>
    <tr><th style="width:40%">Параметр</th><th>Значение</th></tr>
    <tr><td>Тип мероприятия</td><td>${eventType}</td></tr>
    <tr><td>Дата мероприятия</td><td>${eventDate}</td></tr>
    <tr><td>Длительность</td><td>${eventDur}</td></tr>
    <tr><td>Место проведения</td><td>${location}</td></tr>
    <tr><td>Модель / исполнитель</td><td>${modelName}</td></tr>
    ${comments ? `<tr><td>Дополнительные пожелания</td><td>${comments}</td></tr>` : ''}
  </table>

  <h2>4. Стоимость и порядок оплаты</h2>
  <p>
    <span class="label">Стоимость услуг:</span> ${budget} ₽.
  </p>
  <p>
    Оплата производится в соответствии с договорённостью сторон. Предоплата —
    50% от суммы договора не позднее чем за 3 (три) дня до даты мероприятия.
    Оставшаяся часть — не позднее дня проведения мероприятия.
  </p>

  <h2>5. Права и обязанности сторон</h2>
  <p>
    5.1. Исполнитель обязуется предоставить модель, соответствующую описанию, в согласованное время и место.
  </p>
  <p>
    5.2. Заказчик обязуется оплатить услуги в срок, обеспечить безопасные условия проведения мероприятия
    и соблюдать уважительное отношение к исполнителю.
  </p>
  <p>
    5.3. В случае отмены заявки менее чем за 24 часа до мероприятия предоплата не возвращается.
  </p>

  <h2>6. Ответственность сторон</h2>
  <p>
    Стороны несут ответственность в соответствии с действующим законодательством
    Российской Федерации. Споры решаются путём переговоров, при невозможности —
    в судебном порядке по месту нахождения Исполнителя.
  </p>

  <h2>7. Срок действия договора</h2>
  <p>
    Договор вступает в силу с момента подписания и действует до полного исполнения сторонами
    своих обязательств.
  </p>

  <h2>8. Подписи сторон</h2>
  <div class="sig-block">
    <div class="sig-col">
      <p class="label">Исполнитель:</p>
      <p>${companyName}</p>
      <p>ИНН: ${companyInn}</p>
      <div class="sig-line"></div>
      <p class="sig-hint">подпись / дата</p>
    </div>
    <div class="sig-col">
      <p class="label">Заказчик:</p>
      <p>${clientName}</p>
      <p>${clientPhone}</p>
      <div class="sig-line"></div>
      <p class="sig-hint">подпись / дата</p>
    </div>
  </div>

</div>
</body>
</html>`;
}

/**
 * Generate an HTML invoice (счёт на оплату) for an order.
 * @param {Object} order  — row from orders table
 * @returns {string}      — full HTML document
 */
function generateInvoiceHTML(order) {
  const companyName = esc(process.env.COMPANY_NAME || 'ИП Иванова Н.В.');
  const companyInn = esc(process.env.COMPANY_INN || '770100000000');
  const companyBank = esc(process.env.COMPANY_BANK || 'указать реквизиты банка');
  const siteUrl = (process.env.SITE_URL || 'https://nevesty-models.ru').replace(/\/$/, '');

  const orderNum = esc(order.order_number || String(order.id));
  const orderDate = fmtDate(order.created_at);
  const clientName = esc(order.client_name || '');
  const clientPhone = esc(order.client_phone || '');
  const clientEmail = esc(order.client_email || '');
  const eventType = esc(order.event_type || 'Услуги модели / хостес');
  const eventDate = esc(order.event_date || 'по согласованию');
  const budget = order.budget ? esc(String(order.budget)) : null;
  const modelName = esc(order.model_name || '');

  // Parse budget as number if possible
  const budgetNum = budget ? parseFloat(String(order.budget).replace(/[^\d.]/g, '')) : NaN;
  const budgetFormatted = !isNaN(budgetNum) ? budgetNum.toLocaleString('ru-RU') : budget || 'по согласованию';

  const serviceDesc = [
    eventType,
    eventDate !== 'по согласованию' ? `дата: ${eventDate}` : '',
    modelName ? `модель: ${modelName}` : '',
  ]
    .filter(Boolean)
    .join(', ');

  return `<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Счёт № ${orderNum} — ${companyName}</title>
  ${docStyles()}
</head>
<body>
${PRINT_BUTTON}
<div class="doc-wrap">

  <h1>СЧЁТ НА ОПЛАТУ</h1>
  <p class="center">№ ${orderNum} от ${orderDate}</p>

  <h2>Поставщик (Исполнитель)</h2>
  <p><span class="label">Наименование:</span> ${companyName}</p>
  <p><span class="label">ИНН:</span> ${companyInn}</p>
  <p><span class="label">Банковские реквизиты:</span> ${companyBank}</p>
  <p><span class="label">Сайт:</span> <a href="${siteUrl}">${siteUrl}</a></p>

  <h2>Покупатель (Заказчик)</h2>
  <p><span class="label">ФИО:</span> ${clientName}</p>
  ${clientPhone ? `<p><span class="label">Телефон:</span> ${clientPhone}</p>` : ''}
  ${clientEmail ? `<p><span class="label">E-mail:</span> ${clientEmail}</p>` : ''}

  <h2>Перечень услуг</h2>
  <table>
    <thead>
      <tr>
        <th style="width:5%">№</th>
        <th>Наименование услуги</th>
        <th style="width:12%">Кол-во</th>
        <th style="width:20%">Цена, ₽</th>
        <th style="width:20%">Сумма, ₽</th>
      </tr>
    </thead>
    <tbody>
      <tr>
        <td>1</td>
        <td>${serviceDesc}</td>
        <td>1</td>
        <td>${budgetFormatted}</td>
        <td>${budgetFormatted}</td>
      </tr>
    </tbody>
    <tfoot>
      <tr class="total-row">
        <td colspan="4" style="text-align:right">Итого к оплате:</td>
        <td>${budgetFormatted} ₽</td>
      </tr>
      <tr>
        <td colspan="5" style="font-size:12px;color:#555">НДС не облагается (УСН)</td>
      </tr>
    </tfoot>
  </table>

  <h2>Основание</h2>
  <p>Договор оказания услуг № ${orderNum} от ${orderDate}</p>
  <p>
    <span class="label">Назначение платежа:</span>
    Оплата по договору № ${orderNum} от ${orderDate}, без НДС
  </p>

  <div class="sig-block" style="margin-top:36px">
    <div class="sig-col">
      <p class="label">Исполнитель:</p>
      <p>${companyName}</p>
      <div class="sig-line"></div>
      <p class="sig-hint">подпись / дата</p>
    </div>
    <div class="sig-col">
      <p class="label">Заказчик:</p>
      <p>${clientName}</p>
      <div class="sig-line"></div>
      <p class="sig-hint">подпись / дата</p>
    </div>
  </div>

</div>
</body>
</html>`;
}

module.exports = { generateContractHTML, generateInvoiceHTML };
