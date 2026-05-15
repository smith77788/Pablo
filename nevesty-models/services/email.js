'use strict';

/**
 * Email notification service — thin wrapper / re-export from mailer.js
 *
 * All logic (DEV_MODE, SendGrid, SMTP, HTML templates) lives in mailer.js.
 * This module exposes the API expected by БЛОК 10.1 and keeps backward
 * compatibility with any code that still requires services/email.js.
 */

const mailer = require('./mailer');

/**
 * DEV_MODE = true when neither SMTP nor SendGrid credentials are configured.
 * Emails are logged to console instead of being sent.
 */
const DEV_MODE = mailer.DEV_MODE;

/** Send order confirmation email to client. */
const sendOrderConfirmation = mailer.sendOrderConfirmation;

/** Send status-change notification email to client. */
const sendStatusChange = mailer.sendStatusChange;

/**
 * Send contact-form submission to admin email.
 * Alias for mailer.sendContactFormEmail.
 */
async function sendContactFormToAdmin(adminEmail, { name, phone, message, email }) {
  return mailer.sendContactFormEmail(adminEmail, { name, phone, email, message });
}

/** Get list of configured admin email addresses. */
const getAdminEmails = mailer.getAdminEmails;

// Backward-compatible aliases used by older tests (email.test.js, api-wave53.test.js)
async function sendOrderStatusEmail(order, status, statusLabel) {
  if (!order || !order.client_email) return false;
  try {
    await sendStatusChange(order.client_email, {
      orderId: order.id || order.order_number,
      clientName: order.client_name || '',
      newStatus: status,
      statusLabel: statusLabel || status,
    });
    return !DEV_MODE;
  } catch {
    return false;
  }
}

async function sendNewOrderEmail(order) {
  if (!order || !order.client_email) return false;
  try {
    await sendOrderConfirmation(order.client_email, {
      orderId: order.id || order.order_number,
      clientName: order.client_name || '',
      modelName: order.model_name,
      eventType: order.event_type,
      eventDate: order.event_date,
    });
    return !DEV_MODE;
  } catch {
    return false;
  }
}

function getTransporter() {
  const hasSmtp = !!(process.env.SMTP_HOST && process.env.SMTP_USER && process.env.SMTP_PASS);
  const hasSendGrid = !!process.env.SENDGRID_API_KEY;
  return hasSmtp || hasSendGrid ? {} : null;
}

module.exports = {
  DEV_MODE,
  sendOrderConfirmation,
  sendStatusChange,
  sendContactFormToAdmin,
  getAdminEmails,
  // Backward-compatible aliases
  sendOrderStatusEmail,
  sendNewOrderEmail,
  getTransporter,
};
