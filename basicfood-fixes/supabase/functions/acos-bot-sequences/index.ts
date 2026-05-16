// Cycle #21 — Multi-Touch Bot Sequences Runner
// 1) Seeds 2 default sequences (welcome_lead, second_order_nudge) if missing.
// 2) Auto-enrolls customers matching trigger_lifecycle.
// 3) Sends due steps via Telegram and advances enrollments.
import { createClient } from "https://esm.sh/@supabase/supabase-js@2.95.0";

import { requireInternalCaller } from "../_shared/auth.ts";
import { runAgent } from "../_shared/agent-logger.ts";
const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

async function tgSend(chatId: number, text: string): Promise<boolean> {
  const token = Deno.env.get("TELEGRAM_API_KEY");
  if (!token) return false;
  try {
    const res = await fetch(`https://api.telegram.org/bot${token}/sendMessage`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ chat_id: chatId, text, parse_mode: "HTML" }),
    });
    return res.ok;
  } catch { return false; }
}

async function ensureDefaults(supabase: any) {
  const { data: existing } = await supabase
    .from("bot_sequences")
    .select("id, name");
  const have = new Set((existing ?? []).map((s: any) => s.name));

  const seeds: { name: string; trigger_lifecycle: string; description: string; steps: { delay_hours: number; message_text: string; cta_url?: string }[] }[] = [
    {
      name: "welcome_lead",
      trigger_lifecycle: "lead",
      description: "Welcome series for fresh leads (no orders yet).",
      steps: [
        { delay_hours: 1,  message_text: "Привіт! 🐾 Дякуємо, що завітали до Basic Food. Ось 3 наших бестселери: https://basic-food.shop/catalog" },
        { delay_hours: 24, message_text: "Знижка -10% на перше замовлення з кодом <b>WELCOME10</b>. Дійсний 48 годин." },
        { delay_hours: 72, message_text: "Що корисного для вашого улюбленця? Дайте знати породу — ми порадимо корм." },
      ],
    },
    {
      name: "second_order_nudge",
      trigger_lifecycle: "active_one_order",
      description: "Nudges customers with exactly one order toward repeat purchase.",
      steps: [
        { delay_hours: 24, message_text: "Дякуємо за перше замовлення! 🐾 Як ваш улюбленець? Поділіться відгуком: https://basic-food.shop/reviews" },
        { delay_hours: 168, message_text: "Час поповнити запас? -7% на повторне замовлення з кодом <b>REPEAT7</b> (3 дні)." },
      ],
    },
  ];

  for (const seed of seeds) {
    if (have.has(seed.name)) continue;
    const { data: seq, error } = await supabase
      .from("bot_sequences")
      .insert({
        name: seed.name,
        description: seed.description,
        trigger_lifecycle: seed.trigger_lifecycle,
        is_active: true,
      })
      .select("id")
      .single();
    if (error || !seq) continue;
    await supabase.from("bot_sequence_steps").insert(
      seed.steps.map((s, i) => ({
        sequence_id: seq.id,
        step_index: i,
        delay_hours: s.delay_hours,
        message_text: s.message_text,
        cta_url: s.cta_url ?? null,
      })),
    );
  }
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response(null, { headers: corsHeaders });

  // SECURITY: gate against anonymous internet calls. Allowed callers:
  //   pg_cron (X-Cron-Secret), other edge functions (service-role JWT),
  //   admin / moderator users (signed-in JWT).
  const __gate = await requireInternalCaller(req);
  if (!__gate.ok) return __gate.response;

  return runAgent("acos-bot-sequences", req, null, async () => {
    const __res = await (async () => {

  try {
    const supabase = createClient(
      Deno.env.get("SUPABASE_URL")!,
      Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
    );

    await ensureDefaults(supabase);

    // ---- 1) Auto-enroll matching customers ----
    const { data: sequences } = await supabase
      .from("bot_sequences")
      .select("id, name, trigger_lifecycle")
      .eq("is_active", true);

    let enrolled = 0;
    const seqList = (sequences ?? []).filter((s: any) => s.trigger_lifecycle);
    if (seqList.length > 0) {
      // Fetch all TG customers + existing enrollments in one round-trip (instead of per-sequence)
      const [allCandidatesRes, existingEnrollmentsRes] = await Promise.all([
        supabase
          .from("customers")
          .select("id, telegram_chat_id, total_orders, lifecycle_stage")
          .not("telegram_chat_id", "is", null)
          .limit(500),
        supabase
          .from("bot_sequence_enrollments")
          .select("sequence_id, customer_id")
          .in("sequence_id", seqList.map((s: any) => s.id)),
      ]);
      const enrolledSet = new Set(
        (existingEnrollmentsRes.data ?? []).map((e: any) => `${e.sequence_id}:${e.customer_id}`),
      );
      const allCandidates = allCandidatesRes.data ?? [];
      const nowIso = new Date().toISOString();

      // Build all new enrollment rows in-memory
      const enrollmentRows: any[] = [];
      for (const seq of seqList) {
        const eligible = allCandidates.filter((c: any) => {
          if (enrolledSet.has(`${seq.id}:${c.id}`)) return false;
          if (seq.trigger_lifecycle === "lead") return (c.total_orders ?? 0) === 0;
          if (seq.trigger_lifecycle === "active_one_order") return (c.total_orders ?? 0) === 1;
          return c.lifecycle_stage === seq.trigger_lifecycle;
        });
        for (const c of eligible) {
          enrollmentRows.push({
            sequence_id: seq.id,
            customer_id: c.id,
            chat_id: c.telegram_chat_id,
            current_step: 0,
            next_send_at: nowIso,
            status: "active",
          });
        }
      }

      // Batch insert all new enrollments
      if (enrollmentRows.length > 0) {
        const { error } = await supabase.from("bot_sequence_enrollments").insert(enrollmentRows);
        if (!error) enrolled = enrollmentRows.length;
      }
    }

    // ---- 2) Send due steps ----
    const { data: due } = await supabase
      .from("bot_sequence_enrollments")
      .select("id, sequence_id, customer_id, chat_id, current_step")
      .eq("status", "active")
      .lte("next_send_at", new Date().toISOString())
      .limit(200);

    let sent = 0;
    let completed = 0;
    const dueList = due ?? [];
    if (dueList.length > 0) {
      // Prefetch all steps for the relevant sequences (2 queries instead of 2×N)
      const dueSeqIds = [...new Set(dueList.map((e: any) => e.sequence_id as string))];
      const { data: allSteps } = await supabase
        .from("bot_sequence_steps")
        .select("sequence_id, step_index, delay_hours, message_text, cta_url")
        .in("sequence_id", dueSeqIds);
      const stepMap = new Map<string, any>();
      for (const s of allSteps ?? []) {
        stepMap.set(`${s.sequence_id}:${s.step_index}`, s);
      }

      // Parallel TG sends + collect enrollment updates
      const nowMs = Date.now();
      type EnrUpdate = { id: string; update: object; isCompleted: boolean };
      const enrUpdates: EnrUpdate[] = [];

      const tgResults = await Promise.all(
        dueList.map(async (enr: any) => {
          const step = stepMap.get(`${enr.sequence_id}:${enr.current_step}`);
          if (!step) {
            enrUpdates.push({ id: enr.id, update: { status: "completed", completed_at: new Date().toISOString() }, isCompleted: true });
            return false;
          }
          let text = step.message_text;
          if (step.cta_url) text += `\n${step.cta_url}`;
          const ok = enr.chat_id ? await tgSend(Number(enr.chat_id), text) : false;
          const nextStep = stepMap.get(`${enr.sequence_id}:${enr.current_step + 1}`);
          if (nextStep) {
            const nextAt = new Date(nowMs + nextStep.delay_hours * 3600_000).toISOString();
            enrUpdates.push({ id: enr.id, update: { current_step: enr.current_step + 1, next_send_at: nextAt }, isCompleted: false });
          } else {
            enrUpdates.push({ id: enr.id, update: { status: "completed", completed_at: new Date().toISOString() }, isCompleted: true });
          }
          return ok;
        }),
      );

      // Apply enrollment updates in parallel
      await Promise.all(
        enrUpdates.map(({ id, update }) =>
          supabase.from("bot_sequence_enrollments").update(update).eq("id", id).catch(() => {}),
        ),
      );

      sent = tgResults.filter(Boolean).length;
      completed = enrUpdates.filter((u) => u.isCompleted).length;
    }

    // Update sequence aggregate counters (parallel per-sequence, 2 counts each)
    if (sequences?.length) {
      await Promise.all(
        (sequences as any[]).map(async (seq) => {
          const [enrolledRes, completedRes] = await Promise.all([
            supabase.from("bot_sequence_enrollments")
              .select("id", { count: "exact", head: true }).eq("sequence_id", seq.id),
            supabase.from("bot_sequence_enrollments")
              .select("id", { count: "exact", head: true }).eq("sequence_id", seq.id).eq("status", "completed"),
          ]);
          await supabase.from("bot_sequences").update({
            total_enrolled: enrolledRes.count ?? 0,
            total_completed: completedRes.count ?? 0,
          }).eq("id", seq.id).catch(() => {});
        }),
      );
    }

    return new Response(JSON.stringify({ ok: true, enrolled, sent, completed }), {
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  } catch (e: any) {
    console.error("bot-sequences error", e);
    return new Response(JSON.stringify({ error: String(e?.message ?? e) }), {
      status: 500,
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  }
    })();
    return { response: __res };
  });
});
