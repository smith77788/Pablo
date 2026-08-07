-- Guardian — child-safety module tables
-- Run in Supabase SQL Editor after 001/002.
--
-- Scope: evidence queue (metadata ONLY — never media) and moderation action
-- log for the Telegram bot's child-protection module.

-- ─── Evidence / report queue ────────────────────────────────────────────────
-- Stores ONLY metadata about suspected material. The application layer
-- (tools/child_safety_tools.py) enforces a field allowlist so the actual
-- media is never written here. Do not add binary/media columns to this table.
CREATE TABLE IF NOT EXISTS public.pablo_csam_reports (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  source_type       text NOT NULL,                     -- 'chat_message' | 'channel' | 'user_report'
  chat_id           text,                              -- chat/group id (as text; may be large)
  message_id        bigint,
  channel_username  text,
  channel_title     text,
  offender_user_id  bigint,
  offender_username text,
  text_snippet      text,                              -- short evidence excerpt (never media)
  signals           jsonb DEFAULT '{}'::jsonb,         -- scan_text() output
  reporter_note     text,                              -- note captured at record time
  reviewer_note     text,                              -- note added by human reviewer
  status            text NOT NULL DEFAULT 'pending_review',
                    -- 'pending_review' | 'confirmed' | 'dismissed' | 'submitted_to_authority'
  submitted_to      text,                              -- which authority the human filed with
  reviewed_at       timestamptz,
  created_at        timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_csam_reports_status ON public.pablo_csam_reports(status);
CREATE INDEX IF NOT EXISTS idx_csam_reports_created ON public.pablo_csam_reports(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_csam_reports_offender ON public.pablo_csam_reports(offender_user_id);

-- ─── Moderation action log ──────────────────────────────────────────────────
-- Audit trail of enforcement actions taken in chats the bot administers.
CREATE TABLE IF NOT EXISTS public.pablo_moderation_actions (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  chat_id           text,
  message_id        bigint,
  target_user_id    bigint,
  action            text NOT NULL,                     -- 'delete_and_ban' | 'delete' | 'ban'
  result            text,                              -- JSON string of API results
  created_at        timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_mod_actions_chat ON public.pablo_moderation_actions(chat_id);
CREATE INDEX IF NOT EXISTS idx_mod_actions_created ON public.pablo_moderation_actions(created_at DESC);

-- ─── Row-Level Security ─────────────────────────────────────────────────────
ALTER TABLE public.pablo_csam_reports        ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.pablo_moderation_actions  ENABLE ROW LEVEL SECURITY;

-- Service role (backend) has full access.
CREATE POLICY "Service role full access" ON public.pablo_csam_reports
  FOR ALL TO service_role USING (true) WITH CHECK (true);
CREATE POLICY "Service role full access" ON public.pablo_moderation_actions
  FOR ALL TO service_role USING (true) WITH CHECK (true);

-- Admins can read and triage the evidence queue.
CREATE POLICY "Admins can read reports" ON public.pablo_csam_reports
  FOR SELECT TO authenticated
  USING (public.has_role(auth.uid(), 'admin'::public.app_role));
CREATE POLICY "Admins can update reports" ON public.pablo_csam_reports
  FOR UPDATE TO authenticated
  USING (public.has_role(auth.uid(), 'admin'::public.app_role))
  WITH CHECK (public.has_role(auth.uid(), 'admin'::public.app_role));
CREATE POLICY "Admins can read mod actions" ON public.pablo_moderation_actions
  FOR SELECT TO authenticated
  USING (public.has_role(auth.uid(), 'admin'::public.app_role));

-- ─── Helper: pending review count ───────────────────────────────────────────
CREATE OR REPLACE FUNCTION public.pablo_pending_reports_count()
RETURNS integer
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT COUNT(*)::integer FROM public.pablo_csam_reports
  WHERE status = 'pending_review';
$$;

GRANT EXECUTE ON FUNCTION public.pablo_pending_reports_count() TO authenticated, service_role;
