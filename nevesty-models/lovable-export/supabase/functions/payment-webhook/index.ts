// Supabase Edge Function: Payment Webhook Handler
// Handles YooKassa and Stripe webhooks
// Deploy: supabase functions deploy payment-webhook

import { serve } from 'https://deno.land/std@0.168.0/http/server.ts';
import { createClient } from 'https://esm.sh/@supabase/supabase-js@2';

const SUPABASE_URL = Deno.env.get('SUPABASE_URL')!;
const SUPABASE_SERVICE_KEY = Deno.env.get('SUPABASE_SERVICE_ROLE_KEY')!;
const YOOKASSA_SECRET = Deno.env.get('YOOKASSA_SECRET_KEY') || '';
const STRIPE_WEBHOOK_SECRET = Deno.env.get('STRIPE_WEBHOOK_SECRET') || '';

const supabase = createClient(SUPABASE_URL, SUPABASE_SERVICE_KEY);

async function handleYooKassa(body: Record<string, unknown>) {
  const event = body.event as string;
  const obj = body.object as Record<string, unknown>;
  if (event !== 'payment.succeeded') return;

  const metadata = obj.metadata as Record<string, string>;
  const bookingId = metadata?.booking_id;
  if (!bookingId) return;

  await supabase.from('bookings').update({
    payment_status: 'paid',
    payment_id: obj.id,
    payment_provider: 'yookassa',
    paid_at: new Date().toISOString(),
    status: 'confirmed',
  }).eq('id', bookingId);

  await supabase.from('notifications').insert({
    chat_id: metadata.client_chat_id || '',
    type: 'payment_success',
    title: 'Оплата прошла успешно',
    message: `Оплата заявки #${metadata.order_number} подтверждена`,
    data: { booking_id: bookingId },
  });
}

async function handleStripe(payload: string, signature: string) {
  // Basic Stripe signature verification (implement full crypto check for production)
  const timestamp = signature.split(',').find(p => p.startsWith('t='))?.split('=')[1];
  if (!timestamp) return;

  const body = JSON.parse(payload);
  if (body.type !== 'payment_intent.succeeded') return;

  const pi = body.data?.object;
  const bookingId = pi?.metadata?.booking_id;
  if (!bookingId) return;

  await supabase.from('bookings').update({
    payment_status: 'paid',
    payment_id: pi.id,
    payment_provider: 'stripe',
    paid_at: new Date().toISOString(),
    status: 'confirmed',
  }).eq('id', bookingId);
}

serve(async (req) => {
  const provider = new URL(req.url).searchParams.get('provider') || 'yookassa';
  const rawBody = await req.text();

  try {
    if (provider === 'yookassa') {
      await handleYooKassa(JSON.parse(rawBody));
    } else if (provider === 'stripe') {
      const sig = req.headers.get('stripe-signature') || '';
      await handleStripe(rawBody, sig);
    }
    return new Response('ok', { status: 200 });
  } catch (err) {
    console.error('Payment webhook error:', err);
    return new Response('Error', { status: 500 });
  }
});
