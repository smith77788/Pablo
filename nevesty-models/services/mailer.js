'use strict';

/**
 * Mailer service — email notifications via nodemailer
 * Uses SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, SMTP_FROM env vars
 * If env not configured — silently logs, does not throw
 */

let nodemailer;
try {
  nodemailer = require('nodemailer');
} catch {
  nodemailer = null;
}

// ─── Status labels (RU) ──────────────────────────────────────────────────────
const STATUS_LABELS = {
  new: 'Новая',
  reviewing: 'На рассмотрении',
  confirmed: 'Подтверждена',
  in_progress: 'В работе',
  completed: 'Завершена',
  cancelled: 'Отменена',
};

const EVENT_LABELS = {
  fashion_show: 'Показ мод',
  photo_shoot: 'Фотосессия',
  event: 'Мероприятие',
  commercial: 'Коммерческий проект',
  runway: 'Показ (подиум)',
  other: 'Другое',
};

// ─── Status badge colors ─────────────────────────────────────────────────────
const STATUS_COLORS = {
  new: '#c9a96e',
  reviewing: '#5b9bd5',
  confirmed: '#4caf50',
  in_progress: '#ff9800',
  completed: '#43a047',
  cancelled: '#e53935',
};

// ─── Create transporter ───────────────────────────────────────────────────────
function createTransporter() {
  if (!nodemailer) {
    console.log('[mailer] nodemailer not available — skipping email');
    return null;
  }
  const { SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS } = process.env;
  if (!SMTP_HOST || !SMTP_USER || !SMTP_PASS) {
    console.log('[mailer] SMTP not configured (SMTP_HOST/SMTP_USER/SMTP_PASS missing) — skipping email');
    return null;
  }
  return nodemailer.createTransport({
    host: SMTP_HOST,
    port: parseInt(SMTP_PORT || '587', 10),
    secure: parseInt(SMTP_PORT || '587', 10) === 465,
    auth: { user: SMTP_USER, pass: SMTP_PASS },
    tls: { rejectUnauthorized: false },
  });
}

// ─── Safe send ───────────────────────────────────────────────────────────────
async function send(to, subject, html) {
  if (!to) return;
  const transporter = createTransporter();
  if (!transporter) return;
  const from = process.env.SMTP_FROM || `Nevesty Models <${process.env.SMTP_USER}>`;
  try {
    await transporter.sendMail({ from, to, subject, html });
    console.log(`[mailer] sent "${subject}" → ${to}`);
  } catch (e) {
    console.error(`[mailer] error sending to ${to}:`, e.message);
  }
}

// ─── Shared HTML wrapper ─────────────────────────────────────────────────────
function wrapHtml(title, bodyContent) {
  return `<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>${title}</title>
</head>
<body style="margin:0;padding:0;background:#0f0f0f;font-family:'Segoe UI',Arial,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#0f0f0f;padding:32px 0;">
  <tr><td align="center">
    <table width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;background:#1a1a1a;border-radius:12px;overflow:hidden;border:1px solid #2a2a2a;">

      <!-- Header -->
      <tr>
        <td style="background:linear-gradient(135deg,#1a1a1a 0%,#222 100%);padding:32px 40px;border-bottom:2px solid #c9a96e;text-align:center;">
          <div style="font-size:24px;font-weight:700;letter-spacing:3px;color:#c9a96e;text-transform:uppercase;">NEVESTY MODELS</div>
          <div style="font-size:12px;color:#888;margin-top:6px;letter-spacing:2px;text-transform:uppercase;">Элитное модельное агентство</div>
        </td>
      </tr>

      <!-- Body -->
      <tr>
        <td style="padding:36px 40px;color:#e0e0e0;">
          ${bodyContent}
        </td>
      </tr>

      <!-- Footer -->
      <tr>
        <td style="padding:24px 40px;border-top:1px solid #2a2a2a;text-align:center;">
          <p style="margin:0;font-size:12px;color:#555;line-height:1.6;">
            © ${new Date().getFullYear()} Nevesty Models. Все права защищены.<br />
            Если вы получили это письмо по ошибке — просто проигнорируйте его.
          </p>
        </td>
      </tr>

    </table>
  </td></tr>
</table>
</body>
</html>`;
}

