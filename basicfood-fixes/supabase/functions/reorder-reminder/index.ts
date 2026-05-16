// Reorder reminder cron — runs daily.
//
// Two-tier nudge (Butternut "heads up + ship-day" pattern):
//   1. PRE-REMINDER: 2 days before next_reminder_at — soft "тримайте в голові,
//      скоро час повторити" notification + web push. Helps the customer plan,
//      lowers churn risk vs. a single hard "час замовити" ping.
//   2. DUE REMINDER: when next_reminder_at has elapsed — hard reminder, then
//      schedule advances by cadence_days.
//
// Dedupe:
//   - PRE: skip if a 'reorder_pre_reminder' notification already exists for
//     this plan within the last (cadence_days - 1) days.
//   - DUE: existing last_notified_at + advance guard.

import { createClient } from "https://esm.sh/@supabase/supabase-js@2.95.0";
import { requireInternalCaller } from "../_shared/auth.ts";

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers":
    "authorization, x-client-info, apikey, content-type, x-cron-secret",
};

const PRE_REMINDER_LEAD_DAYS = 2;

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: corsHeaders });

  const gate = await requireInternalCaller(req);
  if (!gate.ok) return gate.response;

  const supabase = createClient(
    Deno.env.get("SUPABASE_URL")!,
    Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
  );

  const now = new Date();
  const nowIso = now.toISOString();
  const preWindowEnd = new Date(now.getTime() + PRE_REMINDER_LEAD_DAYS * 86400_000).toISOString();

  // Batch-fetch pet names for plans we're about to notify, so the message can
  // address the pet by name (Butternut "X's box" copy parity). Cheap single
  // round-trip per tier; falls back to generic copy on any error.
  const fetchPetNames = async (petIds: string[]): Promise<Map<string, string>> => {
    const map = new Map<string, string>();
    const unique = [...new Set(petIds.filter(Boolean))];
    if (unique.length === 0) return map;
    try {
      const { data } = await supabase
        .from("pet_profiles")
        .select("id, name")
        .in("id", unique);
      for (const r of (data ?? []) as Array<{ id: string; name: string }>) {
        if (r.name?.trim()) map.set(r.id, r.name.trim());
      }
    } catch {/* generic copy fallback */}
    return map;
  };

  // ===== TIER 1: PRE-REMINDER =====
  // Plans where next_reminder_at falls inside (now, now + 2d].
  const { data: preDue, error: preErr } = await supabase
    .from("reorder_plans")
    .select("id, user_id, cadence_days, next_reminder_at, pet_profile_id")
    .eq("is_active", true)
    .gt("next_reminder_at", nowIso)
    .lte("next_reminder_at", preWindowEnd)
    .limit(500);

  let preNotified = 0;
  if (!preErr && (preDue ?? []).length > 0) {
    const preDueList = preDue ?? [];
    const prePetNames = await fetchPetNames(
      preDueList.map((p: any) => p.pet_profile_id).filter(Boolean) as string[],
    );

    // Batch dedup: fetch all pre-reminders for these plans since the most conservative lookback.
    const minCadence = Math.min(...preDueList.map((p: any) => Math.max(Number(p.cadence_days) - 1, 1)));
    const conservativeLookback = new Date(now.getTime() - minCadence * 86400_000).toISOString();
    const prePlanIds = preDueList.map((p: any) => p.id as string);
    const { data: existingPreNotifs } = await supabase
      .from("notifications")
      .select("reference_id, created_at")
      .eq("type", "reorder_pre_reminder")
      .in("reference_id", prePlanIds)
      .gte("created_at", conservativeLookback);
    const preLastNotified = new Map<string, number>();
    for (const n of existingPreNotifs ?? []) {
      const ts = new Date((n as any).created_at).getTime();
      const prev = preLastNotified.get((n as any).reference_id) ?? 0;
      if (ts > prev) preLastNotified.set((n as any).reference_id, ts);
    }

    // Filter plans that still need pre-reminder (per-plan lookback check in-memory).
    const nowMs = now.getTime();
    const toPreRemind = preDueList.filter((plan: any) => {
      const lookbackMs = Math.max(Number(plan.cadence_days) - 1, 1) * 86400_000;
      const lastTs = preLastNotified.get(plan.id as string) ?? 0;
      return lastTs < nowMs - lookbackMs;
    });

    if (toPreRemind.length > 0) {
      // Batch insert all pre-reminder notifications.
      const { error: batchPreErr } = await supabase.from("notifications").insert(
        toPreRemind.map((plan: any) => {
          const dueOn = new Date(plan.next_reminder_at).toLocaleDateString("uk-UA");
          const petName = plan.pet_profile_id ? prePetNames.get(plan.pet_profile_id as string) ?? null : null;
          const titleSuffix = petName ? ` для ${petName}` : "";
          return {
            user_id: plan.user_id,
            type: "reorder_pre_reminder",
            title: `Скоро час повторити${titleSuffix} 🐾`,
            message: petName
              ? `За ${PRE_REMINDER_LEAD_DAYS} дні (${dueOn}) нагадаємо оформити замовлення для ${petName}.`
              : `За ${PRE_REMINDER_LEAD_DAYS} дні (${dueOn}) ваш план постачання нагадає оформити нове замовлення.`,
            reference_id: plan.id,
          };
        }),
      );
      if (!batchPreErr) {
        preNotified = toPreRemind.length;
        const CRON_SECRET = Deno.env.get("CRON_SECRET") ?? "";
        await Promise.all(
          toPreRemind.map((plan: any) => {
            const petName = plan.pet_profile_id ? prePetNames.get(plan.pet_profile_id as string) ?? null : null;
            const titleSuffix = petName ? ` для ${petName}` : "";
            return supabase.functions.invoke("send-web-push", {
              body: {
                user_id: plan.user_id,
                title: `Скоро час повторити${titleSuffix} 🐾`,
                body: petName
                  ? `Через ${PRE_REMINDER_LEAD_DAYS} дні нагадаємо оформити замовлення для ${petName}. Хочете раніше?`
                  : `Через ${PRE_REMINDER_LEAD_DAYS} дні нагадаємо оформити замовлення. Хочете раніше?`,
                url: "/profile?tab=plans",
                tag: `reorder-pre-${plan.id}`,
                campaign: "reorder_pre_reminder",
                reference_id: plan.id,
              },
              headers: { "x-cron-secret": CRON_SECRET },
            }).catch(() => {});
          }),
        );
      }
    }
  }

  // ===== TIER 2: DUE REMINDER =====
  const { data: due, error } = await supabase
    .from("reorder_plans")
    .select("id, user_id, cadence_days, next_reminder_at, addons, pet_profile_id")
    .eq("is_active", true)
    .lte("next_reminder_at", nowIso)
    .limit(500);

  if (error) {
    return new Response(JSON.stringify({ error: error.message }), {
      status: 500,
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  }

  let notified = 0;
  const dueList = due ?? [];
  if (dueList.length > 0) {
    const duePetNames = await fetchPetNames(
      dueList.map((p: any) => p.pet_profile_id).filter(Boolean) as string[],
    );

    // Build per-plan data in-memory.
    type DuePlan = { plan: any; petName: string | null; titleSuffix: string; addonQty: number; addonNote: string };
    const duePlans: DuePlan[] = dueList.map((plan: any) => {
      const addonsArr = Array.isArray(plan.addons) ? plan.addons as Array<{ quantity: number }> : [];
      const addonQty = addonsArr.reduce((s: number, a: any) => s + (Number(a?.quantity) || 0), 0);
      const addonNote = addonQty > 0 ? ` У наступному боксі також додатки (${addonQty} шт), які ви запланували.` : "";
      const petName = plan.pet_profile_id ? duePetNames.get(plan.pet_profile_id as string) ?? null : null;
      const titleSuffix = petName ? ` для ${petName}` : "";
      return { plan, petName, titleSuffix, addonQty, addonNote };
    });

    // Batch insert all due notifications.
    const { error: batchDueErr } = await supabase.from("notifications").insert(
      duePlans.map(({ plan, petName, titleSuffix, addonNote }) => ({
        user_id: plan.user_id,
        type: "reorder_reminder",
        title: `Час повторити замовлення${titleSuffix} 🐾`,
        message: petName
          ? `Пора оформити нове замовлення улюблених ласощів для ${petName}.${addonNote}`
          : `Ваш план постачання нагадує: пора оформити нове замовлення улюблених ласощів.${addonNote}`,
        reference_id: plan.id,
      })),
    );

    if (!batchDueErr) {
      notified = dueList.length;
      const CRON_SECRET = Deno.env.get("CRON_SECRET") ?? "";
      const nowMs = now.getTime();

      // Parallel web pushes + plan schedule advances.
      await Promise.all([
        ...duePlans.map(({ plan, petName, titleSuffix, addonQty }) =>
          supabase.functions.invoke("send-web-push", {
            body: {
              user_id: plan.user_id,
              title: `Час повторити${titleSuffix} 🐾`,
              body: addonQty > 0
                ? (petName
                    ? `Улюблені ласощі ${petName} + ${addonQty} додаткових позицій чекають — оформіть в один клік.`
                    : `Ваші улюблені ласощі + ${addonQty} додаткових позицій чекають — оформіть в один клік.`)
                : (petName
                    ? `Улюблені ласощі ${petName} чекають — оформіть замовлення в один клік.`
                    : `Ваші улюблені ласощі чекають — оформіть замовлення в один клік.`),
              url: "/profile?tab=plans",
              tag: `reorder-due-${plan.id}`,
              campaign: "reorder_reminder",
              reference_id: plan.id,
            },
            headers: { "x-cron-secret": CRON_SECRET },
          }).catch(() => {}),
        ...duePlans.map(({ plan }) =>
          supabase.from("reorder_plans")
            .update({
              next_reminder_at: new Date(nowMs + Number(plan.cadence_days) * 86400_000).toISOString(),
              last_notified_at: nowIso,
            })
            .eq("id", plan.id)
            .catch(() => {}),
        ),
      ]);
    }
  }

  return new Response(
    JSON.stringify({
      ok: true,
      pre_processed: preDue?.length ?? 0,
      pre_notified: preNotified,
      due_processed: due?.length ?? 0,
      due_notified: notified,
    }),
    { headers: { ...corsHeaders, "Content-Type": "application/json" } },
  );
});
