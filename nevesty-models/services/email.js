'use strict';
/**
 * Email notification service using Nodemailer.
 * Falls back gracefully if not configured.
 */
const nodemailer = require('nodemailer');

let transporter = null;

function getTransporter() {
  if (transporter) return transporter;

  const host = process.env.SMTP_HOST;
  const user = process.env.SMTP_USER;
  const pass = process.env.SMTP_PASS;

  if (!host || !user || !pass) return null;

  transporter = nodemailer.createTransport({
    host,
    port: parseInt(process.env.SMTP_PORT || '587'),
    secure: process.env.SMTP_SECURE === 'true',
    auth: { user, pass },
  });
  return transporter;
}

async function sendOrderStatusEmail(order, newStatus, statusLabel) {
  const t = getTransporter();
  if (!t) return false; // not configured
  if (!order.client_email) return false;

  const fromEmail = process.env.SMTP_FROM || process.env.SMTP_USER;
  const agencyName = 'Nevesty Models';

  const subject = `Статус заявки #${order.order_number} изменён на: ${statusLabel}`;
  const html = `
    <div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;padding:20px">
      <h2 style="color:#c9a96e">${agencyName}</h2>
      <p>Здравствуйте, ${order.client_name || 'клиент'}!</p>
      <p>Статус вашей заявки <strong>#${order.order_number}</strong> изменён на: <strong>${statusLabel}</strong></p>
      ${order.event_type ? `<p>Тип мероприятия: ${order.event_type}</p>` : ''}
      ${order.event_date ? `<p>Дата: ${order.event_date}</p>` : ''}
      <p style="margin-top:24px">Если у вас есть вопросы, свяжитесь с нами.</p>
      <hr style="border:none;border-top:1px solid #eee;margin:24px 0">
      <p style="color:#999;font-size:12px">${agencyName} — профессиональное агентство моделей</p>
    </div>
  `;

  try {
    await t.sendMail({ from: `"${agencyName}" <${fromEmail}>`, to: order.client_email, subject, html });
    return true;
  } catch (e) {
    console.error('[Email] sendOrderStatusEmail error:', e.message);
    return false;
  }
}

async function sendNewOrderEmail(order) {
  const t = getTransporter();
  if (!t || !order.client_email) return false;

  const fromEmail = process.env.SMTP_FROM || process.env.SMTP_USER;
  const agencyName = 'Nevesty Models';

  const subject = `Заявка #${order.order_number} принята!`;
  const html = `
    <div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;padding:20px">
      <h2 style="color:#c9a96e">${agencyName}</h2>
      <p>Здравствуйте, ${order.client_name || 'клиент'}!</p>
      <p>Ваша заявка <strong>#${order.order_number}</strong> успешно принята.</p>
      <p>Наш менеджер свяжется с вами в течение 1-2 часов.</p>
      ${order.event_type ? `<p>Тип мероприятия: ${order.event_type}</p>` : ''}
      ${order.event_date ? `<p>Дата: ${order.event_date}</p>` : ''}
      <p style="margin-top:24px">Спасибо за обращение!</p>
    </div>
  `;

  try {
    await t.sendMail({ from: `"${agencyName}" <${fromEmail}>`, to: order.client_email, subject, html });
    return true;
  } catch (e) {
    console.error('[Email] sendNewOrderEmail error:', e.message);
    return false;
  }
}

module.exports = { sendOrderStatusEmail, sendNewOrderEmail, getTransporter };