// ─── Order info block (reusable) ─────────────────────────────────────────────
function orderInfoBlock(orderData) {
  const rows = [];
  if (orderData.order_number)
    rows.push(['Номер заявки', `<strong style="color:#c9a96e">${orderData.order_number}</strong>`]);
  if (orderData.event_type) rows.push(['Мероприятие', EVENT_LABELS[orderData.event_type] || orderData.event_type]);
  if (orderData.event_date) rows.push(['Дата', orderData.event_date]);
  if (orderData.event_duration) rows.push(['Длительность', `${orderData.event_duration} ч.`]);
  if (orderData.location) rows.push(['Место', orderData.location]);
  if (orderData.budget) rows.push(['Бюджет', orderData.budget]);
  if (orderData.model_name) rows.push(['Модель', orderData.model_name]);
  if (orderData.comments) rows.push(['Комментарий', orderData.comments]);

  const tableRows = rows
    .map(
      ([label, value]) => `
    <tr>
      <td style="padding:10px 16px;font-size:13px;color:#888;white-space:nowrap;border-bottom:1px solid #252525;">${label}</td>
      <td style="padding:10px 16px;font-size:13px;color:#e0e0e0;border-bottom:1px solid #252525;">${value}</td>
    </tr>`
    )
    .join('');

  return `<table width="100%" cellpadding="0" cellspacing="0" style="background:#111;border-radius:8px;overflow:hidden;border:1px solid #252525;margin-top:20px;">
    ${tableRows}
  </table>`;
}

// ─── Status badge ─────────────────────────────────────────────────────────────
function statusBadge(status) {
  const label = STATUS_LABELS[status] || status;
  const color = STATUS_COLORS[status] || '#888';
  return `<span style="display:inline-block;padding:4px 14px;background:${color}22;border:1px solid ${color};border-radius:20px;color:${color};font-size:13px;font-weight:600;">${label}</span>`;
}

// ─── 1. Order confirmation (to client) ───────────────────────────────────────
async function sendOrderConfirmation(email, orderData) {
  if (!email) return;
  const subject = `Заявка ${orderData.order_number || ''} принята — Nevesty Models`;
  const html = wrapHtml(
    'Заявка принята',
    `
    <h2 style="margin:0 0 8px;font-size:22px;color:#c9a96e;font-weight:600;">Ваша заявка принята!</h2>
    <p style="margin:0 0 20px;font-size:15px;color:#aaa;line-height:1.6;">
      Спасибо, <strong style="color:#e0e0e0">${orderData.client_name || 'уважаемый клиент'}</strong>!<br />
      Мы получили вашу заявку и скоро свяжемся с вами для подтверждения деталей.
    </p>

    ${orderInfoBlock(orderData)}

    <div style="margin-top:28px;padding:20px;background:#111;border-radius:8px;border-left:3px solid #c9a96e;">
      <p style="margin:0;font-size:13px;color:#aaa;line-height:1.6;">
        Вы можете отслеживать статус заявки на нашем сайте или через Telegram-бот.<br />
        Номер заявки для отслеживания: <strong style="color:#c9a96e">${orderData.order_number || '—'}</strong>
      </p>
    </div>

    <p style="margin-top:24px;font-size:13px;color:#666;line-height:1.6;">
      По всем вопросам: <a href="mailto:${process.env.AGENCY_EMAIL || 'info@nevesty-models.ru'}" style="color:#c9a96e;text-decoration:none;">${process.env.AGENCY_EMAIL || 'info@nevesty-models.ru'}</a>
    </p>
  `
  );
  await send(email, subject, html);
}

