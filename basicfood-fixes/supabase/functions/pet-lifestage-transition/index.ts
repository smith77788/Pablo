// pet-lifestage-transition — daily cron.
//
// Butternut "Rex is now an adult — here's what changes" parity. Detects pets
// whose age (derived from date_of_birth) just crossed a life-stage boundary:
//   - 12 months: puppy/kitten → adult
//   - 84 months (7y): adult → senior
//
// For each transition we:
//   1. Update pet_profiles.age_months (so PortionCalculator stays accurate)
//   2. Insert notification type='pet_lifestage' (idempotent via dedupe)
//
// Dedupe via existing-count on (user_id, type, reference_id=pet_id, title-prefix).
// Stage detection uses BOTH the prior stored age_months AND the freshly computed
// one, so we only fire on the day the boundary is crossed.

import { createClient } from "https://esm.sh/@supabase/supabase-js@2.95.0";
import { requireInternalCaller } from "../_shared/auth.ts";

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers":
    "authorization, x-client-info, apikey, content-type, x-cron-secret",
};

type Pet = {
  id: string;
  user_id: string;
  name: string;
  species: "dog" | "cat";
  age_months: number | null;
  date_of_birth: string;
};

function ageMonthsFromDOB(dob: string): number {
  const d = new Date(dob);
  const now = new Date();
  return Math.max(
    0,
    (now.getUTCFullYear() - d.getUTCFullYear()) * 12 +
      (now.getUTCMonth() - d.getUTCMonth()) -
      (now.getUTCDate() < d.getUTCDate() ? 1 : 0),
  );
}

function stageOf(months: number): "puppy" | "adult" | "senior" {
  if (months < 12) return "puppy";
  if (months < 84) return "adult";
  return "senior";
}

const COPY: Record<string, { title: string; message: (name: string, species: "dog" | "cat") => string }> = {
  "puppy->adult": {
    title: "🎓 Перехід до дорослого віку",
    message: (n, s) =>
      `${n} вже дорослий — рекомендуємо переглянути порції (зазвичай ~10% денного раціону) та перейти на ${
        s === "cat" ? "котячі" : "дорослі"
      } позиції з твердішою текстурою.`,
  },
  "adult->senior": {
    title: "👑 Сеньйорський вік",
    message: (n) =>
      `${n} перейшов(ла) у сеньйорський вік. Радимо м'якіші субпродукти (легеня, печінка) і трохи менші порції — ми оновили калькулятор.`,
  },
};

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: corsHeaders });

  const gate = await requireInternalCaller(req);
  if (!gate.ok) return gate.response;

  const supabase = createClient(
    Deno.env.get("SUPABASE_URL")!,
    Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
  );

  const { data: pets, error } = await supabase
    .from("pet_profiles")
    .select("id, user_id, name, species, age_months, date_of_birth")
    .not("date_of_birth", "is", null)
    .limit(2000);

  if (error) {
    return new Response(JSON.stringify({ error: error.message }), {
      status: 500,
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  }

  let updated = 0, notified = 0, skipped = 0;

  // Pass 1: compute all age updates and transitions in-memory.
  type Transition = { pet: Pet; fresh: number; key: string; copy: typeof COPY[string] };
  const ageUpdates: Array<{ id: string; age_months: number }> = [];
  const transitions: Transition[] = [];

  for (const p of (pets ?? []) as Pet[]) {
    const fresh = ageMonthsFromDOB(p.date_of_birth);
    const prev = p.age_months ?? fresh;
    if (fresh !== p.age_months) ageUpdates.push({ id: p.id, age_months: fresh });
    const prevStage = stageOf(prev);
    const newStage  = stageOf(fresh);
    if (prevStage === newStage) { skipped++; continue; }
    const key = `${prevStage}->${newStage}`;
    const copy = COPY[key];
    if (!copy) { skipped++; continue; }
    transitions.push({ pet: p, fresh, key, copy });
  }

  // Parallel age updates.
  if (ageUpdates.length > 0) {
    const updateResults = await Promise.all(
      ageUpdates.map(({ id, age_months }) =>
        supabase.from("pet_profiles").update({ age_months }).eq("id", id).then(() => 1).catch(() => 0),
      ),
    );
    updated = updateResults.reduce((s, n) => s + n, 0);
  }

  // Batch dedup: check existing lifestage notifications for all transitioning pets.
  if (transitions.length > 0) {
    const transitionPetIds = transitions.map((t) => t.pet.id);
    const { data: existingNotifs } = await supabase
      .from("notifications")
      .select("reference_id, title")
      .eq("type", "pet_lifestage")
      .in("reference_id", transitionPetIds);
    const notifiedSet = new Set(
      (existingNotifs ?? []).map((n) => `${n.reference_id}:${(n.title as string).slice(0, 12)}`),
    );

    // Batch insert new notifications for non-deduped transitions.
    const toInsert = transitions.filter((t) => {
      const dedupeKey = `${t.pet.id}:${t.copy.title.slice(0, 12)}`;
      if (notifiedSet.has(dedupeKey)) { skipped++; return false; }
      return true;
    });

    if (toInsert.length > 0) {
      const { error } = await supabase.from("notifications").insert(
        toInsert.map(({ pet, copy }) => ({
          user_id: pet.user_id,
          type: "pet_lifestage",
          title: copy.title,
          message: copy.message(pet.name, pet.species),
          reference_id: pet.id,
        })),
      );
      if (!error) notified = toInsert.length;
      else skipped += toInsert.length;
    }
  }

  return new Response(
    JSON.stringify({ ok: true, scanned: pets?.length ?? 0, updated, notified, skipped }),
    { headers: { ...corsHeaders, "Content-Type": "application/json" } },
  );
});
