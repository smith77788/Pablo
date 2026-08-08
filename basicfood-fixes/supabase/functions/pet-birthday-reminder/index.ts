// pet-birthday-reminder
//
// Daily cron. Finds pet_profiles whose date_of_birth's MM-DD === today,
// and notifies the owner with an in-app message + web push + a one-shot
// promo code BIRTHDAY-<petId8> giving 15% off (mín 300₴).
//
// Idempotency: skip if a notification of type 'pet_birthday' for this pet
// was created within the last 30 days.
//
// Promo code creation is upserted on `code` to keep replays safe.

import { createClient } from "https://esm.sh/@supabase/supabase-js@2.95.0";
import { requireInternalCaller } from "../_shared/auth.ts";

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers":
    "authorization, x-client-info, apikey, content-type, x-cron-secret",
};

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: corsHeaders });
  const gate = await requireInternalCaller(req);
  if (!gate.ok) return gate.response;

  const supabase = createClient(
    Deno.env.get("SUPABASE_URL")!,
    Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
  );

  const today = new Date();
  const mm = String(today.getUTCMonth() + 1).padStart(2, "0");
  const dd = String(today.getUTCDate()).padStart(2, "0");
  const monthDay = `${mm}-${dd}`;

  // Pull all pets with DOB and filter in-memory (small table; index on dob).
  const { data: pets, error } = await supabase
    .from("pet_profiles")
    .select("id, name, user_id, date_of_birth")
    .not("date_of_birth", "is", null)
    .limit(2000);

  if (error) {
    return new Response(JSON.stringify({ error: error.message }), {
      status: 500,
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  }

  const matches = (pets ?? []).filter((p) => {
    if (!p.date_of_birth) return false;
    const d = new Date(p.date_of_birth);
    if (Number.isNaN(d.getTime())) return false;
    const m = String(d.getUTCMonth() + 1).padStart(2, "0");
    const dy = String(d.getUTCDate()).padStart(2, "0");
    return `${m}-${dy}` === monthDay;
  });

  let notified = 0;
  const lookbackIso = new Date(Date.now() - 30 * 86400_000).toISOString();
  const expiresAt = new Date(Date.now() + 14 * 86400_000).toISOString();

  if (matches.length > 0) {
    // Batch dedup: which pets already got notified in last 30d?
    const petIds = matches.map((p) => p.id);
    const { data: existingNotifs } = await supabase
      .from("notifications")
      .select("reference_id")
      .eq("type", "pet_birthday")
      .in("reference_id", petIds)
      .gte("created_at", lookbackIso);
    const alreadyNotified = new Set((existingNotifs ?? []).map((n) => n.reference_id as string));
    const toNotify = matches.filter((p) => !alreadyNotified.has(p.id));

    if (toNotify.length > 0) {
      // Batch upsert all promo codes
      await supabase.from("promo_codes").upsert(
        toNotify.map((pet) => ({
          code: `BDAY${pet.id.slice(0, 8).toUpperCase()}`,
          discount_type: "percentage",
          discount_value: 15,
          min_order_amount: 300,
          max_uses: 1,
          ends_at: expiresAt,
          is_active: true,
        })),
        { onConflict: "code" },
      );

      // Batch insert notifications
      const { error: notifErr } = await supabase.from("notifications").insert(
        toNotify.map((pet) => ({
          user_id: pet.user_id,
          type: "pet_birthday",
          title: `🎂 У ${pet.name} сьогодні день народження!`,
          message: `Вітаємо! Тримайте промокод BDAY${pet.id.slice(0, 8).toUpperCase()} — 15% знижки на ласощі (діє 14 днів).`,
          reference_id: pet.id,
        })),
      );
      if (!notifErr) {
        notified = toNotify.length;
        // Parallel web pushes
        const CRON_SECRET = Deno.env.get("CRON_SECRET") ?? "";
        await Promise.all(
          toNotify.map((pet) => {
            const code = `BDAY${pet.id.slice(0, 8).toUpperCase()}`;
            return supabase.functions.invoke("send-web-push", {
              body: {
                user_id: pet.user_id,
                title: `🎂 День народження ${pet.name}!`,
                body: `Подарунок від BASIC.FOOD: промокод ${code} на 15% знижки.`,
                url: `/cart?promo=${code}`,
                tag: `bday-${pet.id}`,
                campaign: "pet_birthday",
                reference_id: pet.id,
              },
              headers: { "x-cron-secret": CRON_SECRET },
            }).catch(() => {});
          }),
        );
      }
    }
  }

  return new Response(
    JSON.stringify({ ok: true, candidates: matches.length, notified }),
    { headers: { ...corsHeaders, "Content-Type": "application/json" } },
  );
});
