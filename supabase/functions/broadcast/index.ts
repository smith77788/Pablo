// Supabase Edge Function: Send Telegram Broadcast
// Called by cron or admin action
// Deploy: supabase functions deploy broadcast

import { serve } from 'https://deno.land/std@0.168.0/http/server.ts';
import { createClient } from 'https://esm.sh/@supabase/supabase-js@2';

const BOT_TOKEN = Deno.env.get('TELEGRAM_BOT_TOKEN')!;
const SUPABASE_URL = Deno.env.get('SUPABASE_URL')!;
const SUPABASE_SERVICE_KEY = Deno.env.get('SUPABASE_SERVICE_ROLE_KEY')!;

const supabase = createClient(SUPABASE_URL, SUPABASE_SERVICE_KEY);

async function sendTgMessage(chatId: string, text: string, photoUrl?: string) {
  const method = photoUrl ? 'sendPhoto' : 'sendMessage';
  const body = photoUrl
    ? { chat_id: chatId, photo: photoUrl, caption: text, parse_mode: 'HTML' }
    : { chat_id: chatId, text, parse_mode: 'HTML' };

  const res = await fetch(`https://api.telegram.org/bot${BOT_TOKEN}/${method}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  return res.ok;
}

serve(async (req) => {
  const { broadcast_id } = await req.json().catch(() => ({}));
  if (!broadcast_id) return new Response('Missing broadcast_id', { status: 400 });

  const { data: broadcast } = await supabase
    .from('scheduled_broadcasts')
    .select('*')
    .eq('id', broadcast_id)
    .eq('status', 'pending')
    .single();

  if (!broadcast) return new Response('Broadcast not found or already sent', { status: 404 });

  await supabase.from('scheduled_broadcasts').update({ status: 'sending' }).eq('id', broadcast_id);

  // Get recipients from bot_sessions (users who interacted with the bot)
  let query = supabase.from('bot_sessions').select('chat_id');
  if (broadcast.target_city) {
    // Filter by city stored in session data
  }
  const { data: sessions } = await query;

  let sentCount = 0, errorCount = 0;
  for (const session of sessions || []) {
    try {
      const ok = await sendTgMessage(session.chat_id, broadcast.message, broadcast.photo_url);
      if (ok) sentCount++; else errorCount++;
    } catch {
      errorCount++;
    }
    // Rate limit: 30 msgs/sec max
    await new Promise(r => setTimeout(r, 35));
  }

  await supabase.from('scheduled_broadcasts').update({
    status: 'sent',
    sent_at: new Date().toISOString(),
    sent_count: sentCount,
    error_count: errorCount,
  }).eq('id', broadcast_id);

  return new Response(JSON.stringify({ sent: sentCount, errors: errorCount }), {
    headers: { 'Content-Type': 'application/json' }
  });
});