// ─── 2. Status change (to client) ────────────────────────────────────────────
async function sendStatusChange(email, orderData, oldStatus, newStatus) {
  if (!email) return;
  const subject = `Статус заявки ${orderData.order_number || ''} изменён — Nevesty Models`;
  const html = wrapHtml(
    'Статус заявки изменён',
    `
    <h2 style="margin:0 0 8px;font-size:22px;color:#c9a96e;font-weight:600;">Статус вашей заявки изменён</h2>
    <p style="margin:0 0 20px;font-size:15px;color:#aaa;line-height:1.6;">
      Уважаемый(ая) <strong style="color:#e0e0e0">${orderData.client_name || 'клиент'}</strong>,<br />
      статус вашей заявки <strong style="color:#c9a96e">${orderData.order_number || ''}</strong> был обновлён.
    </p>

    <table cellpadding="0" cellspacing="0" style="margin:20px 0;">
      <tr>
        <td style="padding-right:16px;font-size:13px;color:#888;">Было:</td>
        <td>${statusBadge(oldStatus)}</td>
      </tr>
      <tr><td colspan="2" style="padding:8px 0;"></td></tr>
      <tr>
        <td style="padding-right:16px;font-size:13px;color:#888;">Стало:</td>
        <td>${statusBadge(newStatus)}</td>
      </tr>
    </table>

    ${orderInfoBlock(orderData)}

    ${
      newStatus === 'confirmed'
        ? `
    <div style="margin-top:24px;padding:20px;background:#0d2b14;border-radius:8px;border-left:3px solid #4caf50;">
      <p style="margin:0;font-size:14px;color:#aaa;line-height:1.6;">
        Ваша заявка подтверждена! Менеджер свяжется с вами в ближайшее время для уточнения деталей.
      </p>
    </div>`
        : ''
    }

    ${
      newStatus === 'cancelled'
        ? `
    <div style="margin-top:24px;padding:20px;background:#2b0d0d;border-radius:8px;border-left:3px solid #e53935;">
      <p style="margin:0;font-size:14px;color:#aaa;line-height:1.6;">
        К сожалению, ваша заявка была отменена. Если у вас есть вопросы — пожалуйста, свяжитесь с нами.
      </p>
    </div>`
        : ''
    }

    <p style="margin-top:24px;font-size:13px;color:#666;line-height:1.6;">
      По всем вопросам: <a href="mailto:${process.env.AGENCY_EMAIL || 'info@nevesty-models.ru'}" style="color:#c9a96e;text-decoration:none;">${process.env.AGENCY_EMAIL || 'info@nevesty-models.ru'}</a>
    </p>
  `
  );
  await send(email, subject, html);
}

// ─── 3. Manager notification (to admin) ──────────────────────────────────────
async function sendManagerNotification(adminEmail, orderData) {
  if (!adminEmail) return;
  const subject = `Новая заявка ${orderData.order_number || ''} — Nevesty Models`;
  const html = wrapHtml(
    'Новая заявка',
    `
    <h2 style="margin:0 0 8px;font-size:22px;color:#c9a96e;font-weight:600;">Новая заявка от клиента</h2>
    <p style="margin:0 0 20px;font-size:15px;color:#aaa;line-height:1.6;">
      Поступила новая заявка. Пожалуйста, рассмотрите её в панели управления.
    </p>

    <table width="100%" cellpadding="0" cellspacing="0" style="background:#111;border-radius:8px;overflow:hidden;border:1px solid #252525;margin-bottom:20px;">
      <tr>
        <td style="padding:10px 16px;font-size:13px;color:#888;border-bottom:1px solid #252525;">Номер заявки</td>
        <td style="padding:10px 16px;font-size:13px;color:#c9a96e;font-weight:600;border-bottom:1px solid #252525;">${orderData.order_number || '—'}</td>
      </tr>
      <tr>
        <td style="padding:10px 16px;font-size:13px;color:#888;border-bottom:1px solid #252525;">Клиент</td>
        <td style="padding:10px 16px;font-size:13px;color:#e0e0e0;border-bottom:1px solid #252525;">${orderData.client_name || '—'}</td>
      </tr>
      <tr>
        <td style="padding:10px 16px;font-size:13px;color:#888;border-bottom:1px solid #252525;">Телефон</td>
        <td style="padding:10px 16px;font-size:13px;color:#e0e0e0;border-bottom:1px solid #252525;"><a href="tel:${orderData.client_phone || ''}" style="color:#c9a96e;text-decoration:none;">${orderData.client_phone || '—'}</a></td>
      </tr>
      ${
        orderData.client_email
          ? `<tr>
        <td style="padding:10px 16px;font-size:13px;color:#888;border-bottom:1px solid #252525;">Email</td>
        <td style="padding:10px 16px;font-size:13px;color:#e0e0e0;border-bottom:1px solid #252525;"><a href="mailto:${orderData.client_email}" style="color:#c9a96e;text-decoration:none;">${orderData.client_email}</a></td>
      </tr>`
          : ''
      }
      ${
        orderData.client_telegram
          ? `<tr>
        <td style="padding:10px 16px;font-size:13px;color:#888;border-bottom:1px solid #252525;">Telegram</td>
        <td style="padding:10px 16px;font-size:13px;color:#e0e0e0;border-bottom:1px solid #252525;">${orderData.client_telegram}</td>
      </tr>`
          : ''
      }
    </table>

    ${orderInfoBlock(orderData)}

    <div style="margin-top:28px;text-align:center;">
      <a href="${process.env.SITE_URL || 'http://localhost:3000'}/admin/orders.html${orderData.id ? `?id=${orderData.id}` : ''}"
         style="display:inline-block;padding:14px 32px;background:#c9a96e;color:#000;text-decoration:none;border-radius:8px;font-size:14px;font-weight:700;letter-spacing:0.5px;">
        Открыть заявку в панели
      </a>
    </div>
  `
  );
  await send(adminEmail, subject, html);
}

