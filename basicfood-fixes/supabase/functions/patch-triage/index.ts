// PatchTriageAgent — auto-validates pending patches.
// Approves LOW-risk + high-confidence patches with valid target_files.
// Rejects empty/non-actionable or HIGH-risk patches.
import { createClient } from "https://esm.sh/@supabase/supabase-js@2.45.0";
import { requireInternalCaller } from "../_shared/auth.ts";

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response(null, { headers: corsHeaders });
  const gate = await requireInternalCaller(req);
  if (!gate.ok) return gate.response;

  const supabase = createClient(
    Deno.env.get("SUPABASE_URL")!,
    Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
  );

  const { data: pending } = await supabase
    .from("agent_pending_patches")
    .select("id, title, severity, regression_risk, ai_confidence, target_files, diff")
    .eq("status", "pending");

  let approved = 0, rejected = 0, kept = 0;

  // Classify patches synchronously (no DB calls needed)
  const reviewedAt = new Date().toISOString();
  interface PatchDecision { id: string; title: string; status: string; note: string }
  const toUpdate: PatchDecision[] = [];

  for (const p of pending ?? []) {
    const files = Array.isArray(p.target_files) ? p.target_files : [];
    const hasFiles = files.length > 0;
    const hasDiff = typeof p.diff === "string" && p.diff.trim().length > 0;
    const conf = Number(p.ai_confidence ?? 0);
    const risk = String(p.regression_risk ?? "").toUpperCase();

    let status = "pending", note = "";
    if (!hasFiles && !hasDiff) {
      status = "rejected";
      note = "Not actionable: no target_files and no diff provided.";
      rejected++;
    } else if (risk === "HIGH") {
      status = "rejected";
      note = "HIGH regression risk — requires human review.";
      rejected++;
    } else if (conf >= 0.85 && (risk === "LOW" || risk === "")) {
      status = "approved";
      note = `Auto-approved: confidence ${conf}, risk ${risk || "n/a"}.`;
      approved++;
    } else {
      kept++;
      continue;
    }
    toUpdate.push({ id: p.id, title: p.title, status, note });
  }

  // Parallel status updates
  await Promise.all(toUpdate.map(({ id, status, note }) =>
    supabase.from("agent_pending_patches").update({
      status,
      reviewed_at: reviewedAt,
      reviewed_by: "PatchTriageAgent",
      review_note: note,
    }).eq("id", id)
  ));
  const decisions = toUpdate.map(({ id, title, status, note }) => ({ id, title, status, note }));

  // Prune dead-weight synapses: confidence < 0.2 and older than 7 days, still proposed
  const cutoff = new Date(Date.now() - 7 * 24 * 60 * 60 * 1000).toISOString();
  const { data: dead } = await supabase
    .from("agent_synapses")
    .select("id")
    .eq("status", "proposed")
    .lt("confidence", 0.2)
    .lt("created_at", cutoff);

  let pruned = 0;
  if (dead && dead.length > 0) {
    const ids = dead.map((s) => s.id);
    await supabase.from("agent_synapses").update({ status: "pruned" }).in("id", ids);
    pruned = ids.length;
  }

  return new Response(
    JSON.stringify({ approved, rejected, kept, pruned, decisions }),
    { headers: { ...corsHeaders, "Content-Type": "application/json" } },
  );
});
