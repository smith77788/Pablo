// Supabase Edge Function: Telegram Webhook Handler
// Deploy: supabase functions deploy telegram-webhook
// Set webhook: https://api.telegram.org/bot<TOKEN>/setWebhook?url=https://<project>.supabase.co/functions/v1/telegram-webhook

import { serve } from 'https://deno.land/std@0.168.0/http/server.ts';
import { createClient } from 'https://esm.sh/@supabase/supabase-js@2';

const BOT_TOKEN = Deno.env.get('TELEGRAM_BOT_TOKEN')!;
const WEBHOOK_SECRET = Deno.env.get('TELEGRAM_WEBHOOK_SECRET') || '';
const SUPABASE_URL = Deno.env.get('SUPABASE_URL')!;
const SUPABASE_SERVICE_KEY = Deno.env.get('SUPABASE_SERVICE_ROLE_KEY')!;

const supabase = createClient(SUPABASE_URL, SUPABASE_SERVICE_KEY);

async function sendMessage(chatId: number | string, text: string, extra: Record<string, unknown> = {}) {
  await fetch(`https://api.telegram.org/bot${BOT_TOKEN}/sendMessage`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ chat_id: chatId, text, parse_mode: 'HTML', ...extra }),
  });
}

async function handleMessage(msg: Record<string, unknown>) {
  const chatId = (msg.chat as Record<string, unknown>)?.id as number;
  const text = (msg.text as string) || '';
  const from = msg.from as Record<string, unknown>;

  if (text === '/start') {
    await sendMessage(chatId,
      `👋 Добро пожаловать в модельное агентство!\n\nИспользуйте сайт для просмотра каталога и оформления заявок.`
    );
    return;
  }

  // Store incoming message
  await supabase.from('order_messages').insert({
    from_client: true,
    sender_name: [from?.first_name, from?.last_name].filter(Boolean).join(' ') || 'Клиент',
    sender_username: from?.username,
    content: text,
    chat_id: String(chatId),
    message_type: 'text',
  }).catch(() => {});
}

async function handleCallbackQuery(cq: Record<string, unknown>) {
  const chatId = ((cq.message as Record<string, unknown>)?.chat as Record<string, unknown>)?.id;
  const data = cq.data as string;
  const cqId = cq.id as string;

  await fetch(`https://api.telegram.org/bot${BOT_TOKEN}/answerCallbackQuery`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ callback_query_id: cqId }),
  });

  // Handle booking status updates from admin
  if (data?.startsWith('confirm_booking_')) {
    const bookingId = data.replace('confirm_booking_', '');
    await supabase.from('bookings').update({ status: 'confirmed' }).eq('id', bookingId);
    await sendMessage(chatId!, `✅ Заявка #${bookingId} подтверждена`);
  }
}

serve(async (req) => {
  // Verify webhook secret
  const secret = req.headers.get('X-Telegram-Bot-Api-Secret-Token');
  if (WEBHOOK_SECRET && secret !== WEBHOOK_SECRET) {
    return new Response('Unauthorized', { status: 401 });
  }

  try {
    const update = await req.json();
    if (update.message) await handleMessage(update.message);
    if (update.callback_query) await handleCallbackQuery(update.callback_query);
    return new Response('ok', { status: 200 });
  } catch (err) {
    console.error('Webhook error:', err);
    return new Response('Error', { status: 500 });
  }
});