// ─── 4. Contact form notification (to admin) ─────────────────────────────────
async function sendContactFormEmail(adminEmail, formData) {
  if (!adminEmail) return;
  const subject = `Сообщение с сайта от ${formData.name || 'Неизвестно'} — Nevesty Models`;
  const html = wrapHtml(
    'Сообщение с сайта',
    `
    <h2 style="margin:0 0 8px;font-size:22px;color:#c9a96e;font-weight:600;">Новое сообщение с сайта</h2>
    <p style="margin:0 0 20px;font-size:15px;color:#aaa;line-height:1.6;">
      Посетитель сайта оставил сообщение через контактную форму.
    </p>

    <table width="100%" cellpadding="0" cellspacing="0" style="background:#111;border-radius:8px;overflow:hidden;border:1px solid #252525;">
      <tr>
        <td style="padding:10px 16px;font-size:13px;color:#888;border-bottom:1px solid #252525;">Имя</td>
        <td style="padding:10px 16px;font-size:13px;color:#e0e0e0;border-bottom:1px solid #252525;">${formData.name || '—'}</td>
      </tr>
      <tr>
        <td style="padding:10px 16px;font-size:13px;color:#888;border-bottom:1px solid #252525;">Телефон</td>
        <td style="padding:10px 16px;font-size:13px;color:#e0e0e0;border-bottom:1px solid #252525;"><a href="tel:${formData.phone || ''}" style="color:#c9a96e;text-decoration:none;">${formData.phone || '—'}</a></td>
      </tr>
      ${
        formData.email
          ? `<tr>
        <td style="padding:10px 16px;font-size:13px;color:#888;border-bottom:1px solid #252525;">Email</td>
        <td style="padding:10px 16px;font-size:13px;color:#e0e0e0;border-bottom:1px solid #252525;"><a href="mailto:${formData.email}" style="color:#c9a96e;text-decoration:none;">${formData.email}</a></td>
      </tr>`
          : ''
      }
      <tr>
        <td style="padding:10px 16px;font-size:13px;color:#888;vertical-align:top;">Сообщение</td>
        <td style="padding:10px 16px;font-size:13px;color:#e0e0e0;line-height:1.6;white-space:pre-wrap;">${formData.message || '—'}</td>
      </tr>
    </table>

    <div style="margin-top:28px;text-align:center;">
      <a href="${process.env.SITE_URL || 'http://localhost:3000'}/admin/orders.html"
         style="display:inline-block;padding:14px 32px;background:#c9a96e;color:#000;text-decoration:none;border-radius:8px;font-size:14px;font-weight:700;letter-spacing:0.5px;">
        Открыть панель управления
      </a>
    </div>
  `
  );
  await send(adminEmail, subject, html);
}

// ─── 5. Review invitation (to client after order completion) ─────────────────
async function sendReviewInvitation(toEmail, orderNum, clientName) {
  if (!toEmail) return;
  const siteUrl = (process.env.SITE_URL || 'https://nevesty-models.ru').replace(/\/$/, '');
  const subject = `Оставьте отзыв о сотрудничестве — ${orderNum}`;
  const html = wrapHtml(
    'Спасибо за сотрудничество!',
    `
    <h2 style="margin:0 0 8px;font-size:22px;color:#c9a96e;font-weight:600;">Спасибо за сотрудничество!</h2>
    <p style="margin:0 0 20px;font-size:15px;color:#aaa;line-height:1.6;">
      Здравствуйте, <strong style="color:#e0e0e0">${clientName || 'уважаемый клиент'}</strong>!<br />
      Ваша заявка <strong style="color:#c9a96e">${orderNum}</strong> успешно завершена.
    </p>

    <div style="padding:24px;background:#111;border-radius:8px;border:1px solid #252525;text-align:center;">
      <p style="margin:0 0 20px;font-size:15px;color:#aaa;line-height:1.6;">
        Будем рады, если вы поделитесь впечатлениями о работе с нашим агентством.<br />
        Ваш отзыв поможет нам стать лучше.
      </p>
      <a href="${siteUrl}/reviews.html"
         style="display:inline-block;padding:14px 32px;background:#c9a96e;color:#000;text-decoration:none;border-radius:8px;font-size:14px;font-weight:700;letter-spacing:0.5px;">
        Оставить отзыв
      </a>
    </div>

    <p style="margin-top:24px;font-size:13px;color:#666;line-height:1.6;">
      По всем вопросам: <a href="mailto:${process.env.AGENCY_EMAIL || 'info@nevesty-models.ru'}" style="color:#c9a96e;text-decoration:none;">${process.env.AGENCY_EMAIL || 'info@nevesty-models.ru'}</a>
    </p>
  `
  );
  await send(toEmail, subject, html);
}

