-- Pablo AI Executive Layer — database migrations
-- Run in Supabase SQL Editor

-- ─── Executive Decisions ───────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.pablo_executive_decisions (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  agent             text NOT NULL,                    -- 'ceo' | 'cmo' | 'cfo' | 'coo' | 'analyst'
  decision_type     text NOT NULL,                    -- 'strategy' | 'alert' | 'recommendation' | 'action'
  title             text NOT NULL,
  summary           text NOT NULL,
  reasoning         text,                             -- Claude's chain of thought
  actions           jsonb DEFAULT '[]'::jsonb,        -- list of proposed actions
  risk_level        text DEFAULT 'low',               -- 'low' | 'medium' | 'high'
  approval_status   text DEFAULT 'auto_executed',     -- 'auto_executed' | 'pending' | 'approved' | 'rejected'
  approved_by       uuid REFERENCES auth.users(id),
  approved_at       timestamptz,
  rejection_reason  text,
  metrics_snapshot  jsonb DEFAULT '{}'::jsonb,        -- KPIs at time of decision
  impact_estimate   text,
  executed_at       timestamptz,
  created_at        timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_pablo_decisions_agent ON public.pablo_executive_decisions(agent);
CREATE INDEX IF NOT EXISTS idx_pablo_decisions_status ON public.pablo_executive_decisions(approval_status);
CREATE INDEX IF NOT EXISTS idx_pablo_decisions_created ON public.pablo_executive_decisions(created_at DESC);

-- ─── Approval Queue ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.pablo_approval_queue (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  decision_id     uuid REFERENCES public.pablo_executive_decisions(id) ON DELETE CASCADE,
  agent           text NOT NULL,
  action_type     text NOT NULL,
  title           text NOT NULL,
  description     text NOT NULL,
  risk_level      text NOT NULL DEFAULT 'medium',
  payload         jsonb DEFAULT '{}'::jsonb,          -- data needed to execute
  expires_at      timestamptz DEFAULT (now() + INTERVAL '48 hours'),
  status          text DEFAULT 'pending',             -- 'pending' | 'approved' | 'rejected' | 'expired'
  reviewed_by     uuid REFERENCES auth.users(id),
  reviewed_at     timestamptz,
  review_note     text,
  created_at      timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_pablo_approvals_status ON public.pablo_approval_queue(status);
CREATE INDEX IF NOT EXISTS idx_pablo_approvals_created ON public.pablo_approval_queue(created_at DESC);

-- ─── Executive Memory ──────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.pablo_executive_memory (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  agent         text NOT NULL,
  memory_type   text NOT NULL,     -- 'strategic_context' | 'customer_insight' | 'market_pattern'
  key           text NOT NULL,
  content       text NOT NULL,
  importance    integer DEFAULT 5 CHECK (importance BETWEEN 1 AND 10),
  valid_until   timestamptz,
  meta          jsonb DEFAULT '{}'::jsonb,
  created_at    timestamptz DEFAULT now(),
  updated_at    timestamptz DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_pablo_memory_agent_key ON public.pablo_executive_memory(agent, key);
CREATE INDEX IF NOT EXISTS idx_pablo_memory_type ON public.pablo_executive_memory(memory_type);

-- ─── Support Session Context ───────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.pablo_support_sessions (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  chat_id        bigint NOT NULL,
  customer_id    uuid REFERENCES public.customers(id) ON DELETE SET NULL,
  messages       jsonb DEFAULT '[]'::jsonb,           -- conversation history [{role, content}]
  context        jsonb DEFAULT '{}'::jsonb,            -- orders, profile, etc.
  last_active_at timestamptz DEFAULT now(),
  created_at     timestamptz DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_pablo_support_chat ON public.pablo_support_sessions(chat_id);
CREATE INDEX IF NOT EXISTS idx_pablo_support_active ON public.pablo_support_sessions(last_active_at DESC);

-- ─── Daily Briefings ──────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.pablo_briefings (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  briefing_type text NOT NULL DEFAULT 'morning',       -- 'morning' | 'weekly' | 'alert'
  title         text NOT NULL,
  content       text NOT NULL,                         -- full briefing text (Markdown)
  metrics       jsonb DEFAULT '{}'::jsonb,
  top_actions   jsonb DEFAULT '[]'::jsonb,
  sent_to_tg    boolean DEFAULT false,
  tg_chat_ids   bigint[] DEFAULT '{}',
  created_at    timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_pablo_briefings_created ON public.pablo_briefings(created_at DESC);

-- ─── RLS Policies ─────────────────────────────────────────────────────────
ALTER TABLE public.pablo_executive_decisions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.pablo_approval_queue ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.pablo_executive_memory ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.pablo_support_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.pablo_briefings ENABLE ROW LEVEL SECURITY;

-- Service role has full access (Pablo agents use service key)
CREATE POLICY "Service role full access" ON public.pablo_executive_decisions FOR ALL TO service_role USING (true) WITH CHECK (true);
CREATE POLICY "Service role full access" ON public.pablo_approval_queue FOR ALL TO service_role USING (true) WITH CHECK (true);
CREATE POLICY "Service role full access" ON public.pablo_executive_memory FOR ALL TO service_role USING (true) WITH CHECK (true);
CREATE POLICY "Service role full access" ON public.pablo_support_sessions FOR ALL TO service_role USING (true) WITH CHECK (true);
CREATE POLICY "Service role full access" ON public.pablo_briefings FOR ALL TO service_role USING (true) WITH CHECK (true);

-- Admins can read everything
CREATE POLICY "Admins can read decisions" ON public.pablo_executive_decisions FOR SELECT TO authenticated USING (public.has_role(auth.uid(), 'admin'::public.app_role));
CREATE POLICY "Admins can update approvals" ON public.pablo_approval_queue FOR ALL TO authenticated USING (public.has_role(auth.uid(), 'admin'::public.app_role)) WITH CHECK (public.has_role(auth.uid(), 'admin'::public.app_role));
CREATE POLICY "Admins can read memory" ON public.pablo_executive_memory FOR SELECT TO authenticated USING (public.has_role(auth.uid(), 'admin'::public.app_role));
CREATE POLICY "Admins can read briefings" ON public.pablo_briefings FOR SELECT TO authenticated USING (public.has_role(auth.uid(), 'admin'::public.app_role));

-- ─── Helper: get pending approvals count ──────────────────────────────────
CREATE OR REPLACE FUNCTION public.pablo_pending_count()
RETURNS integer
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT COUNT(*)::integer FROM public.pablo_approval_queue
  WHERE status = 'pending' AND expires_at > now();
$$;

GRANT EXECUTE ON FUNCTION public.pablo_pending_count() TO authenticated, service_role;

-- ─── Helper: expire stale approvals ───────────────────────────────────────
CREATE OR REPLACE FUNCTION public.pablo_expire_approvals()
RETURNS void
LANGUAGE sql SECURITY DEFINER SET search_path = public AS $$
  UPDATE public.pablo_approval_queue
  SET status = 'expired'
  WHERE status = 'pending' AND expires_at <= now();
$$;

GRANT EXECUTE ON FUNCTION public.pablo_expire_approvals() TO service_role;
