// Supabase Edge Function: Send SMS
// Supports: smsru, smsc, twilio
// Deploy: supabase functions deploy send-sms

import { serve } from 'https://deno.land/std@0.168.0/http/server.ts';

const SMS_PROVIDER = Deno.env.get('SMS_PROVIDER') || '';
const SMS_RU_API_KEY = Deno.env.get('SMS_RU_API_KEY') || '';
const SMSC_LOGIN = Deno.env.get('SMSC_LOGIN') || '';
const SMSC_PASSWORD = Deno.env.get('SMSC_PASSWORD') || '';
const TWILIO_ACCOUNT_SID = Deno.env.get('TWILIO_ACCOUNT_SID') || '';
const TWILIO_AUTH_TOKEN = Deno.env.get('TWILIO_AUTH_TOKEN') || '';
const TWILIO_FROM = Deno.env.get('TWILIO_FROM_NUMBER') || '';
const SMS_FROM = Deno.env.get('SMS_FROM') || 'NEVESTY';

async function sendViaSmsRu(to: string, text: string): Promise<boolean> {
  const url = new URL('https://sms.ru/sms/send');
  url.searchParams.set('api_id', SMS_RU_API_KEY);
  url.searchParams.set('to', to);
  url.searchParams.set('msg', text);
  url.searchParams.set('from', SMS_FROM);
  url.searchParams.set('json', '1');
  const res = await fetch(url.toString());
  const data = await res.json();
  return data.status === 'OK';
}

async function sendViaSmsc(to: string, text: string): Promise<boolean> {
  const url = new URL('https://smsc.ru/sys/send.php');
  url.searchParams.set('login', SMSC_LOGIN);
  url.searchParams.set('psw', SMSC_PASSWORD);
  url.searchParams.set('phones', to);
  url.searchParams.set('mes', text);
  url.searchParams.set('sender', SMS_FROM);
  url.searchParams.set('fmt', '3');
  const res = await fetch(url.toString());
  const data = await res.json();
  return !data.error;
}

async function sendViaTwilio(to: string, text: string): Promise<boolean> {
  const res = await fetch(
    `https://api.twilio.com/2010-04-01/Accounts/${TWILIO_ACCOUNT_SID}/Messages.json`,
    {
      method: 'POST',
      headers: {
        'Authorization': `Basic ${btoa(`${TWILIO_ACCOUNT_SID}:${TWILIO_AUTH_TOKEN}`)}`,
        'Content-Type': 'application/x-www-form-urlencoded',
      },
      body: new URLSearchParams({ To: to, From: TWILIO_FROM, Body: text }),
    }
  );
  return res.ok;
}

serve(async (req) => {
  if (req.method !== 'POST') return new Response('Method Not Allowed', { status: 405 });

  const { to, text } = await req.json();
  if (!to || !text) return new Response('Missing to or text', { status: 400 });
  if (!SMS_PROVIDER) return new Response(JSON.stringify({ sent: false, reason: 'SMS_PROVIDER not configured' }), {
    status: 200, headers: { 'Content-Type': 'application/json' }
  });

  let sent = false;
  if (SMS_PROVIDER === 'smsru') sent = await sendViaSmsRu(to, text);
  else if (SMS_PROVIDER === 'smsc') sent = await sendViaSmsc(to, text);
  else if (SMS_PROVIDER === 'twilio') sent = await sendViaTwilio(to, text);

  return new Response(JSON.stringify({ sent }), {
    headers: { 'Content-Type': 'application/json' }
  });
});