// ─── 6. Password reset (to admin) ────────────────────────────────────────────
async function sendPasswordReset(toEmail, resetLink) {
  if (!toEmail) return;
  const subject = 'Сброс пароля — Nevesty Models';
  const html = wrapHtml(
    'Сброс пароля',
    `
    <h2 style="margin:0 0 8px;font-size:22px;color:#c9a96e;font-weight:600;">Запрос на сброс пароля</h2>
    <p style="margin:0 0 20px;font-size:15px;color:#aaa;line-height:1.6;">
      Вы (или кто-то от вашего имени) запросили сброс пароля для панели управления Nevesty Models.
    </p>

    <div style="padding:24px;background:#111;border-radius:8px;border:1px solid #252525;text-align:center;">
      <p style="margin:0 0 20px;font-size:13px;color:#aaa;">
        Нажмите кнопку ниже, чтобы задать новый пароль. Ссылка действительна в течение 1 часа.
      </p>
      <a href="${resetLink}"
         style="display:inline-block;padding:14px 32px;background:#c9a96e;color:#000;text-decoration:none;border-radius:8px;font-size:14px;font-weight:700;letter-spacing:0.5px;">
        Сбросить пароль
      </a>
    </div>

    <div style="margin-top:24px;padding:16px 20px;background:#1a1a1a;border-radius:8px;border:1px solid #2a2a2a;">
      <p style="margin:0;font-size:12px;color:#555;line-height:1.6;">
        Если вы не запрашивали сброс пароля — просто проигнорируйте это письмо.<br />
        Ваш пароль останется без изменений.
      </p>
    </div>
  `
  );
  await send(toEmail, subject, html);
}

// ─── 7. Test email ───────────────────────────────────────────────────────────
async function sendTestEmail(toEmail) {
  if (!toEmail) return { ok: false, error: 'No recipient' };
  const transporter = createTransporter();
  if (!transporter) return { ok: false, error: 'SMTP not configured' };
  const from = process.env.SMTP_FROM || `Nevesty Models <${process.env.SMTP_USER}>`;
  const html = wrapHtml(
    'Тестовое письмо',
    `
    <h2 style="margin:0 0 8px;font-size:22px;color:#c9a96e;font-weight:600;">Тестовое письмо</h2>
    <p style="margin:0 0 20px;font-size:15px;color:#aaa;line-height:1.6;">
      SMTP настроен корректно. Это тестовое письмо от панели управления Nevesty Models.
    </p>
    <div style="padding:16px 20px;background:#111;border-radius:8px;border-left:3px solid #4caf50;">
      <p style="margin:0;font-size:13px;color:#aaa;">
        Дата и время: <strong style="color:#e0e0e0">${new Date().toLocaleString('ru-RU')}</strong><br />
        SMTP хост: <strong style="color:#e0e0e0">${process.env.SMTP_HOST}</strong><br />
        Отправитель: <strong style="color:#e0e0e0">${from}</strong>
      </p>
    </div>
  `
  );
  try {
    await transporter.sendMail({ from, to: toEmail, subject: 'Тестовое письмо — Nevesty Models', html });
    return { ok: true };
  } catch (e) {
    return { ok: false, error: e.message };
  }
}

// ─── Helper: get admin emails from env ───────────────────────────────────────
function getAdminEmails() {
  return (process.env.ADMIN_EMAILS || '')
    .split(',')
    .map(e => e.trim())
    .filter(Boolean);
}

module.exports = {
  sendOrderConfirmation,
  sendStatusChange,
  sendManagerNotification,
  sendContactFormEmail,
  sendReviewInvitation,
  sendPasswordReset,
  sendTestEmail,
  getAdminEmails,
};
