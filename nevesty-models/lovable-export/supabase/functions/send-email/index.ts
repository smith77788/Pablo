// Supabase Edge Function: Send Email
// Supports SMTP via MailChannels (built-in in Cloudflare, use for Supabase)
// or SendGrid API
// Deploy: supabase functions deploy send-email

import { serve } from 'https://deno.land/std@0.168.0/http/server.ts';

const SENDGRID_API_KEY = Deno.env.get('SENDGRID_API_KEY') || '';
const FROM_EMAIL = Deno.env.get('FROM_EMAIL') || 'noreply@nevesty-models.ru';
const FROM_NAME = Deno.env.get('FROM_NAME') || 'Nevesty Models';

interface EmailPayload {
  to: string;
  subject: string;
  html: string;
  text?: string;
}

async function sendViaSendGrid(payload: EmailPayload): Promise<boolean> {
  const res = await fetch('https://api.sendgrid.com/v3/mail/send', {
    method: 'POST',
    headers: {
      'Authorization': `Bearer ${SENDGRID_API_KEY}`,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({
      personalizations: [{ to: [{ email: payload.to }] }],
      from: { email: FROM_EMAIL, name: FROM_NAME },
      subject: payload.subject,
      content: [
        { type: 'text/html', value: payload.html },
        ...(payload.text ? [{ type: 'text/plain', value: payload.text }] : []),
      ],
    }),
  });
  return res.status === 202;
}

// Email templates
function bookingConfirmTemplate(data: Record<string, string>): string {
  return `
    <div style="font-family:sans-serif;max-width:600px;margin:0 auto">
      <h2 style="color:#c9a96e">Ваша заявка принята!</h2>
      <p>Здравствуйте, <strong>${data.client_name}</strong>!</p>
      <p>Ваша заявка <strong>#${data.order_number}</strong> успешно оформлена.</p>
      <table style="border-collapse:collapse;width:100%">
        <tr><td style="padding:8px;border:1px solid #eee"><b>Мероприятие:</b></td><td style="padding:8px;border:1px solid #eee">${data.event_type}</td></tr>
        <tr><td style="padding:8px;border:1px solid #eee"><b>Дата:</b></td><td style="padding:8px;border:1px solid #eee">${data.event_date || 'Уточняется'}</td></tr>
        <tr><td style="padding:8px;border:1px solid #eee"><b>Модель:</b></td><td style="padding:8px;border:1px solid #eee">${data.model_name || 'Будет назначена'}</td></tr>
      </table>
      <p>Наш менеджер свяжется с вами в ближайшее время.</p>
      <p style="color:#888">С уважением, команда Nevesty Models</p>
    </div>
  `;
}

serve(async (req) => {
  if (req.method !== 'POST') return new Response('Method Not Allowed', { status: 405 });

  const body = await req.json();
  const { to, subject, html, text, template, template_data } = body;

  let finalHtml = html;
  let finalSubject = subject;

  if (template === 'booking_confirm') {
    finalHtml = bookingConfirmTemplate(template_data || {});
    finalSubject = subject || `Заявка #${template_data?.order_number} принята — Nevesty Models`;
  }

  if (!to || !finalSubject || !finalHtml) {
    return new Response('Missing required fields', { status: 400 });
  }

  if (!SENDGRID_API_KEY) {
    console.log('[DEV] Email to:', to, 'Subject:', finalSubject);
    return new Response(JSON.stringify({ sent: false, reason: 'SENDGRID_API_KEY not configured' }), {
      status: 200, headers: { 'Content-Type': 'application/json' }
    });
  }

  const sent = await sendViaSendGrid({ to, subject: finalSubject, html: finalHtml, text });
  return new Response(JSON.stringify({ sent }), {
    headers: { 'Content-Type': 'application/json' }
  });
});
