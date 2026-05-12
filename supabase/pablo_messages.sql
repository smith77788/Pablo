-- Pablo AI agent message log
-- Run once in your Supabase SQL editor (or add to migrations)

CREATE TABLE IF NOT EXISTS public.pablo_messages (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id uuid REFERENCES public.customers(id) ON DELETE SET NULL,
  chat_id bigint,
  channel text NOT NULL,          -- 'telegram', 'email', 'web'
  direction text NOT NULL,        -- 'inbound', 'outbound'
  subject text,
  content text NOT NULL,
  is_resolved boolean NOT NULL DEFAULT false,
  agent_response text,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_pablo_messages_customer ON public.pablo_messages(customer_id);
CREATE INDEX IF NOT EXISTS idx_pablo_messages_channel ON public.pablo_messages(channel);
CREATE INDEX IF NOT EXISTS idx_pablo_messages_resolved ON public.pablo_messages(is_resolved) WHERE is_resolved = false;

-- Service role only (Pablo uses service key)
ALTER TABLE public.pablo_messages ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Service role full access"
  ON public.pablo_messages FOR ALL TO service_role
  USING (true) WITH CHECK (true);
