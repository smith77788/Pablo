--
-- PostgreSQL database dump
--

-- Dumped from database version 16.15 (Ubuntu 16.15-0ubuntu0.24.04.1)
-- Dumped by pg_dump version 16.15 (Ubuntu 16.15-0ubuntu0.24.04.1)

--
-- Name: _immunity_capture_status(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public._immunity_capture_status() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    IF NEW.acc_status IS DISTINCT FROM OLD.acc_status THEN
        INSERT INTO account_status_events
            (acc_id, owner_id, old_status, new_status, is_death, created_at)
        VALUES
            (NEW.id, NEW.owner_id, OLD.acc_status, NEW.acc_status,
             (NEW.acc_status IN ('banned','spamblock','deactivated','session_expired','frozen')),
             now());
    END IF;
    RETURN NEW;
END;
$$;

--
-- Name: account_capabilities; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.account_capabilities (
    account_id bigint NOT NULL,
    owner_id bigint NOT NULL,
    can_invite boolean DEFAULT true,
    can_dm boolean DEFAULT true,
    can_create_channel boolean DEFAULT true,
    can_create_bot boolean DEFAULT true,
    can_set_username boolean DEFAULT true,
    is_premium boolean DEFAULT false,
    has_2fa boolean DEFAULT false,
    daily_dm_limit integer DEFAULT 50,
    daily_invite_limit integer DEFAULT 200,
    last_discovery timestamp with time zone,
    restriction_flags text[],
    meta jsonb DEFAULT '{}'::jsonb
);

--
-- Name: account_daily_stats; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.account_daily_stats (
    account_id bigint NOT NULL,
    stat_date date DEFAULT CURRENT_DATE NOT NULL,
    actions_ok integer DEFAULT 0,
    actions_fail integer DEFAULT 0,
    flood_events integer DEFAULT 0,
    messages_sent integer DEFAULT 0,
    invites_ok integer DEFAULT 0,
    joins_ok integer DEFAULT 0
);

--
-- Name: account_flood_log; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.account_flood_log (
    id bigint NOT NULL,
    account_id bigint NOT NULL,
    operation text,
    flood_seconds integer,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    action_type text DEFAULT 'default'::text,
    consecutive_count integer DEFAULT 1,
    actual_wait integer,
    operation_id bigint
);

--
-- Name: account_flood_log_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.account_flood_log_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: account_flood_log_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.account_flood_log_id_seq OWNED BY public.account_flood_log.id;

--
-- Name: account_health_history; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.account_health_history (
    id bigint NOT NULL,
    account_id integer NOT NULL,
    owner_id bigint NOT NULL,
    health_score numeric(5,2) DEFAULT 0 NOT NULL,
    load_score numeric(5,2) DEFAULT 0 NOT NULL,
    trust_score numeric(5,4) DEFAULT 0 NOT NULL,
    flood_events_7d integer DEFAULT 0 NOT NULL,
    success_ops integer DEFAULT 0 NOT NULL,
    fail_ops integer DEFAULT 0 NOT NULL,
    warmup_state text DEFAULT 'raw'::text NOT NULL,
    suitability jsonb DEFAULT '{}'::jsonb,
    recorded_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: account_health_history_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.account_health_history_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: account_health_history_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.account_health_history_id_seq OWNED BY public.account_health_history.id;

--
-- Name: account_niche_profiles; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.account_niche_profiles (
    account_id bigint NOT NULL,
    owner_id bigint NOT NULL,
    niche text DEFAULT 'general'::text NOT NULL,
    profile_type text DEFAULT 'reader'::text NOT NULL,
    custom_channels text[] DEFAULT '{}'::text[],
    flood_wait_count integer DEFAULT 0 NOT NULL,
    last_flood_at timestamp with time zone,
    updated_at timestamp with time zone DEFAULT now()
);

--
-- Name: account_rehab_state; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.account_rehab_state (
    acc_id bigint NOT NULL,
    owner_id bigint NOT NULL,
    phase text DEFAULT 'appeal'::text NOT NULL,
    kind text,
    attempts integer DEFAULT 0 NOT NULL,
    appeal_count integer DEFAULT 0 NOT NULL,
    warm_cycles integer DEFAULT 0 NOT NULL,
    warm_actions integer DEFAULT 0 NOT NULL,
    note text,
    first_seen timestamp with time zone DEFAULT now() NOT NULL,
    last_action_at timestamp with time zone,
    next_action_at timestamp with time zone DEFAULT now() NOT NULL,
    freed_at timestamp with time zone,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    notified_phase text
);

--
-- Name: account_risk_scores; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.account_risk_scores (
    account_id bigint NOT NULL,
    risk_score double precision DEFAULT 0.0 NOT NULL,
    ban_probability double precision DEFAULT 0.0 NOT NULL,
    flood_rate_1h double precision DEFAULT 0.0 NOT NULL,
    ops_24h integer DEFAULT 0 NOT NULL,
    last_flood_at timestamp with time zone,
    computed_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: account_status_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.account_status_events (
    id bigint NOT NULL,
    acc_id bigint NOT NULL,
    owner_id bigint,
    old_status text,
    new_status text NOT NULL,
    is_death boolean DEFAULT false NOT NULL,
    reason text,
    source text,
    context jsonb,
    autopsy jsonb,
    signature text,
    processed_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: account_status_events_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.account_status_events_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: account_status_events_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.account_status_events_id_seq OWNED BY public.account_status_events.id;

--
-- Name: account_trust_history; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.account_trust_history (
    id bigint NOT NULL,
    account_id bigint NOT NULL,
    owner_id bigint NOT NULL,
    trust_score double precision NOT NULL,
    recorded_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: account_trust_history_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.account_trust_history_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: account_trust_history_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.account_trust_history_id_seq OWNED BY public.account_trust_history.id;

--
-- Name: account_usage_log; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.account_usage_log (
    id bigint NOT NULL,
    account_id bigint NOT NULL,
    operation_type text NOT NULL,
    success boolean DEFAULT true NOT NULL,
    duration_ms integer,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: account_usage_log_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.account_usage_log_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: account_usage_log_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.account_usage_log_id_seq OWNED BY public.account_usage_log.id;

--
-- Name: account_warmup_interests; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.account_warmup_interests (
    account_id bigint NOT NULL,
    channel_ref text NOT NULL,
    source text DEFAULT 'discovery'::text NOT NULL,
    seen_count integer DEFAULT 0 NOT NULL,
    found_at timestamp with time zone DEFAULT now() NOT NULL,
    last_seen_at timestamp with time zone
);

--
-- Name: account_warmup_log; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.account_warmup_log (
    id bigint NOT NULL,
    account_id bigint NOT NULL,
    action_type text NOT NULL,
    target text,
    success boolean NOT NULL,
    error text,
    performed_at timestamp with time zone DEFAULT now()
);

--
-- Name: account_warmup_log_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.account_warmup_log_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: account_warmup_log_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.account_warmup_log_id_seq OWNED BY public.account_warmup_log.id;

--
-- Name: account_warmup_plans; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.account_warmup_plans (
    id bigint NOT NULL,
    owner_id bigint NOT NULL,
    account_id bigint NOT NULL,
    plan_type text DEFAULT 'standard'::text NOT NULL,
    current_day integer DEFAULT 0,
    target_days integer DEFAULT 14,
    daily_actions integer DEFAULT 5,
    status text DEFAULT 'active'::text,
    started_at timestamp with time zone DEFAULT now(),
    completed_at timestamp with time zone,
    last_action_at timestamp with time zone,
    meta jsonb DEFAULT '{}'::jsonb,
    pause_reason text,
    pause_detail text,
    paused_at timestamp with time zone,
    pause_notified_at timestamp with time zone,
    last_skip_reason text,
    last_skip_at timestamp with time zone
);

--
-- Name: account_warmup_plans_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.account_warmup_plans_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: account_warmup_plans_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.account_warmup_plans_id_seq OWNED BY public.account_warmup_plans.id;

--
-- Name: activity_log; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.activity_log (
    id bigint NOT NULL,
    owner_id bigint NOT NULL,
    event_type text NOT NULL,
    action text NOT NULL,
    detail text,
    status text DEFAULT 'ok'::text NOT NULL,
    error_msg text,
    duration_ms integer,
    occurred_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: activity_log_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.activity_log_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: activity_log_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.activity_log_id_seq OWNED BY public.activity_log.id;

--
-- Name: ad_advertisers; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ad_advertisers (
    id bigint NOT NULL,
    owner_id bigint NOT NULL,
    advertiser_username character varying(128) NOT NULL,
    niche character varying(64),
    placements_count integer DEFAULT 0,
    first_seen_at timestamp with time zone DEFAULT now(),
    last_seen_at timestamp with time zone DEFAULT now()
);

--
-- Name: ad_advertisers_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.ad_advertisers_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: ad_advertisers_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.ad_advertisers_id_seq OWNED BY public.ad_advertisers.id;

--
-- Name: ad_placements; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ad_placements (
    id bigint NOT NULL,
    owner_id bigint NOT NULL,
    channel_username character varying(128) NOT NULL,
    channel_title character varying(256),
    subscribers integer DEFAULT 0,
    views_avg integer DEFAULT 0,
    er_rate double precision DEFAULT 0,
    ad_price_est integer DEFAULT 0,
    quality_score double precision DEFAULT 0,
    niches text[] DEFAULT ARRAY[]::text[],
    ad_posts_count integer DEFAULT 0,
    last_ad_seen_at timestamp with time zone,
    last_scanned_at timestamp with time zone DEFAULT now()
);

--
-- Name: ad_placements_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.ad_placements_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: ad_placements_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.ad_placements_id_seq OWNED BY public.ad_placements.id;

--
-- Name: ad_posts_log; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ad_posts_log (
    id bigint NOT NULL,
    placement_id bigint,
    owner_id bigint NOT NULL,
    advertiser_username character varying(128),
    post_text text,
    post_views integer DEFAULT 0,
    detected_at timestamp with time zone DEFAULT now()
);

--
-- Name: ad_posts_log_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.ad_posts_log_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: ad_posts_log_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.ad_posts_log_id_seq OWNED BY public.ad_posts_log.id;

--
-- Name: adaptive_pacing_log; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.adaptive_pacing_log (
    id bigint NOT NULL,
    owner_id bigint NOT NULL,
    operation_id bigint,
    base_delay double precision,
    actual_delay double precision,
    hour_of_day integer,
    day_of_week integer,
    success boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: adaptive_pacing_log_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.adaptive_pacing_log_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: adaptive_pacing_log_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.adaptive_pacing_log_id_seq OWNED BY public.adaptive_pacing_log.id;

--
-- Name: admin_audit_log; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.admin_audit_log (
    id bigint NOT NULL,
    admin_id bigint NOT NULL,
    action text NOT NULL,
    target_user_id bigint,
    details jsonb DEFAULT '{}'::jsonb,
    created_at timestamp with time zone DEFAULT now()
);

--
-- Name: admin_audit_log_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.admin_audit_log_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: admin_audit_log_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.admin_audit_log_id_seq OWNED BY public.admin_audit_log.id;

--
-- Name: admin_state; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.admin_state (
    admin_id bigint NOT NULL,
    state text NOT NULL,
    data text DEFAULT ''::text NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: ambassador_status; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ambassador_status (
    user_id bigint NOT NULL,
    tier_key text,
    total_commission numeric(12,4) DEFAULT 0,
    paid_commission numeric(12,4) DEFAULT 0,
    last_tier_up_at timestamp with time zone,
    updated_at timestamp with time zone DEFAULT now()
);

--
-- Name: ambassador_tiers; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ambassador_tiers (
    tier_key text NOT NULL,
    sort_order integer NOT NULL,
    tier_name text NOT NULL,
    tier_emoji text NOT NULL,
    min_active_refs integer DEFAULT 0 NOT NULL,
    min_paid_refs integer DEFAULT 0 NOT NULL,
    reward_days integer DEFAULT 0,
    reward_plan text DEFAULT 'starter'::text,
    commission_pct numeric(5,2) DEFAULT 0,
    badge_label text
);

--
-- Name: anomaly_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.anomaly_events (
    id bigint NOT NULL,
    owner_id bigint NOT NULL,
    anomaly_type text NOT NULL,
    detector text NOT NULL,
    severity text DEFAULT 'warning'::text NOT NULL,
    title text NOT NULL,
    description text DEFAULT ''::text NOT NULL,
    baseline_value double precision,
    anomaly_value double precision,
    deviation_pct double precision,
    affected_count integer DEFAULT 0 NOT NULL,
    target_ids jsonb DEFAULT '[]'::jsonb NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    triggered_recovery boolean DEFAULT false NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    detected_at timestamp with time zone DEFAULT now() NOT NULL,
    resolved_at timestamp with time zone
);

--
-- Name: anomaly_events_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.anomaly_events_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: anomaly_events_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.anomaly_events_id_seq OWNED BY public.anomaly_events.id;

--
-- Name: api_keys; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.api_keys (
    id bigint NOT NULL,
    user_id bigint NOT NULL,
    key_hash text NOT NULL,
    key_prefix text NOT NULL,
    name text DEFAULT 'Default'::text NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    last_used_at timestamp with time zone,
    requests_total bigint DEFAULT 0 NOT NULL
);

--
-- Name: api_keys_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.api_keys_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: api_keys_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.api_keys_id_seq OWNED BY public.api_keys.id;

--
-- Name: asset_templates; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.asset_templates (
    id bigint NOT NULL,
    owner_id bigint NOT NULL,
    asset_type text NOT NULL,
    name text NOT NULL,
    template jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: asset_templates_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.asset_templates_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: asset_templates_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.asset_templates_id_seq OWNED BY public.asset_templates.id;

--
-- Name: audience_dna; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.audience_dna (
    id bigint NOT NULL,
    bot_id bigint NOT NULL,
    owner_id bigint NOT NULL,
    peak_hours integer[] DEFAULT ARRAY[]::integer[],
    peak_days text[] DEFAULT ARRAY[]::text[],
    best_content_types text[] DEFAULT ARRAY[]::text[],
    avg_engagement_rate double precision DEFAULT 0,
    churn_risk_pct double precision DEFAULT 0,
    top_topics text[] DEFAULT ARRAY[]::text[],
    total_users_analyzed integer DEFAULT 0,
    computed_at timestamp with time zone DEFAULT now()
);

--
-- Name: audience_dna_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.audience_dna_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: audience_dna_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.audience_dna_id_seq OWNED BY public.audience_dna.id;

--
-- Name: audience_overlaps; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.audience_overlaps (
    id bigint NOT NULL,
    node_a bigint NOT NULL,
    node_b bigint NOT NULL,
    overlap_pct double precision DEFAULT 0.0 NOT NULL,
    shared_users integer DEFAULT 0 NOT NULL,
    computed_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT audience_overlaps_check CHECK ((node_a < node_b))
);

--
-- Name: audience_overlaps_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.audience_overlaps_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: audience_overlaps_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.audience_overlaps_id_seq OWNED BY public.audience_overlaps.id;

--
-- Name: auto_funnel_runs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.auto_funnel_runs (
    id bigint NOT NULL,
    funnel_id bigint NOT NULL,
    bot_id bigint NOT NULL,
    user_id bigint NOT NULL,
    next_step_num integer DEFAULT 1 NOT NULL,
    next_send_at timestamp with time zone NOT NULL,
    status text DEFAULT 'active'::text NOT NULL,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    last_sent_step integer,
    fail_count integer DEFAULT 0
);

--
-- Name: auto_funnel_runs_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.auto_funnel_runs_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: auto_funnel_runs_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.auto_funnel_runs_id_seq OWNED BY public.auto_funnel_runs.id;

--
-- Name: auto_funnel_steps; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.auto_funnel_steps (
    id bigint NOT NULL,
    funnel_id bigint NOT NULL,
    step_num integer NOT NULL,
    delay_hours integer DEFAULT 0 NOT NULL,
    message_text text NOT NULL,
    button_text text,
    button_url text
);

--
-- Name: auto_funnel_steps_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.auto_funnel_steps_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: auto_funnel_steps_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.auto_funnel_steps_id_seq OWNED BY public.auto_funnel_steps.id;

--
-- Name: auto_funnels; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.auto_funnels (
    id bigint NOT NULL,
    owner_id bigint NOT NULL,
    name text NOT NULL,
    bot_id bigint NOT NULL,
    target_segment text DEFAULT 'all'::text NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: auto_funnels_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.auto_funnels_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: auto_funnels_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.auto_funnels_id_seq OWNED BY public.auto_funnels.id;

--
-- Name: auto_replies; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.auto_replies (
    id integer NOT NULL,
    bot_id bigint NOT NULL,
    trigger_type text NOT NULL,
    keyword text,
    response_text text NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    match_mode text DEFAULT 'contains'::text,
    buttons jsonb,
    reply_delay_sec integer DEFAULT 0,
    active_from_hour integer,
    active_to_hour integer,
    priority integer DEFAULT 0,
    CONSTRAINT auto_replies_trigger_type_check CHECK ((trigger_type = ANY (ARRAY['start'::text, 'keyword'::text, 'any'::text])))
);

--
-- Name: auto_replies_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.auto_replies_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: auto_replies_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.auto_replies_id_seq OWNED BY public.auto_replies.id;

--
-- Name: auto_reply_log; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.auto_reply_log (
    id bigint NOT NULL,
    bot_id bigint NOT NULL,
    chat_id bigint NOT NULL,
    rule_id integer,
    rule_type text DEFAULT 'auto_reply'::text NOT NULL,
    trigger_type text,
    keyword text,
    fired_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: auto_reply_log_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.auto_reply_log_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: auto_reply_log_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.auto_reply_log_id_seq OWNED BY public.auto_reply_log.id;

--
-- Name: automation_rules; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.automation_rules (
    id integer NOT NULL,
    bot_id bigint NOT NULL,
    name text DEFAULT ''::text NOT NULL,
    trigger_type text NOT NULL,
    trigger_value text,
    action_type text NOT NULL,
    action_value text NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT automation_rules_action_type_check CHECK ((action_type = ANY (ARRAY['send_message'::text, 'add_tag'::text, 'remove_tag'::text, 'subscribe_funnel'::text]))),
    CONSTRAINT automation_rules_trigger_type_check CHECK ((trigger_type = ANY (ARRAY['message_received'::text, 'user_joined'::text, 'tag_added'::text, 'keyword'::text])))
);

--
-- Name: automation_rules_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.automation_rules_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: automation_rules_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.automation_rules_id_seq OWNED BY public.automation_rules.id;

--
-- Name: autoreg_device_profiles; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.autoreg_device_profiles (
    owner_id bigint NOT NULL,
    manufacturer text,
    app_version text,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: behavioral_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.behavioral_events (
    id bigint NOT NULL,
    owner_id bigint NOT NULL,
    entity_type text NOT NULL,
    entity_id bigint NOT NULL,
    event_type text NOT NULL,
    session_id text,
    meta jsonb DEFAULT '{}'::jsonb,
    occurred_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: behavioral_events_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.behavioral_events_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: behavioral_events_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.behavioral_events_id_seq OWNED BY public.behavioral_events.id;

--
-- Name: blocked_users; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.blocked_users (
    user_id bigint NOT NULL,
    blocked_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: bm_node_threads; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.bm_node_threads (
    id bigint NOT NULL,
    node_id bigint NOT NULL,
    tg_thread_id integer NOT NULL,
    entity_type character varying(32) NOT NULL,
    entity_id bigint NOT NULL,
    topic_name text DEFAULT ''::text NOT NULL,
    status character varying(16) DEFAULT 'open'::character varying NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: bm_node_threads_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.bm_node_threads_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: bm_node_threads_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.bm_node_threads_id_seq OWNED BY public.bm_node_threads.id;

--
-- Name: bm_telegram_nodes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.bm_telegram_nodes (
    id bigint NOT NULL,
    owner_id bigint NOT NULL,
    tg_chat_id bigint NOT NULL,
    node_type character varying(32) NOT NULL,
    name text DEFAULT ''::text NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: bm_telegram_nodes_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.bm_telegram_nodes_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: bm_telegram_nodes_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.bm_telegram_nodes_id_seq OWNED BY public.bm_telegram_nodes.id;

--
-- Name: booster_sessions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.booster_sessions (
    id integer NOT NULL,
    owner_id bigint NOT NULL,
    phone text NOT NULL,
    session_str text NOT NULL,
    proxy text,
    label text,
    status text DEFAULT 'active'::text NOT NULL,
    last_used_at timestamp with time zone,
    added_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT booster_sessions_status_check CHECK ((status = ANY (ARRAY['active'::text, 'invalid'::text, 'banned'::text])))
);

--
-- Name: booster_sessions_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.booster_sessions_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: booster_sessions_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.booster_sessions_id_seq OWNED BY public.booster_sessions.id;

--
-- Name: bot_admin_sessions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.bot_admin_sessions (
    bot_id bigint NOT NULL,
    owner_id bigint NOT NULL,
    token text NOT NULL,
    created_at timestamp with time zone DEFAULT now()
);

--
-- Name: bot_deep_links; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.bot_deep_links (
    id integer NOT NULL,
    bot_id bigint NOT NULL,
    name text NOT NULL,
    start_param text NOT NULL,
    click_count integer DEFAULT 0 NOT NULL,
    unique_users integer DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: bot_deep_links_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.bot_deep_links_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: bot_deep_links_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.bot_deep_links_id_seq OWNED BY public.bot_deep_links.id;

--
-- Name: bot_flood_config; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.bot_flood_config (
    bot_id bigint NOT NULL,
    owner_id bigint NOT NULL,
    mode text DEFAULT 'off'::text NOT NULL,
    threshold_per_min integer DEFAULT 30 NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: bot_flood_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.bot_flood_events (
    id bigint NOT NULL,
    bot_id bigint NOT NULL,
    owner_id bigint NOT NULL,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    last_at timestamp with time zone DEFAULT now() NOT NULL,
    peak_per_min integer DEFAULT 0 NOT NULL,
    suspected_count integer DEFAULT 0 NOT NULL,
    mode text DEFAULT ''::text NOT NULL,
    status text DEFAULT 'active'::text NOT NULL
);

--
-- Name: bot_flood_events_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.bot_flood_events_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: bot_flood_events_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.bot_flood_events_id_seq OWNED BY public.bot_flood_events.id;

--
-- Name: bot_mesh_hops; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.bot_mesh_hops (
    id bigint NOT NULL,
    task_id text NOT NULL,
    step integer NOT NULL,
    from_bot bigint,
    to_bot bigint,
    capability text,
    outcome text,
    reason text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: bot_mesh_hops_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.bot_mesh_hops_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: bot_mesh_hops_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.bot_mesh_hops_id_seq OWNED BY public.bot_mesh_hops.id;

--
-- Name: bot_mesh_tasks; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.bot_mesh_tasks (
    task_id text NOT NULL,
    owner_id bigint NOT NULL,
    origin_bot bigint,
    route jsonb DEFAULT '[]'::jsonb NOT NULL,
    step integer DEFAULT 0 NOT NULL,
    depth integer DEFAULT 0 NOT NULL,
    trace jsonb DEFAULT '[]'::jsonb NOT NULL,
    payload jsonb DEFAULT '{}'::jsonb NOT NULL,
    status text DEFAULT 'running'::text NOT NULL,
    drop_reason text,
    deadline_at timestamp with time zone NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: bot_metrics; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.bot_metrics (
    bot_id bigint NOT NULL,
    ctr double precision DEFAULT 0 NOT NULL,
    conversion_rate double precision DEFAULT 0 NOT NULL,
    retention_d1 double precision DEFAULT 0 NOT NULL,
    retention_d7 double precision DEFAULT 0 NOT NULL,
    score double precision DEFAULT 0 NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: bot_routing_weights; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.bot_routing_weights (
    id integer NOT NULL,
    bot_id bigint NOT NULL,
    weight numeric(10,2) DEFAULT 50.0 NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: bot_routing_weights_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.bot_routing_weights_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: bot_routing_weights_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.bot_routing_weights_id_seq OWNED BY public.bot_routing_weights.id;

--
-- Name: bot_sales_delivery; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.bot_sales_delivery (
    id bigint NOT NULL,
    persona_id bigint NOT NULL,
    owner_id bigint NOT NULL,
    name text DEFAULT ''::text NOT NULL,
    price_cents integer DEFAULT 0 NOT NULL,
    eta text DEFAULT ''::text NOT NULL,
    zones text DEFAULT ''::text NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: bot_sales_delivery_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.bot_sales_delivery_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: bot_sales_delivery_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.bot_sales_delivery_id_seq OWNED BY public.bot_sales_delivery.id;

--
-- Name: bot_sales_dialogs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.bot_sales_dialogs (
    id bigint NOT NULL,
    bot_id bigint NOT NULL,
    customer_chat_id bigint NOT NULL,
    owner_id bigint NOT NULL,
    persona_id bigint,
    stage text DEFAULT 'greeting'::text NOT NULL,
    prefs jsonb DEFAULT '{}'::jsonb NOT NULL,
    history jsonb DEFAULT '[]'::jsonb NOT NULL,
    summary text DEFAULT ''::text NOT NULL,
    handed_off boolean DEFAULT false NOT NULL,
    msg_count integer DEFAULT 0 NOT NULL,
    last_at timestamp with time zone DEFAULT now() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    followup_sent_at timestamp with time zone,
    age_confirmed boolean DEFAULT false NOT NULL
);

--
-- Name: bot_sales_dialogs_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.bot_sales_dialogs_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: bot_sales_dialogs_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.bot_sales_dialogs_id_seq OWNED BY public.bot_sales_dialogs.id;

--
-- Name: bot_sales_examples; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.bot_sales_examples (
    id bigint NOT NULL,
    persona_id bigint NOT NULL,
    owner_id bigint NOT NULL,
    user_msg text DEFAULT ''::text NOT NULL,
    assistant_msg text DEFAULT ''::text NOT NULL,
    ord integer DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: bot_sales_examples_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.bot_sales_examples_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: bot_sales_examples_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.bot_sales_examples_id_seq OWNED BY public.bot_sales_examples.id;

--
-- Name: bot_sales_faq; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.bot_sales_faq (
    id bigint NOT NULL,
    persona_id bigint NOT NULL,
    owner_id bigint NOT NULL,
    question text DEFAULT ''::text NOT NULL,
    answer text DEFAULT ''::text NOT NULL,
    keywords text DEFAULT ''::text NOT NULL,
    priority integer DEFAULT 0 NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: bot_sales_faq_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.bot_sales_faq_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: bot_sales_faq_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.bot_sales_faq_id_seq OWNED BY public.bot_sales_faq.id;

--
-- Name: bot_sales_orders; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.bot_sales_orders (
    id bigint NOT NULL,
    owner_id bigint NOT NULL,
    bot_id bigint NOT NULL,
    persona_id bigint,
    customer_chat_id bigint NOT NULL,
    customer_username text DEFAULT ''::text NOT NULL,
    customer_name text DEFAULT ''::text NOT NULL,
    items jsonb DEFAULT '[]'::jsonb NOT NULL,
    total_cents bigint DEFAULT 0 NOT NULL,
    currency text DEFAULT 'USD'::text NOT NULL,
    status text DEFAULT 'new'::text NOT NULL,
    contact jsonb DEFAULT '{}'::jsonb NOT NULL,
    note text DEFAULT ''::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    discount_cents integer DEFAULT 0 NOT NULL,
    promo_code text DEFAULT ''::text NOT NULL
);

--
-- Name: bot_sales_orders_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.bot_sales_orders_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: bot_sales_orders_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.bot_sales_orders_id_seq OWNED BY public.bot_sales_orders.id;

--
-- Name: bot_sales_personas; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.bot_sales_personas (
    id bigint NOT NULL,
    owner_id bigint NOT NULL,
    bot_id bigint,
    name text NOT NULL,
    role_title text DEFAULT 'менеджер по продажам'::text NOT NULL,
    gender text DEFAULT 'unspecified'::text NOT NULL,
    age integer,
    avatar_emoji text DEFAULT '🧑‍💼'::text NOT NULL,
    personality text DEFAULT ''::text NOT NULL,
    tone text DEFAULT 'friendly'::text NOT NULL,
    formality text DEFAULT 'auto'::text NOT NULL,
    humor_level integer DEFAULT 1 NOT NULL,
    emoji_level text DEFAULT 'medium'::text NOT NULL,
    msg_length text DEFAULT 'medium'::text NOT NULL,
    language text DEFAULT 'ru'::text NOT NULL,
    mirror_language boolean DEFAULT true NOT NULL,
    company_name text DEFAULT ''::text NOT NULL,
    company_about text DEFAULT ''::text NOT NULL,
    product_knowledge text DEFAULT ''::text NOT NULL,
    pricing_policy text DEFAULT ''::text NOT NULL,
    disclose_prices boolean DEFAULT true NOT NULL,
    currency text DEFAULT 'USD'::text NOT NULL,
    can_take_orders boolean DEFAULT true NOT NULL,
    can_consult boolean DEFAULT true NOT NULL,
    can_smalltalk boolean DEFAULT true NOT NULL,
    can_discuss_prefs boolean DEFAULT true NOT NULL,
    proactive_offers boolean DEFAULT true NOT NULL,
    smalltalk_topics text DEFAULT ''::text NOT NULL,
    taboo_topics text DEFAULT ''::text NOT NULL,
    operator_username text DEFAULT ''::text NOT NULL,
    operator_chat_id bigint,
    handoff_triggers text DEFAULT ''::text NOT NULL,
    handoff_message text DEFAULT 'Секунду, подключаю живого специалиста 🙌'::text NOT NULL,
    channels jsonb DEFAULT '[]'::jsonb NOT NULL,
    order_fields jsonb DEFAULT '["Имя", "Телефон", "Адрес доставки"]'::jsonb NOT NULL,
    order_confirm_message text DEFAULT 'Спасибо! Ваш заказ принят, скоро с вами свяжутся ✅'::text NOT NULL,
    greeting text DEFAULT ''::text NOT NULL,
    fallback text DEFAULT 'Дайте секунду, уточню и вернусь с ответом 🙌'::text NOT NULL,
    guardrails text DEFAULT ''::text NOT NULL,
    ai_provider text DEFAULT ''::text NOT NULL,
    model text DEFAULT ''::text NOT NULL,
    max_tokens integer DEFAULT 400 NOT NULL,
    temperature numeric(3,2) DEFAULT 0.7 NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    order_rules text DEFAULT ''::text NOT NULL,
    payment_details text DEFAULT ''::text NOT NULL,
    payment_via_operator boolean DEFAULT false NOT NULL,
    work_start text DEFAULT ''::text NOT NULL,
    work_end text DEFAULT ''::text NOT NULL,
    work_days text DEFAULT ''::text NOT NULL,
    tz_offset integer DEFAULT 0 NOT NULL,
    offhours_message text DEFAULT ''::text NOT NULL,
    sales_intensity text DEFAULT 'balanced'::text NOT NULL,
    scope_guard boolean DEFAULT false NOT NULL,
    greeting_by_time boolean DEFAULT false NOT NULL,
    min_order_total integer DEFAULT 0 NOT NULL,
    free_delivery_threshold integer DEFAULT 0 NOT NULL,
    followup_enabled boolean DEFAULT false NOT NULL,
    followup_delay_min integer DEFAULT 60 NOT NULL,
    followup_message text DEFAULT ''::text NOT NULL,
    rate_limit_per_min integer DEFAULT 0 NOT NULL,
    discount_max_percent integer DEFAULT 0 NOT NULL,
    require_age_confirm boolean DEFAULT false NOT NULL,
    age_confirm_message text DEFAULT ''::text NOT NULL,
    require_payment_proof boolean DEFAULT false NOT NULL,
    notify_channel_chat_id bigint
);

--
-- Name: bot_sales_personas_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.bot_sales_personas_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: bot_sales_personas_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.bot_sales_personas_id_seq OWNED BY public.bot_sales_personas.id;

--
-- Name: bot_sales_products; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.bot_sales_products (
    id bigint NOT NULL,
    persona_id bigint NOT NULL,
    owner_id bigint NOT NULL,
    name text NOT NULL,
    description text DEFAULT ''::text NOT NULL,
    sku text DEFAULT ''::text NOT NULL,
    price_cents bigint DEFAULT 0 NOT NULL,
    currency text DEFAULT 'USD'::text NOT NULL,
    in_stock boolean DEFAULT true NOT NULL,
    attributes jsonb DEFAULT '{}'::jsonb NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    min_qty integer DEFAULT 1 NOT NULL,
    unit text DEFAULT 'шт'::text NOT NULL,
    stock_qty integer,
    related_skus text DEFAULT ''::text NOT NULL,
    variants jsonb DEFAULT '[]'::jsonb NOT NULL
);

--
-- Name: bot_sales_products_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.bot_sales_products_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: bot_sales_products_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.bot_sales_products_id_seq OWNED BY public.bot_sales_products.id;

--
-- Name: bot_sales_promos; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.bot_sales_promos (
    id bigint NOT NULL,
    persona_id bigint NOT NULL,
    owner_id bigint NOT NULL,
    code text DEFAULT ''::text NOT NULL,
    percent integer DEFAULT 0 NOT NULL,
    min_total_cents integer DEFAULT 0 NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    expires_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: bot_sales_promos_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.bot_sales_promos_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: bot_sales_promos_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.bot_sales_promos_id_seq OWNED BY public.bot_sales_promos.id;

--
-- Name: bot_seo_suggestions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.bot_seo_suggestions (
    owner_id bigint NOT NULL,
    bot_id bigint NOT NULL,
    name text,
    short_desc text,
    reason text,
    keyword text,
    created_at timestamp with time zone DEFAULT now(),
    applied_at timestamp with time zone
);

--
-- Name: bot_update_offsets; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.bot_update_offsets (
    bot_id bigint NOT NULL,
    last_update_id bigint DEFAULT 0 NOT NULL
);

--
-- Name: bot_user_facts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.bot_user_facts (
    id bigint NOT NULL,
    bot_id bigint NOT NULL,
    user_id bigint NOT NULL,
    fact_key character varying(64) NOT NULL,
    fact_value text NOT NULL,
    confidence double precision DEFAULT 1.0,
    extracted_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now()
);

--
-- Name: bot_user_facts_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.bot_user_facts_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: bot_user_facts_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.bot_user_facts_id_seq OWNED BY public.bot_user_facts.id;

--
-- Name: bot_user_memory; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.bot_user_memory (
    id bigint NOT NULL,
    bot_id bigint NOT NULL,
    user_id bigint NOT NULL,
    role character varying(8) NOT NULL,
    text text NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb,
    created_at timestamp with time zone DEFAULT now()
);

--
-- Name: bot_user_memory_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.bot_user_memory_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: bot_user_memory_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.bot_user_memory_id_seq OWNED BY public.bot_user_memory.id;

--
-- Name: bot_users; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.bot_users (
    id integer NOT NULL,
    bot_id bigint NOT NULL,
    user_id bigint NOT NULL,
    username text,
    first_name text,
    last_name text,
    language_code text,
    first_seen timestamp with time zone DEFAULT now() NOT NULL,
    last_seen timestamp with time zone DEFAULT now() NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    is_blocked boolean DEFAULT false NOT NULL,
    phone text,
    suspect boolean DEFAULT false NOT NULL,
    flagged_at timestamp with time zone
);

--
-- Name: bot_users_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.bot_users_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: bot_users_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.bot_users_id_seq OWNED BY public.bot_users.id;

--
-- Name: bot_warehouse; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.bot_warehouse (
    id integer NOT NULL,
    owner_id bigint NOT NULL,
    bot_username text NOT NULL,
    bot_token_enc text,
    session_path text,
    account_id integer,
    proxy_id integer,
    status text DEFAULT 'aging'::text NOT NULL,
    registered_at timestamp with time zone,
    ready_at timestamp with time zone,
    current_subs integer DEFAULT 0,
    notes text DEFAULT ''::text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: bot_warehouse_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.bot_warehouse_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: bot_warehouse_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.bot_warehouse_id_seq OWNED BY public.bot_warehouse.id;

--
-- Name: botmother_memory; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.botmother_memory (
    id bigint NOT NULL,
    owner_id bigint NOT NULL,
    kind text DEFAULT 'note'::text NOT NULL,
    title text DEFAULT ''::text NOT NULL,
    body text NOT NULL,
    tags text[] DEFAULT ARRAY[]::text[] NOT NULL,
    source text DEFAULT 'manual'::text NOT NULL,
    pinned boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: botmother_memory_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.botmother_memory_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: botmother_memory_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.botmother_memory_id_seq OWNED BY public.botmother_memory.id;

--
-- Name: broadcast_campaign_ops; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.broadcast_campaign_ops (
    campaign_id bigint NOT NULL,
    op_id bigint NOT NULL,
    kind text NOT NULL,
    created_at timestamp with time zone DEFAULT now()
);

--
-- Name: broadcast_campaigns; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.broadcast_campaigns (
    id bigint NOT NULL,
    owner_id bigint NOT NULL,
    name text DEFAULT 'Кампания'::text NOT NULL,
    spec jsonb DEFAULT '{}'::jsonb NOT NULL,
    status text DEFAULT 'running'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now()
);

--
-- Name: broadcast_campaigns_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.broadcast_campaigns_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: broadcast_campaigns_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.broadcast_campaigns_id_seq OWNED BY public.broadcast_campaigns.id;

--
-- Name: broadcast_delivery_log; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.broadcast_delivery_log (
    broadcast_id bigint NOT NULL,
    user_id bigint NOT NULL,
    delivered_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: broadcasts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.broadcasts (
    id integer NOT NULL,
    bot_id bigint NOT NULL,
    message_text text NOT NULL,
    total_users integer DEFAULT 0 NOT NULL,
    sent_count integer DEFAULT 0 NOT NULL,
    failed_count integer DEFAULT 0 NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    created_by bigint NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    finished_at timestamp with time zone,
    photo_file_id text,
    target_user_ids jsonb,
    buttons jsonb,
    silent boolean DEFAULT false NOT NULL,
    delivery_log_pruned boolean DEFAULT false NOT NULL
);

--
-- Name: broadcasts_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.broadcasts_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: broadcasts_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.broadcasts_id_seq OWNED BY public.broadcasts.id;

--
-- Name: bulk_edit_snapshots; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.bulk_edit_snapshots (
    id integer NOT NULL,
    owner_id bigint NOT NULL,
    op_id bigint,
    entity_type text NOT NULL,
    target_id bigint NOT NULL,
    before jsonb DEFAULT '{}'::jsonb NOT NULL,
    reverted_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: bulk_edit_snapshots_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.bulk_edit_snapshots_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: bulk_edit_snapshots_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.bulk_edit_snapshots_id_seq OWNED BY public.bulk_edit_snapshots.id;

--
-- Name: business_connections; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.business_connections (
    connection_id text NOT NULL,
    owner_id bigint NOT NULL,
    user_chat_id bigint,
    can_reply boolean DEFAULT false NOT NULL,
    is_enabled boolean DEFAULT true NOT NULL,
    rights jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    notify_deleted boolean DEFAULT true NOT NULL,
    notify_edited boolean DEFAULT true NOT NULL,
    stale_notified_at timestamp with time zone
);

--
-- Name: cf_worker_pool; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.cf_worker_pool (
    id integer NOT NULL,
    owner_id bigint NOT NULL,
    worker_url text NOT NULL,
    region text DEFAULT 'auto'::text,
    status text DEFAULT 'active'::text,
    assigned_accounts integer DEFAULT 0,
    last_used_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now(),
    fail_streak integer DEFAULT 0 NOT NULL
);

--
-- Name: cf_worker_pool_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.cf_worker_pool_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: cf_worker_pool_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.cf_worker_pool_id_seq OWNED BY public.cf_worker_pool.id;

--
-- Name: channel_identity_history; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.channel_identity_history (
    id bigint NOT NULL,
    owner_id bigint NOT NULL,
    channel_id bigint NOT NULL,
    old_title text NOT NULL,
    new_title text NOT NULL,
    source text NOT NULL,
    operation_id bigint,
    reverted_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT channel_identity_history_new_title_check CHECK (((length(new_title) >= 1) AND (length(new_title) <= 128)))
);

--
-- Name: channel_identity_history_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.channel_identity_history_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: channel_identity_history_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.channel_identity_history_id_seq OWNED BY public.channel_identity_history.id;

--
-- Name: channel_member_history; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.channel_member_history (
    id bigint NOT NULL,
    owner_id bigint NOT NULL,
    channel_id bigint NOT NULL,
    members_count integer DEFAULT 0 NOT NULL,
    captured_on date DEFAULT CURRENT_DATE NOT NULL,
    captured_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: channel_member_history_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.channel_member_history_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: channel_member_history_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.channel_member_history_id_seq OWNED BY public.channel_member_history.id;

--
-- Name: channel_members; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.channel_members (
    channel_id bigint NOT NULL,
    user_id bigint NOT NULL,
    joined_at timestamp with time zone DEFAULT now()
);

--
-- Name: channel_search_rankings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.channel_search_rankings (
    id bigint NOT NULL,
    keyword_id bigint NOT NULL,
    channel_id bigint NOT NULL,
    "position" integer,
    checked_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: channel_search_rankings_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.channel_search_rankings_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: channel_search_rankings_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.channel_search_rankings_id_seq OWNED BY public.channel_search_rankings.id;

--
-- Name: channel_tracked_keywords; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.channel_tracked_keywords (
    id bigint NOT NULL,
    owner_id bigint NOT NULL,
    channel_id bigint NOT NULL,
    keyword text NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: channel_tracked_keywords_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.channel_tracked_keywords_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: channel_tracked_keywords_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.channel_tracked_keywords_id_seq OWNED BY public.channel_tracked_keywords.id;

--
-- Name: chat_invite_state; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.chat_invite_state (
    chat_key text NOT NULL,
    owner_id bigint NOT NULL,
    window_start timestamp with time zone,
    window_count integer DEFAULT 0 NOT NULL,
    paused_until timestamp with time zone,
    pause_reason text,
    invited_total integer DEFAULT 0 NOT NULL,
    joined_total integer DEFAULT 0 NOT NULL,
    left_total integer DEFAULT 0 NOT NULL,
    reported_total integer DEFAULT 0 NOT NULL,
    flood_hits integer DEFAULT 0 NOT NULL,
    liveness_score double precision,
    liveness_checked_at timestamp with time zone,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: chat_warmup_log; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.chat_warmup_log (
    id bigint NOT NULL,
    session_id integer NOT NULL,
    owner_id bigint NOT NULL,
    account_id bigint,
    kind text DEFAULT 'turn'::text NOT NULL,
    status text NOT NULL,
    reason text,
    text text,
    reply_to text,
    repeats integer DEFAULT 1 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: chat_warmup_log_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.chat_warmup_log_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: chat_warmup_log_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.chat_warmup_log_id_seq OWNED BY public.chat_warmup_log.id;

--
-- Name: chat_warmup_sessions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.chat_warmup_sessions (
    id integer NOT NULL,
    owner_id bigint NOT NULL,
    chat_ref text NOT NULL,
    chat_id bigint,
    account_ids bigint[] DEFAULT '{}'::bigint[] NOT NULL,
    mode text DEFAULT 'mixed'::text NOT NULL,
    topics text,
    intensity text DEFAULT 'normal'::text NOT NULL,
    status text DEFAULT 'active'::text NOT NULL,
    last_run_at timestamp with time zone,
    last_speaker bigint,
    last_seen_msg bigint DEFAULT 0 NOT NULL,
    messages_sent integer DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now()
);

--
-- Name: chat_warmup_sessions_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.chat_warmup_sessions_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: chat_warmup_sessions_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.chat_warmup_sessions_id_seq OWNED BY public.chat_warmup_sessions.id;

--
-- Name: chatlist_folders; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.chatlist_folders (
    id bigint NOT NULL,
    owner_id bigint NOT NULL,
    acc_id bigint,
    title text NOT NULL,
    chat_ids bigint[] DEFAULT '{}'::bigint[] NOT NULL,
    chat_count integer DEFAULT 0 NOT NULL,
    filter_id integer,
    invite_link text,
    invite_slug text,
    status text DEFAULT 'draft'::text NOT NULL,
    error text,
    instance_id bigint,
    join_count integer DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: chatlist_folders_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.chatlist_folders_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: chatlist_folders_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.chatlist_folders_id_seq OWNED BY public.chatlist_folders.id;

--
-- Name: clone_adapt_history; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.clone_adapt_history (
    id bigint NOT NULL,
    owner_id bigint NOT NULL,
    source_bot_id bigint NOT NULL,
    target_bot_id bigint NOT NULL,
    fields text NOT NULL,
    status text NOT NULL,
    details text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: clone_adapt_history_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.clone_adapt_history_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: clone_adapt_history_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.clone_adapt_history_id_seq OWNED BY public.clone_adapt_history.id;

--
-- Name: clusters; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.clusters (
    id integer NOT NULL,
    owner_id bigint NOT NULL,
    name text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: clusters_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.clusters_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: clusters_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.clusters_id_seq OWNED BY public.clusters.id;

--
-- Name: commission_ledger; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.commission_ledger (
    id bigint NOT NULL,
    referrer_id bigint NOT NULL,
    referred_id bigint NOT NULL,
    payment_amount numeric(10,4) NOT NULL,
    commission_pct numeric(5,2) NOT NULL,
    commission_usd numeric(10,4) NOT NULL,
    status text DEFAULT 'pending'::text,
    created_at timestamp with time zone DEFAULT now(),
    paid_at timestamp with time zone
);

--
-- Name: commission_ledger_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.commission_ledger_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: commission_ledger_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.commission_ledger_id_seq OWNED BY public.commission_ledger.id;

--
-- Name: community_channels; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.community_channels (
    id bigint NOT NULL,
    node_id bigint NOT NULL,
    tg_topic_id integer NOT NULL,
    name text DEFAULT ''::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: community_channels_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.community_channels_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: community_channels_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.community_channels_id_seq OWNED BY public.community_channels.id;

--
-- Name: community_node_members; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.community_node_members (
    id bigint NOT NULL,
    node_id bigint NOT NULL,
    account_id bigint NOT NULL,
    role character varying(16) DEFAULT 'member'::character varying NOT NULL,
    joined_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: community_node_members_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.community_node_members_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: community_node_members_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.community_node_members_id_seq OWNED BY public.community_node_members.id;

--
-- Name: community_nodes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.community_nodes (
    id bigint NOT NULL,
    owner_id bigint NOT NULL,
    tg_chat_id bigint NOT NULL,
    title text DEFAULT ''::text NOT NULL,
    description text DEFAULT ''::text NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: community_nodes_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.community_nodes_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: community_nodes_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.community_nodes_id_seq OWNED BY public.community_nodes.id;

--
-- Name: competitors; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.competitors (
    id integer NOT NULL,
    owner_id bigint NOT NULL,
    username text NOT NULL,
    label text,
    channel_id bigint,
    last_members integer,
    last_checked timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    prev_members integer
);

--
-- Name: competitors_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.competitors_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: competitors_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.competitors_id_seq OWNED BY public.competitors.id;

--
-- Name: compliance_audit; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.compliance_audit (
    id bigint NOT NULL,
    user_id bigint,
    account_id bigint,
    op_type text NOT NULL,
    op_id bigint,
    params_hash text,
    outcome text DEFAULT 'unknown'::text NOT NULL,
    hmac_sig text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: compliance_audit_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.compliance_audit_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: compliance_audit_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.compliance_audit_id_seq OWNED BY public.compliance_audit.id;

--
-- Name: contact_bulk_ops; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.contact_bulk_ops (
    id integer NOT NULL,
    owner_id bigint NOT NULL,
    op_type text NOT NULL,
    contact_ids uuid[] NOT NULL,
    params jsonb DEFAULT '{}'::jsonb,
    status text DEFAULT 'pending'::text,
    result jsonb DEFAULT '{}'::jsonb,
    started_at timestamp with time zone,
    finished_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now()
);

--
-- Name: contact_bulk_ops_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.contact_bulk_ops_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: contact_bulk_ops_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.contact_bulk_ops_id_seq OWNED BY public.contact_bulk_ops.id;

--
-- Name: contact_conflicts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.contact_conflicts (
    id integer NOT NULL,
    owner_id bigint NOT NULL,
    contact_id uuid NOT NULL,
    field_name text NOT NULL,
    source_a_id integer,
    value_a text,
    source_b_id integer,
    value_b text,
    resolution text DEFAULT 'pending'::text,
    resolved_value text,
    resolved_by bigint,
    resolved_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now()
);

--
-- Name: contact_conflicts_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.contact_conflicts_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: contact_conflicts_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.contact_conflicts_id_seq OWNED BY public.contact_conflicts.id;

--
-- Name: contact_crm; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.contact_crm (
    id integer NOT NULL,
    owner_id bigint NOT NULL,
    contact_id uuid NOT NULL,
    stage text DEFAULT 'lead'::text,
    deal_value real DEFAULT 0,
    currency text DEFAULT 'USD'::text,
    last_interaction_at timestamp with time zone,
    last_interaction_type text,
    last_message_preview text,
    next_reminder_at timestamp with time zone,
    next_reminder_text text,
    assigned_to bigint,
    custom_fields jsonb DEFAULT '{}'::jsonb,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now()
);

--
-- Name: contact_crm_activity; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.contact_crm_activity (
    id integer NOT NULL,
    owner_id bigint NOT NULL,
    contact_id uuid NOT NULL,
    activity_type text NOT NULL,
    description text,
    metadata jsonb DEFAULT '{}'::jsonb,
    created_at timestamp with time zone DEFAULT now()
);

--
-- Name: contact_crm_activity_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.contact_crm_activity_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: contact_crm_activity_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.contact_crm_activity_id_seq OWNED BY public.contact_crm_activity.id;

--
-- Name: contact_crm_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.contact_crm_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: contact_crm_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.contact_crm_id_seq OWNED BY public.contact_crm.id;

--
-- Name: contact_export_jobs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.contact_export_jobs (
    id integer NOT NULL,
    owner_id bigint NOT NULL,
    format text DEFAULT 'csv'::text NOT NULL,
    filter_params jsonb DEFAULT '{}'::jsonb,
    status text DEFAULT 'pending'::text,
    file_url text,
    record_count integer DEFAULT 0,
    created_at timestamp with time zone DEFAULT now(),
    finished_at timestamp with time zone
);

--
-- Name: contact_export_jobs_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.contact_export_jobs_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: contact_export_jobs_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.contact_export_jobs_id_seq OWNED BY public.contact_export_jobs.id;

--
-- Name: contact_group_members; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.contact_group_members (
    group_id bigint NOT NULL,
    contact_id uuid NOT NULL
);

--
-- Name: contact_groups; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.contact_groups (
    id bigint NOT NULL,
    owner_id bigint NOT NULL,
    name text NOT NULL,
    color text,
    created_at timestamp with time zone DEFAULT now()
);

--
-- Name: contact_groups_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.contact_groups_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: contact_groups_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.contact_groups_id_seq OWNED BY public.contact_groups.id;

--
-- Name: contact_history; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.contact_history (
    id bigint NOT NULL,
    contact_id uuid NOT NULL,
    owner_id bigint NOT NULL,
    action text NOT NULL,
    field_name text,
    old_value text,
    new_value text,
    source text DEFAULT 'user'::text,
    created_at timestamp with time zone DEFAULT now()
);

--
-- Name: contact_history_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.contact_history_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: contact_history_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.contact_history_id_seq OWNED BY public.contact_history.id;

--
-- Name: contact_identity_graph; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.contact_identity_graph (
    id integer NOT NULL,
    owner_id bigint NOT NULL,
    contact_id uuid NOT NULL,
    identity_type text NOT NULL,
    identity_value text NOT NULL,
    is_primary boolean DEFAULT false,
    confidence real DEFAULT 1.0,
    source text DEFAULT 'sync'::text,
    verified boolean DEFAULT false,
    created_at timestamp with time zone DEFAULT now()
);

--
-- Name: contact_identity_graph_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.contact_identity_graph_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: contact_identity_graph_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.contact_identity_graph_id_seq OWNED BY public.contact_identity_graph.id;

--
-- Name: contact_opt_out; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.contact_opt_out (
    owner_id bigint NOT NULL,
    target text NOT NULL,
    reason text,
    source text DEFAULT 'manual'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: contact_relationships; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.contact_relationships (
    id integer NOT NULL,
    owner_id bigint NOT NULL,
    contact_a_id uuid NOT NULL,
    contact_b_id uuid NOT NULL,
    relationship_type text DEFAULT 'mutual_group'::text NOT NULL,
    strength real DEFAULT 0.5,
    shared_groups text[],
    shared_channels text[],
    shared_accounts integer[],
    metadata jsonb DEFAULT '{}'::jsonb,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now()
);

--
-- Name: contact_relationships_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.contact_relationships_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: contact_relationships_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.contact_relationships_id_seq OWNED BY public.contact_relationships.id;

--
-- Name: contact_smart_tags; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.contact_smart_tags (
    id integer NOT NULL,
    owner_id bigint NOT NULL,
    contact_id uuid NOT NULL,
    tag text NOT NULL,
    source text DEFAULT 'rule'::text,
    confidence real DEFAULT 1.0,
    rule_id integer,
    applied_at timestamp with time zone DEFAULT now()
);

--
-- Name: contact_smart_tags_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.contact_smart_tags_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: contact_smart_tags_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.contact_smart_tags_id_seq OWNED BY public.contact_smart_tags.id;

--
-- Name: contact_sources; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.contact_sources (
    id bigint NOT NULL,
    contact_id uuid NOT NULL,
    account_id bigint NOT NULL,
    local_name text,
    first_seen_at timestamp with time zone DEFAULT now(),
    last_synced_at timestamp with time zone,
    status text DEFAULT 'active'::text
);

--
-- Name: contact_sources_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.contact_sources_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: contact_sources_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.contact_sources_id_seq OWNED BY public.contact_sources.id;

--
-- Name: contact_sync_log; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.contact_sync_log (
    id bigint NOT NULL,
    owner_id bigint NOT NULL,
    account_id bigint NOT NULL,
    sync_type text NOT NULL,
    contacts_synced integer DEFAULT 0,
    contacts_created integer DEFAULT 0,
    contacts_updated integer DEFAULT 0,
    contacts_merged integer DEFAULT 0,
    duration_ms integer,
    error_message text,
    started_at timestamp with time zone DEFAULT now(),
    finished_at timestamp with time zone
);

--
-- Name: contact_sync_log_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.contact_sync_log_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: contact_sync_log_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.contact_sync_log_id_seq OWNED BY public.contact_sync_log.id;

--
-- Name: contact_sync_state; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.contact_sync_state (
    id integer NOT NULL,
    owner_id bigint NOT NULL,
    account_id integer NOT NULL,
    last_full_sync_at timestamp with time zone,
    last_partial_sync_at timestamp with time zone,
    total_contacts integer DEFAULT 0,
    pending_changes integer DEFAULT 0,
    sync_status text DEFAULT 'idle'::text,
    last_error text,
    sync_config jsonb DEFAULT '{}'::jsonb,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now()
);

--
-- Name: contact_sync_state_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.contact_sync_state_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: contact_sync_state_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.contact_sync_state_id_seq OWNED BY public.contact_sync_state.id;

--
-- Name: contact_versions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.contact_versions (
    id integer NOT NULL,
    contact_id uuid NOT NULL,
    owner_id bigint NOT NULL,
    version_num integer NOT NULL,
    snapshot jsonb NOT NULL,
    changed_fields text[],
    changed_by text DEFAULT 'sync'::text,
    change_summary text,
    created_at timestamp with time zone DEFAULT now()
);

--
-- Name: contact_versions_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.contact_versions_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: contact_versions_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.contact_versions_id_seq OWNED BY public.contact_versions.id;

--
-- Name: content_meshes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.content_meshes (
    id bigint NOT NULL,
    owner_id bigint NOT NULL,
    name text NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    source_channel text,
    source_account_id bigint,
    last_post_id bigint DEFAULT 0,
    delay_minutes integer DEFAULT 30 NOT NULL,
    append_text text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: content_meshes_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.content_meshes_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: content_meshes_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.content_meshes_id_seq OWNED BY public.content_meshes.id;

--
-- Name: content_performance; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.content_performance (
    id bigint NOT NULL,
    bot_id bigint NOT NULL,
    message_id bigint,
    content_type character varying(32),
    views integer DEFAULT 0,
    reactions integer DEFAULT 0,
    forwards integer DEFAULT 0,
    replies integer DEFAULT 0,
    publish_hour integer,
    publish_weekday integer,
    engagement_rate double precision GENERATED ALWAYS AS (
CASE
    WHEN (views > 0) THEN (((reactions + forwards))::double precision / (views)::double precision)
    ELSE (0)::double precision
END) STORED,
    published_at timestamp with time zone DEFAULT now()
);

--
-- Name: content_performance_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.content_performance_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: content_performance_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.content_performance_id_seq OWNED BY public.content_performance.id;

--
-- Name: crm_activity; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.crm_activity (
    id integer NOT NULL,
    owner_id bigint NOT NULL,
    deal_id integer,
    note text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: crm_activity_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.crm_activity_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: crm_activity_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.crm_activity_id_seq OWNED BY public.crm_activity.id;

--
-- Name: crm_contacts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.crm_contacts (
    id integer NOT NULL,
    owner_id bigint NOT NULL,
    tg_user_id bigint NOT NULL,
    username text,
    first_name text,
    last_name text,
    phone text,
    tags text[] DEFAULT '{}'::text[],
    notes text,
    source text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: crm_contacts_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.crm_contacts_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: crm_contacts_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.crm_contacts_id_seq OWNED BY public.crm_contacts.id;

--
-- Name: crm_deals; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.crm_deals (
    id integer NOT NULL,
    owner_id bigint NOT NULL,
    title text NOT NULL,
    contact text,
    stage text DEFAULT 'new'::text NOT NULL,
    value numeric(14,2) DEFAULT 0,
    notes text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT crm_deals_stage_check CHECK ((stage = ANY (ARRAY['new'::text, 'contacted'::text, 'qualified'::text, 'won'::text, 'lost'::text])))
);

--
-- Name: crm_deals_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.crm_deals_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: crm_deals_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.crm_deals_id_seq OWNED BY public.crm_deals.id;

--
-- Name: crosspost_links; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.crosspost_links (
    id bigint NOT NULL,
    owner_id bigint NOT NULL,
    source_channel_id bigint NOT NULL,
    target_channel_id bigint NOT NULL,
    account_id bigint,
    enabled boolean DEFAULT true NOT NULL,
    last_msg_id bigint DEFAULT 0 NOT NULL,
    forwarded_total bigint DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    last_run_at timestamp with time zone
);

--
-- Name: crosspost_links_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.crosspost_links_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: crosspost_links_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.crosspost_links_id_seq OWNED BY public.crosspost_links.id;

--
-- Name: daughter_groups; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.daughter_groups (
    id bigint NOT NULL,
    owner_id bigint NOT NULL,
    mother_ref text NOT NULL,
    group_ref text NOT NULL,
    channel_id bigint NOT NULL,
    access_hash bigint DEFAULT 0 NOT NULL,
    creator_account_id bigint NOT NULL,
    status text DEFAULT 'active'::text NOT NULL,
    burn_reason text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    burned_at timestamp with time zone
);

--
-- Name: daughter_groups_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.daughter_groups_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: daughter_groups_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.daughter_groups_id_seq OWNED BY public.daughter_groups.id;

--
-- Name: deep_link_visits; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.deep_link_visits (
    id integer NOT NULL,
    link_id integer NOT NULL,
    user_id bigint NOT NULL,
    visited_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: deep_link_visits_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.deep_link_visits_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: deep_link_visits_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.deep_link_visits_id_seq OWNED BY public.deep_link_visits.id;

--
-- Name: deploy_log; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.deploy_log (
    id bigint NOT NULL,
    build text NOT NULL,
    commit_sha text,
    commit_msg text,
    branch text,
    notified boolean DEFAULT false,
    deployed_at timestamp with time zone DEFAULT now()
);

--
-- Name: deploy_log_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.deploy_log_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: deploy_log_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.deploy_log_id_seq OWNED BY public.deploy_log.id;

--
-- Name: device_pairings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.device_pairings (
    id bigint NOT NULL,
    owner_id bigint NOT NULL,
    code text,
    code_expires_at timestamp with time zone,
    token_fp text,
    device_label text DEFAULT ''::text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    paired_at timestamp with time zone,
    last_seen_at timestamp with time zone,
    revoked_at timestamp with time zone
);

--
-- Name: device_pairings_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.device_pairings_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: device_pairings_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.device_pairings_id_seq OWNED BY public.device_pairings.id;

--
-- Name: discovered_bots; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.discovered_bots (
    id bigint NOT NULL,
    owner_id bigint NOT NULL,
    acc_id bigint,
    username text NOT NULL,
    linked_bot_id bigint,
    first_seen timestamp with time zone DEFAULT now() NOT NULL,
    last_seen timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: discovered_bots_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.discovered_bots_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: discovered_bots_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.discovered_bots_id_seq OWNED BY public.discovered_bots.id;

--
-- Name: dm_campaign_log; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.dm_campaign_log (
    id bigint NOT NULL,
    campaign_id bigint NOT NULL,
    account_id integer,
    tg_user_id bigint NOT NULL,
    status text DEFAULT 'sent'::text,
    error_msg text,
    sent_at timestamp with time zone DEFAULT now()
);

--
-- Name: dm_campaign_log_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.dm_campaign_log_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: dm_campaign_log_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.dm_campaign_log_id_seq OWNED BY public.dm_campaign_log.id;

--
-- Name: dm_campaigns; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.dm_campaigns (
    id bigint NOT NULL,
    owner_id bigint NOT NULL,
    name text NOT NULL,
    text_template text NOT NULL,
    target_type text DEFAULT 'bot_users'::text NOT NULL,
    target_id bigint,
    status text DEFAULT 'draft'::text,
    sent_count integer DEFAULT 0,
    fail_count integer DEFAULT 0,
    total_targets integer DEFAULT 0,
    created_at timestamp with time zone DEFAULT now(),
    started_at timestamp with time zone,
    finished_at timestamp with time zone,
    params jsonb DEFAULT '{}'::jsonb
);

--
-- Name: dm_campaigns_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.dm_campaigns_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: dm_campaigns_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.dm_campaigns_id_seq OWNED BY public.dm_campaigns.id;

--
-- Name: ecosystem_bots; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ecosystem_bots (
    ecosystem_id bigint NOT NULL,
    bot_id bigint NOT NULL,
    added_at timestamp with time zone DEFAULT now()
);

--
-- Name: ecosystem_channels; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ecosystem_channels (
    ecosystem_id bigint NOT NULL,
    channel_id bigint NOT NULL,
    added_at timestamp with time zone DEFAULT now()
);

--
-- Name: ecosystem_dna; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ecosystem_dna (
    id bigint NOT NULL,
    owner_id bigint NOT NULL,
    name text NOT NULL,
    dna_type text NOT NULL,
    description text DEFAULT ''::text,
    template_data jsonb DEFAULT '{}'::jsonb NOT NULL,
    is_public boolean DEFAULT false,
    created_at timestamp with time zone DEFAULT now()
);

--
-- Name: ecosystem_dna_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.ecosystem_dna_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: ecosystem_dna_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.ecosystem_dna_id_seq OWNED BY public.ecosystem_dna.id;

--
-- Name: ecosystem_drift_log; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ecosystem_drift_log (
    id bigint NOT NULL,
    ecosystem_id bigint NOT NULL,
    owner_id bigint NOT NULL,
    drift_type text NOT NULL,
    object_type text,
    object_id bigint,
    description text NOT NULL,
    suggested_fix text,
    auto_fixable boolean DEFAULT false,
    resolved boolean DEFAULT false,
    resolved_at timestamp with time zone,
    detected_at timestamp with time zone DEFAULT now()
);

--
-- Name: ecosystem_drift_log_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.ecosystem_drift_log_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: ecosystem_drift_log_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.ecosystem_drift_log_id_seq OWNED BY public.ecosystem_drift_log.id;

--
-- Name: ecosystem_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ecosystem_events (
    id bigint NOT NULL,
    ecosystem_id bigint NOT NULL,
    owner_id bigint NOT NULL,
    event_type text NOT NULL,
    severity text DEFAULT 'info'::text,
    title text NOT NULL,
    details jsonb DEFAULT '{}'::jsonb,
    object_type text,
    object_id bigint,
    occurred_at timestamp with time zone DEFAULT now()
);

--
-- Name: ecosystem_events_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.ecosystem_events_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: ecosystem_events_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.ecosystem_events_id_seq OWNED BY public.ecosystem_events.id;

--
-- Name: ecosystem_global_presence; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ecosystem_global_presence (
    ecosystem_id bigint NOT NULL,
    plan_id bigint NOT NULL,
    added_at timestamp with time zone DEFAULT now()
);

--
-- Name: ecosystem_members; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ecosystem_members (
    id bigint NOT NULL,
    ecosystem_id bigint NOT NULL,
    owner_id bigint NOT NULL,
    object_type text NOT NULL,
    object_id bigint NOT NULL,
    role text DEFAULT 'member'::text,
    added_at timestamp with time zone DEFAULT now()
);

--
-- Name: ecosystem_members_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.ecosystem_members_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: ecosystem_members_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.ecosystem_members_id_seq OWNED BY public.ecosystem_members.id;

--
-- Name: ecosystems; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ecosystems (
    id bigint NOT NULL,
    owner_id bigint NOT NULL,
    name text NOT NULL,
    description text DEFAULT ''::text,
    ecosystem_type text DEFAULT 'custom'::text,
    status text DEFAULT 'active'::text,
    health_score double precision DEFAULT 1.0,
    stability_score double precision DEFAULT 1.0,
    reliability_score double precision DEFAULT 1.0,
    recovery_score double precision DEFAULT 1.0,
    growth_score double precision DEFAULT 0.0,
    pressure_score integer DEFAULT 0,
    risk_level text DEFAULT 'low'::text,
    region text,
    meta jsonb DEFAULT '{}'::jsonb,
    dna_id bigint,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now()
);

--
-- Name: ecosystems_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.ecosystems_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: ecosystems_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.ecosystems_id_seq OWNED BY public.ecosystems.id;

--
-- Name: entity_behavioral_score; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.entity_behavioral_score (
    owner_id bigint NOT NULL,
    entity_type text NOT NULL,
    entity_id bigint NOT NULL,
    attention_score numeric(5,2) DEFAULT 0 NOT NULL,
    habit_score numeric(5,2) DEFAULT 0 NOT NULL,
    ecosystem_score numeric(5,2) DEFAULT 0 NOT NULL,
    decay_rate numeric(5,4) DEFAULT 0 NOT NULL,
    last_reentry_at timestamp with time zone,
    reentry_count integer DEFAULT 0 NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: entity_follow_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.entity_follow_events (
    id bigint NOT NULL,
    follow_id bigint NOT NULL,
    owner_id bigint NOT NULL,
    entity_id bigint NOT NULL,
    change_type text NOT NULL,
    old_username text,
    new_username text,
    old_name text,
    new_name text,
    detected_at timestamp with time zone DEFAULT now() NOT NULL,
    notified boolean DEFAULT false NOT NULL
);

--
-- Name: entity_follow_events_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.entity_follow_events_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: entity_follow_events_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.entity_follow_events_id_seq OWNED BY public.entity_follow_events.id;

--
-- Name: entity_follows; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.entity_follows (
    id bigint NOT NULL,
    owner_id bigint NOT NULL,
    entity_id bigint NOT NULL,
    entity_type text NOT NULL,
    label text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    last_checked_at timestamp with time zone,
    CONSTRAINT entity_follows_entity_type_check CHECK ((entity_type = ANY (ARRAY['user'::text, 'bot'::text, 'channel'::text, 'group'::text, 'supergroup'::text])))
);

--
-- Name: entity_follows_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.entity_follows_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: entity_follows_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.entity_follows_id_seq OWNED BY public.entity_follows.id;

--
-- Name: entity_last_known; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.entity_last_known (
    entity_id bigint NOT NULL,
    entity_type text NOT NULL,
    username text,
    display_name text,
    first_seen_at timestamp with time zone DEFAULT now() NOT NULL,
    last_seen_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: entity_name_history; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.entity_name_history (
    id bigint NOT NULL,
    entity_id bigint NOT NULL,
    entity_type text NOT NULL,
    username text,
    display_name text,
    seen_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT entity_name_history_entity_type_check CHECK ((entity_type = ANY (ARRAY['user'::text, 'bot'::text, 'channel'::text, 'group'::text, 'supergroup'::text])))
);

--
-- Name: entity_name_history_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.entity_name_history_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: entity_name_history_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.entity_name_history_id_seq OWNED BY public.entity_name_history.id;

--
-- Name: entity_radar_stats; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.entity_radar_stats (
    entity_id bigint NOT NULL,
    entity_type text NOT NULL,
    first_seen_at timestamp with time zone NOT NULL,
    last_seen_at timestamp with time zone NOT NULL,
    distinct_chats integer DEFAULT 0 NOT NULL,
    total_sightings integer DEFAULT 0 NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: error_reports; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.error_reports (
    id integer NOT NULL,
    user_id bigint NOT NULL,
    description text NOT NULL,
    screenshot_id text,
    screenshot_path text,
    device_info jsonb,
    context jsonb,
    status character varying(20) DEFAULT 'new'::character varying NOT NULL,
    assignee_id bigint,
    notes text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: error_reports_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.error_reports_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: error_reports_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.error_reports_id_seq OWNED BY public.error_reports.id;

--
-- Name: executor_lease; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.executor_lease (
    id smallint DEFAULT 1 NOT NULL,
    worker_id text NOT NULL,
    role text DEFAULT 'all'::text NOT NULL,
    acquired_at timestamp with time zone DEFAULT now() NOT NULL,
    renewed_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    CONSTRAINT executor_lease_id_check CHECK ((id = 1))
);

--
-- Name: experiment_assignments; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.experiment_assignments (
    id integer NOT NULL,
    bot_id bigint NOT NULL,
    user_id bigint NOT NULL,
    experiment_id integer NOT NULL,
    variant_id integer NOT NULL,
    converted boolean DEFAULT false NOT NULL,
    assigned_at timestamp with time zone DEFAULT now() NOT NULL,
    converted_at timestamp with time zone
);

--
-- Name: experiment_assignments_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.experiment_assignments_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: experiment_assignments_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.experiment_assignments_id_seq OWNED BY public.experiment_assignments.id;

--
-- Name: experiment_variants; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.experiment_variants (
    id integer NOT NULL,
    experiment_id integer NOT NULL,
    name text NOT NULL,
    content text NOT NULL,
    weight integer DEFAULT 50 NOT NULL,
    impressions integer DEFAULT 0 NOT NULL,
    conversions integer DEFAULT 0 NOT NULL
);

--
-- Name: experiment_variants_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.experiment_variants_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: experiment_variants_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.experiment_variants_id_seq OWNED BY public.experiment_variants.id;

--
-- Name: experiments; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.experiments (
    id integer NOT NULL,
    bot_id bigint NOT NULL,
    name text NOT NULL,
    experiment_type text DEFAULT 'start_message'::text NOT NULL,
    status text DEFAULT 'draft'::text NOT NULL,
    winner_variant_id integer,
    min_sample_size integer DEFAULT 100 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    started_at timestamp with time zone,
    ended_at timestamp with time zone,
    CONSTRAINT experiments_experiment_type_check CHECK ((experiment_type = ANY (ARRAY['start_message'::text, 'auto_reply'::text, 'funnel'::text]))),
    CONSTRAINT experiments_status_check CHECK ((status = ANY (ARRAY['draft'::text, 'active'::text, 'paused'::text, 'completed'::text])))
);

--
-- Name: experiments_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.experiments_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: experiments_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.experiments_id_seq OWNED BY public.experiments.id;

--
-- Name: funnel_steps; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.funnel_steps (
    id integer NOT NULL,
    funnel_id integer NOT NULL,
    step_order integer NOT NULL,
    message_text text NOT NULL,
    delay_minutes integer DEFAULT 0 NOT NULL
);

--
-- Name: funnel_steps_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.funnel_steps_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: funnel_steps_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.funnel_steps_id_seq OWNED BY public.funnel_steps.id;

--
-- Name: funnel_subscriptions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.funnel_subscriptions (
    id bigint NOT NULL,
    funnel_id integer NOT NULL,
    user_id bigint NOT NULL,
    current_step integer DEFAULT 0 NOT NULL,
    next_send_at timestamp with time zone DEFAULT now() NOT NULL,
    completed boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    completed_at timestamp with time zone,
    conversion_recorded boolean DEFAULT false,
    dropped boolean DEFAULT false NOT NULL
);

--
-- Name: funnel_subscriptions_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.funnel_subscriptions_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: funnel_subscriptions_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.funnel_subscriptions_id_seq OWNED BY public.funnel_subscriptions.id;

--
-- Name: funnels; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.funnels (
    id integer NOT NULL,
    bot_id bigint NOT NULL,
    name text NOT NULL,
    trigger_type text NOT NULL,
    keyword text,
    is_active boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    entered_count integer DEFAULT 0 NOT NULL,
    completed_count integer DEFAULT 0 NOT NULL,
    dropped_count integer DEFAULT 0 NOT NULL,
    CONSTRAINT funnels_trigger_type_check CHECK ((trigger_type = ANY (ARRAY['start'::text, 'keyword'::text, 'join'::text])))
);

--
-- Name: funnels_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.funnels_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: funnels_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.funnels_id_seq OWNED BY public.funnels.id;

--
-- Name: ghost_action_log; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ghost_action_log (
    id bigint NOT NULL,
    ghost_profile_id bigint NOT NULL,
    account_id bigint NOT NULL,
    action_type text NOT NULL,
    target text,
    result text NOT NULL,
    error_msg text,
    executed_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: ghost_action_log_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.ghost_action_log_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: ghost_action_log_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.ghost_action_log_id_seq OWNED BY public.ghost_action_log.id;

--
-- Name: ghost_profiles; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ghost_profiles (
    id bigint NOT NULL,
    owner_id bigint NOT NULL,
    account_id bigint NOT NULL,
    personality text DEFAULT 'ghost'::text NOT NULL,
    active_hours_start integer DEFAULT 9 NOT NULL,
    active_hours_end integer DEFAULT 23 NOT NULL,
    daily_cap integer DEFAULT 8 NOT NULL,
    cooldown_minutes integer DEFAULT 60 NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: ghost_profiles_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.ghost_profiles_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: ghost_profiles_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.ghost_profiles_id_seq OWNED BY public.ghost_profiles.id;

--
-- Name: gift_inventory; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.gift_inventory (
    id bigint NOT NULL,
    owner_id bigint NOT NULL,
    account_id bigint NOT NULL,
    gift_id text NOT NULL,
    gift_type text NOT NULL,
    slug text,
    stars_cost integer,
    is_transferable boolean DEFAULT true,
    is_premium boolean DEFAULT false,
    is_unique boolean DEFAULT false,
    is_limited boolean DEFAULT false,
    limited_count integer,
    first_owner boolean DEFAULT false,
    generation integer,
    added_at timestamp with time zone DEFAULT now(),
    last_seen_at timestamp with time zone DEFAULT now()
);

--
-- Name: gift_inventory_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.gift_inventory_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: gift_inventory_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.gift_inventory_id_seq OWNED BY public.gift_inventory.id;

--
-- Name: gift_recipients; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.gift_recipients (
    id bigint NOT NULL,
    owner_id bigint NOT NULL,
    name text NOT NULL,
    username text,
    user_id bigint,
    is_main_admin boolean DEFAULT false,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now()
);

--
-- Name: gift_recipients_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.gift_recipients_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: gift_recipients_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.gift_recipients_id_seq OWNED BY public.gift_recipients.id;

--
-- Name: gift_transfer_items; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.gift_transfer_items (
    id bigint NOT NULL,
    plan_id bigint NOT NULL,
    inventory_id bigint NOT NULL,
    account_id bigint NOT NULL,
    gift_id text NOT NULL,
    gift_type text NOT NULL,
    stars_cost integer DEFAULT 0,
    status text DEFAULT 'pending'::text NOT NULL,
    error_message text,
    error_code text,
    is_retryable boolean DEFAULT true,
    retry_count integer DEFAULT 0,
    max_retries integer DEFAULT 3,
    transferred_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now()
);

--
-- Name: gift_transfer_items_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.gift_transfer_items_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: gift_transfer_items_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.gift_transfer_items_id_seq OWNED BY public.gift_transfer_items.id;

--
-- Name: gift_transfer_ops; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.gift_transfer_ops (
    id bigint NOT NULL,
    plan_id bigint NOT NULL,
    operation_id bigint NOT NULL,
    total_items integer DEFAULT 0,
    transferred integer DEFAULT 0,
    failed integer DEFAULT 0,
    skipped integer DEFAULT 0,
    pending_confirmation integer DEFAULT 0,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    completed_at timestamp with time zone
);

--
-- Name: gift_transfer_ops_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.gift_transfer_ops_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: gift_transfer_ops_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.gift_transfer_ops_id_seq OWNED BY public.gift_transfer_ops.id;

--
-- Name: gift_transfer_plans; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.gift_transfer_plans (
    id bigint NOT NULL,
    owner_id bigint NOT NULL,
    name text,
    recipient_username text,
    recipient_user_id bigint,
    recipient_name text,
    payment_source text NOT NULL,
    payment_method_id bigint,
    status text DEFAULT 'pending'::text NOT NULL,
    total_gifts integer DEFAULT 0,
    selected_gifts integer DEFAULT 0,
    estimated_cost bigint DEFAULT 0,
    actual_cost bigint DEFAULT 0,
    error_message text,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    completed_at timestamp with time zone
);

--
-- Name: gift_transfer_plans_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.gift_transfer_plans_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: gift_transfer_plans_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.gift_transfer_plans_id_seq OWNED BY public.gift_transfer_plans.id;

--
-- Name: gift_transfer_reports; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.gift_transfer_reports (
    id bigint NOT NULL,
    plan_id bigint NOT NULL,
    operation_id bigint,
    owner_id bigint NOT NULL,
    total_gifts_found integer DEFAULT 0,
    total_selected integer DEFAULT 0,
    transferred integer DEFAULT 0,
    failed integer DEFAULT 0,
    skipped integer DEFAULT 0,
    pending_confirmation integer DEFAULT 0,
    total_cost bigint DEFAULT 0,
    currency text DEFAULT 'stars'::text,
    recipient_username text,
    recipient_user_id bigint,
    recipient_name text,
    accounts_used integer DEFAULT 0,
    accounts_data jsonb,
    error_summary jsonb,
    retryable_failures jsonb,
    non_retryable jsonb,
    next_actions jsonb,
    created_at timestamp with time zone DEFAULT now(),
    completed_at timestamp with time zone
);

--
-- Name: gift_transfer_reports_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.gift_transfer_reports_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: gift_transfer_reports_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.gift_transfer_reports_id_seq OWNED BY public.gift_transfer_reports.id;

--
-- Name: global_presence_plans; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.global_presence_plans (
    id integer NOT NULL,
    owner_id bigint NOT NULL,
    asset_type text DEFAULT 'channel'::text NOT NULL,
    template_id integer,
    name_pattern text NOT NULL,
    username_pattern text,
    geo_selection jsonb DEFAULT '{}'::jsonb NOT NULL,
    account_selection jsonb DEFAULT '{}'::jsonb NOT NULL,
    safety_settings jsonb DEFAULT '{"safe_mode": true}'::jsonb NOT NULL,
    status text DEFAULT 'draft'::text NOT NULL,
    op_id integer,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    ecosystem_id bigint,
    project_name text,
    roles jsonb,
    levels jsonb,
    name_pool jsonb,
    username_pool jsonb,
    about_pool jsonb,
    avatar_style text,
    plan_seed bigint
);

--
-- Name: global_presence_plans_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.global_presence_plans_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: global_presence_plans_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.global_presence_plans_id_seq OWNED BY public.global_presence_plans.id;

--
-- Name: global_presence_targets; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.global_presence_targets (
    id integer NOT NULL,
    plan_id integer NOT NULL,
    country text,
    country_code text,
    region text,
    city text,
    city_slug text,
    language text,
    timezone text,
    asset_type text DEFAULT 'channel'::text NOT NULL,
    planned_name text,
    planned_username text,
    selected_account_id integer,
    status text DEFAULT 'pending'::text NOT NULL,
    result_asset_id bigint,
    error_message text,
    retryable boolean DEFAULT true,
    created_at timestamp with time zone DEFAULT now(),
    role text,
    level text,
    planned_about text,
    avatar_seed bigint,
    avatar_style text,
    final_username text,
    avatar_applied boolean DEFAULT false NOT NULL
);

--
-- Name: global_presence_targets_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.global_presence_targets_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: global_presence_targets_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.global_presence_targets_id_seq OWNED BY public.global_presence_targets.id;

--
-- Name: graph_edges; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.graph_edges (
    id bigint NOT NULL,
    from_node bigint NOT NULL,
    to_node bigint NOT NULL,
    edge_type text NOT NULL,
    weight double precision DEFAULT 1.0 NOT NULL,
    first_seen timestamp with time zone DEFAULT now() NOT NULL,
    last_seen timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: graph_edges_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.graph_edges_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: graph_edges_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.graph_edges_id_seq OWNED BY public.graph_edges.id;

--
-- Name: graph_nodes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.graph_nodes (
    id bigint NOT NULL,
    entity_id text NOT NULL,
    entity_type text DEFAULT 'channel'::text NOT NULL,
    title text,
    username text,
    member_count integer DEFAULT 0 NOT NULL,
    first_seen timestamp with time zone DEFAULT now() NOT NULL,
    last_seen timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: graph_nodes_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.graph_nodes_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: graph_nodes_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.graph_nodes_id_seq OWNED BY public.graph_nodes.id;

--
-- Name: growth_actions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.growth_actions (
    id bigint NOT NULL,
    goal_id bigint,
    owner_id bigint NOT NULL,
    action_type character varying(64),
    description text,
    outcome character varying(32),
    delta_value bigint DEFAULT 0,
    executed_at timestamp with time zone DEFAULT now()
);

--
-- Name: growth_actions_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.growth_actions_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: growth_actions_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.growth_actions_id_seq OWNED BY public.growth_actions.id;

--
-- Name: growth_content_seeds; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.growth_content_seeds (
    id integer NOT NULL,
    content_type text NOT NULL,
    title text NOT NULL,
    template text NOT NULL,
    deployed_count integer DEFAULT 0,
    is_active boolean DEFAULT true,
    created_at timestamp with time zone DEFAULT now()
);

--
-- Name: growth_content_seeds_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.growth_content_seeds_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: growth_content_seeds_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.growth_content_seeds_id_seq OWNED BY public.growth_content_seeds.id;

--
-- Name: growth_goals; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.growth_goals (
    id bigint NOT NULL,
    owner_id bigint NOT NULL,
    description text NOT NULL,
    target_metric character varying(64) NOT NULL,
    target_value bigint NOT NULL,
    current_value bigint DEFAULT 0,
    deadline_at timestamp with time zone,
    status character varying(20) DEFAULT 'active'::character varying,
    strategy character varying(32) DEFAULT 'balanced'::character varying,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    target_entity_type character varying(20) DEFAULT 'bot'::character varying,
    target_entity_id bigint,
    target_entity_label text
);

--
-- Name: growth_goals_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.growth_goals_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: growth_goals_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.growth_goals_id_seq OWNED BY public.growth_goals.id;

--
-- Name: growth_reports; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.growth_reports (
    id bigint NOT NULL,
    goal_id bigint,
    owner_id bigint NOT NULL,
    report_date date DEFAULT CURRENT_DATE,
    progress_pct double precision DEFAULT 0,
    actions_count integer DEFAULT 0,
    delta_value bigint DEFAULT 0,
    ai_commentary text,
    created_at timestamp with time zone DEFAULT now()
);

--
-- Name: growth_reports_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.growth_reports_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: growth_reports_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.growth_reports_id_seq OWNED BY public.growth_reports.id;

--
-- Name: growth_schedule; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.growth_schedule (
    id integer NOT NULL,
    user_id bigint NOT NULL,
    seed_id integer,
    interval_h integer DEFAULT 24 NOT NULL,
    next_run_at timestamp with time zone DEFAULT now() NOT NULL,
    last_run_at timestamp with time zone,
    total_sent integer DEFAULT 0,
    is_active boolean DEFAULT true,
    created_at timestamp with time zone DEFAULT now()
);

--
-- Name: growth_schedule_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.growth_schedule_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: growth_schedule_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.growth_schedule_id_seq OWNED BY public.growth_schedule.id;

--
-- Name: growth_settings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.growth_settings (
    user_id bigint NOT NULL,
    watermark_enabled boolean DEFAULT false,
    updated_at timestamp with time zone DEFAULT now()
);

--
-- Name: guard_captcha_pending; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.guard_captcha_pending (
    chat_id bigint NOT NULL,
    user_id bigint NOT NULL,
    captcha_msg_id bigint DEFAULT 0,
    expires_at timestamp with time zone NOT NULL,
    action text DEFAULT 'kick'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: guard_chats; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.guard_chats (
    id bigint NOT NULL,
    owner_id bigint NOT NULL,
    chat_id bigint NOT NULL,
    title text DEFAULT ''::text,
    username text DEFAULT ''::text,
    is_active boolean DEFAULT true NOT NULL,
    settings jsonb DEFAULT '{}'::jsonb NOT NULL,
    added_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: guard_chats_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.guard_chats_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: guard_chats_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.guard_chats_id_seq OWNED BY public.guard_chats.id;

--
-- Name: guard_warnings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.guard_warnings (
    chat_id bigint NOT NULL,
    user_id bigint NOT NULL,
    warns integer DEFAULT 0 NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: host_offerings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.host_offerings (
    id bigint NOT NULL,
    owner_id bigint NOT NULL,
    kind text DEFAULT 'server'::text NOT NULL,
    title text NOT NULL,
    description text,
    specs jsonb,
    region text,
    price_usd numeric(12,2) DEFAULT 0 NOT NULL,
    period text DEFAULT 'month'::text NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    allow_device_compute boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now()
);

--
-- Name: host_offerings_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.host_offerings_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: host_offerings_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.host_offerings_id_seq OWNED BY public.host_offerings.id;

--
-- Name: host_rentals; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.host_rentals (
    id bigint NOT NULL,
    offering_id bigint NOT NULL,
    tenant_id bigint NOT NULL,
    provider_id bigint NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    period text DEFAULT 'month'::text NOT NULL,
    units integer DEFAULT 1 NOT NULL,
    price_usd numeric(12,2) DEFAULT 0 NOT NULL,
    started_at timestamp with time zone,
    ends_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now()
);

--
-- Name: host_rentals_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.host_rentals_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: host_rentals_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.host_rentals_id_seq OWNED BY public.host_rentals.id;

--
-- Name: host_server_access; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.host_server_access (
    user_id bigint NOT NULL,
    purchased_at timestamp with time zone DEFAULT now(),
    payment_ref text,
    granted_by bigint
);

--
-- Name: immunity_policy; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.immunity_policy (
    id bigint NOT NULL,
    owner_id bigint NOT NULL,
    op_type text,
    signature text,
    action text NOT NULL,
    reason text NOT NULL,
    until timestamp with time zone NOT NULL,
    auto boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: immunity_policy_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.immunity_policy_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: immunity_policy_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.immunity_policy_id_seq OWNED BY public.immunity_policy.id;

--
-- Name: immunity_rules; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.immunity_rules (
    id bigint NOT NULL,
    owner_id bigint NOT NULL,
    match jsonb NOT NULL,
    action text DEFAULT 'throttle'::text NOT NULL,
    source text DEFAULT 'manual'::text NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    cooldown_sec integer DEFAULT 3600 NOT NULL,
    note text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: immunity_rules_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.immunity_rules_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: immunity_rules_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.immunity_rules_id_seq OWNED BY public.immunity_rules.id;

--
-- Name: immunity_signatures; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.immunity_signatures (
    signature text NOT NULL,
    owner_id bigint NOT NULL,
    deaths_1h integer DEFAULT 0 NOT NULL,
    deaths_24h integer DEFAULT 0 NOT NULL,
    exposures_24h integer DEFAULT 0 NOT NULL,
    death_rate numeric DEFAULT 0 NOT NULL,
    threat_level text DEFAULT 'calm'::text NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: inactivity_alerts_sent; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.inactivity_alerts_sent (
    id bigint NOT NULL,
    bot_id bigint NOT NULL,
    chat_id bigint NOT NULL,
    rule_id bigint NOT NULL,
    sent_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: inactivity_alerts_sent_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.inactivity_alerts_sent_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: inactivity_alerts_sent_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.inactivity_alerts_sent_id_seq OWNED BY public.inactivity_alerts_sent.id;

--
-- Name: infra_memory_accounts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.infra_memory_accounts (
    account_id bigint NOT NULL,
    action_type text NOT NULL,
    successes integer DEFAULT 0 NOT NULL,
    failures integer DEFAULT 0 NOT NULL,
    last_success_at timestamp with time zone,
    last_failure_at timestamp with time zone,
    last_errors text[] DEFAULT '{}'::text[] NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    avg_duration_s double precision DEFAULT 0 NOT NULL
);

--
-- Name: infra_memory_proxies; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.infra_memory_proxies (
    proxy_url text NOT NULL,
    action_type text NOT NULL,
    successes integer DEFAULT 0 NOT NULL,
    failures integer DEFAULT 0 NOT NULL,
    avg_latency_ms double precision DEFAULT 0 NOT NULL,
    last_success_at timestamp with time zone,
    last_failure_at timestamp with time zone,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: infra_pressure_cache; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.infra_pressure_cache (
    owner_id bigint NOT NULL,
    pressure_score integer DEFAULT 0 NOT NULL,
    breakdown text DEFAULT '{}'::text NOT NULL,
    computed_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: infrastructure_alerts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.infrastructure_alerts (
    id bigint NOT NULL,
    owner_id bigint NOT NULL,
    alert_type text DEFAULT 'general'::text NOT NULL,
    severity text DEFAULT 'warning'::text NOT NULL,
    title text DEFAULT ''::text NOT NULL,
    description text DEFAULT ''::text NOT NULL,
    target_type text,
    target_id bigint,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    first_seen_at timestamp with time zone DEFAULT now() NOT NULL,
    last_seen_at timestamp with time zone DEFAULT now() NOT NULL,
    resolved_at timestamp with time zone
);

--
-- Name: infrastructure_alerts_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.infrastructure_alerts_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: infrastructure_alerts_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.infrastructure_alerts_id_seq OWNED BY public.infrastructure_alerts.id;

--
-- Name: intent_operation_links; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.intent_operation_links (
    intent_id bigint NOT NULL,
    op_id bigint NOT NULL,
    created_at timestamp with time zone DEFAULT now()
);

--
-- Name: intents; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.intents (
    id bigint NOT NULL,
    owner_id bigint NOT NULL,
    intent_type text NOT NULL,
    description text NOT NULL,
    status text DEFAULT 'draft'::text NOT NULL,
    plan jsonb DEFAULT '{}'::jsonb NOT NULL,
    strategy text DEFAULT 'balanced'::text NOT NULL,
    forecast jsonb DEFAULT '{}'::jsonb NOT NULL,
    feedback jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now(),
    executed_at timestamp with time zone,
    completed_at timestamp with time zone
);

--
-- Name: intents_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.intents_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: intents_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.intents_id_seq OWNED BY public.intents.id;

--
-- Name: invite_overflow_channels; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.invite_overflow_channels (
    id bigint NOT NULL,
    owner_id bigint NOT NULL,
    chain_key text NOT NULL,
    channel_ref text NOT NULL,
    channel_id bigint,
    status text DEFAULT 'active'::text NOT NULL,
    prepared boolean DEFAULT false NOT NULL,
    invited_ok integer DEFAULT 0 NOT NULL,
    reason text DEFAULT ''::text NOT NULL,
    op_id bigint,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: invite_overflow_channels_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.invite_overflow_channels_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: invite_overflow_channels_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.invite_overflow_channels_id_seq OWNED BY public.invite_overflow_channels.id;

--
-- Name: invite_target_log; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.invite_target_log (
    owner_id bigint NOT NULL,
    group_key text NOT NULL,
    target text NOT NULL,
    op_id bigint,
    created_at timestamp with time zone DEFAULT now()
);

--
-- Name: keyword_hits; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.keyword_hits (
    id bigint NOT NULL,
    watcher_id bigint NOT NULL,
    owner_id bigint NOT NULL,
    chat_ref text,
    message_id bigint NOT NULL,
    from_user_id bigint,
    from_username text,
    matched_keyword text,
    text text,
    caught_at timestamp with time zone DEFAULT now(),
    delivered boolean DEFAULT false
);

--
-- Name: keyword_hits_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.keyword_hits_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: keyword_hits_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.keyword_hits_id_seq OWNED BY public.keyword_hits.id;

--
-- Name: keyword_stats; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.keyword_stats (
    id integer NOT NULL,
    bot_id bigint NOT NULL,
    keyword text NOT NULL,
    count integer DEFAULT 1 NOT NULL,
    last_seen timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: keyword_stats_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.keyword_stats_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: keyword_stats_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.keyword_stats_id_seq OWNED BY public.keyword_stats.id;

--
-- Name: keyword_watchers; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.keyword_watchers (
    id bigint NOT NULL,
    owner_id bigint NOT NULL,
    account_id bigint NOT NULL,
    chat_ref text NOT NULL,
    chat_title text,
    keywords jsonb DEFAULT '[]'::jsonb NOT NULL,
    status text DEFAULT 'active'::text NOT NULL,
    last_msg_id bigint DEFAULT 0,
    hits_count integer DEFAULT 0,
    created_at timestamp with time zone DEFAULT now(),
    last_checked_at timestamp with time zone,
    last_error text
);

--
-- Name: keyword_watchers_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.keyword_watchers_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: keyword_watchers_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.keyword_watchers_id_seq OWNED BY public.keyword_watchers.id;

--
-- Name: linked_platform_users; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.linked_platform_users (
    owner_id_a bigint NOT NULL,
    owner_id_b bigint NOT NULL,
    link_type text DEFAULT 'shared_phone'::text NOT NULL,
    linked_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT linked_platform_users_check CHECK ((owner_id_a < owner_id_b))
);

--
-- Name: llm_cooldowns; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.llm_cooldowns (
    key text NOT NULL,
    until timestamp with time zone NOT NULL,
    reason text DEFAULT ''::text NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: llm_usage; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.llm_usage (
    day date NOT NULL,
    provider text NOT NULL,
    calls integer DEFAULT 0 NOT NULL,
    ok integer DEFAULT 0 NOT NULL,
    limited integer DEFAULT 0 NOT NULL
);

--
-- Name: managed_bots; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.managed_bots (
    id integer NOT NULL,
    token text NOT NULL,
    bot_id bigint NOT NULL,
    username text,
    first_name text,
    is_active boolean DEFAULT true NOT NULL,
    added_by bigint NOT NULL,
    added_at timestamp with time zone DEFAULT now() NOT NULL,
    relay_enabled boolean DEFAULT false NOT NULL,
    bot_role text DEFAULT 'general'::text NOT NULL,
    cluster text DEFAULT 'default'::text,
    swarm_weight double precision DEFAULT 1.0 NOT NULL,
    swarm_enabled boolean DEFAULT false NOT NULL,
    note text,
    acc_id integer,
    created_via text DEFAULT 'manual'::text,
    last_error text,
    last_error_at timestamp with time zone,
    fail_streak integer DEFAULT 0 NOT NULL,
    last_ok_at timestamp with time zone,
    dead_notified_at timestamp with time zone,
    b2b_enabled boolean DEFAULT false NOT NULL,
    b2b_checked_at timestamp with time zone,
    profile_checked_at timestamp with time zone,
    use_webhook boolean DEFAULT false,
    deleted_at timestamp with time zone,
    extra_username text,
    extra_username_verified boolean DEFAULT false NOT NULL,
    extra_username_checked_at timestamp with time zone,
    CONSTRAINT managed_bots_bot_role_check CHECK ((bot_role = ANY (ARRAY['entry'::text, 'conversion'::text, 'retention'::text, 'general'::text])))
);

--
-- Name: managed_bots_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.managed_bots_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: managed_bots_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.managed_bots_id_seq OWNED BY public.managed_bots.id;

--
-- Name: managed_channels; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.managed_channels (
    id integer NOT NULL,
    owner_id bigint NOT NULL,
    acc_id integer NOT NULL,
    channel_id bigint NOT NULL,
    title text,
    username text,
    added_at timestamp with time zone DEFAULT now(),
    access_hash bigint DEFAULT 0 NOT NULL,
    about text,
    last_drift_check timestamp with time zone,
    type text,
    members_count integer DEFAULT 0 NOT NULL,
    avg_views integer DEFAULT 0 NOT NULL,
    influence numeric(8,2) DEFAULT 0 NOT NULL,
    is_admin boolean,
    is_creator boolean,
    gone_streak integer DEFAULT 0 NOT NULL
);

--
-- Name: managed_channels_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.managed_channels_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: managed_channels_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.managed_channels_id_seq OWNED BY public.managed_channels.id;

--
-- Name: memory_settings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.memory_settings (
    bot_id bigint NOT NULL,
    enabled boolean DEFAULT true,
    max_history_days integer DEFAULT 90,
    auto_extract_facts boolean DEFAULT true,
    updated_at timestamp with time zone DEFAULT now()
);

--
-- Name: mesh_queue; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.mesh_queue (
    id bigint NOT NULL,
    mesh_id bigint NOT NULL,
    target_id bigint NOT NULL,
    source_msg_id bigint NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    scheduled_at timestamp with time zone NOT NULL,
    sent_at timestamp with time zone,
    error_msg text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: mesh_queue_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.mesh_queue_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: mesh_queue_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.mesh_queue_id_seq OWNED BY public.mesh_queue.id;

--
-- Name: mesh_targets; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.mesh_targets (
    id bigint NOT NULL,
    mesh_id bigint NOT NULL,
    target_channel text NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    added_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: mesh_targets_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.mesh_targets_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: mesh_targets_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.mesh_targets_id_seq OWNED BY public.mesh_targets.id;

--
-- Name: message_templates; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.message_templates (
    id integer NOT NULL,
    owner_id bigint NOT NULL,
    name text NOT NULL,
    text text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: message_templates_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.message_templates_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: message_templates_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.message_templates_id_seq OWNED BY public.message_templates.id;

--
-- Name: mp_audit_log; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.mp_audit_log (
    id bigint NOT NULL,
    entity_type text NOT NULL,
    entity_id bigint,
    action text NOT NULL,
    actor_id bigint,
    meta jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: mp_audit_log_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.mp_audit_log_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: mp_audit_log_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.mp_audit_log_id_seq OWNED BY public.mp_audit_log.id;

--
-- Name: mp_provider_api_keys; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.mp_provider_api_keys (
    id bigint NOT NULL,
    provider_id bigint NOT NULL,
    key_prefix text NOT NULL,
    key_hash text NOT NULL,
    scopes text DEFAULT 'catalog,orders'::text NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    last_used_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    revoked_at timestamp with time zone
);

--
-- Name: mp_provider_api_keys_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.mp_provider_api_keys_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: mp_provider_api_keys_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.mp_provider_api_keys_id_seq OWNED BY public.mp_provider_api_keys.id;

--
-- Name: mp_providers; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.mp_providers (
    id bigint NOT NULL,
    owner_id bigint NOT NULL,
    slug text NOT NULL,
    name text NOT NULL,
    description text DEFAULT ''::text NOT NULL,
    category text DEFAULT 'other'::text NOT NULL,
    website text DEFAULT ''::text NOT NULL,
    contact text DEFAULT ''::text NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    verified_at timestamp with time zone,
    verified_by bigint,
    reject_reason text DEFAULT ''::text NOT NULL,
    rating numeric(3,2) DEFAULT 0 NOT NULL,
    success_rate numeric(5,2) DEFAULT 0 NOT NULL,
    total_orders integer DEFAULT 0 NOT NULL,
    api_base_url text DEFAULT ''::text NOT NULL,
    webhook_url text DEFAULT ''::text NOT NULL,
    webhook_secret_enc text DEFAULT ''::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: mp_providers_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.mp_providers_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: mp_providers_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.mp_providers_id_seq OWNED BY public.mp_providers.id;

--
-- Name: mp_services; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.mp_services (
    id bigint NOT NULL,
    provider_id bigint NOT NULL,
    external_id text DEFAULT ''::text NOT NULL,
    category text DEFAULT 'other'::text NOT NULL,
    resource_kind text DEFAULT ''::text NOT NULL,
    title text NOT NULL,
    description text DEFAULT ''::text NOT NULL,
    price_cents bigint DEFAULT 0 NOT NULL,
    currency text DEFAULT 'USD'::text NOT NULL,
    unit text DEFAULT 'unit'::text NOT NULL,
    min_qty integer DEFAULT 1 NOT NULL,
    max_qty integer DEFAULT 1000000 NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    stock integer DEFAULT '-1'::integer NOT NULL,
    sla_hours integer DEFAULT 24 NOT NULL,
    params_schema jsonb DEFAULT '[]'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: mp_services_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.mp_services_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: mp_services_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.mp_services_id_seq OWNED BY public.mp_services.id;

--
-- Name: narrative_campaigns; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.narrative_campaigns (
    id bigint NOT NULL,
    owner_id bigint NOT NULL,
    topic character varying(256) NOT NULL,
    core_message text NOT NULL,
    campaign_type character varying(32) DEFAULT 'trend'::character varying,
    spread_hours integer DEFAULT 4,
    posts_total integer DEFAULT 0,
    posts_published integer DEFAULT 0,
    status character varying(20) DEFAULT 'draft'::character varying,
    created_at timestamp with time zone DEFAULT now(),
    started_at timestamp with time zone,
    completed_at timestamp with time zone
);

--
-- Name: narrative_campaigns_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.narrative_campaigns_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: narrative_campaigns_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.narrative_campaigns_id_seq OWNED BY public.narrative_campaigns.id;

--
-- Name: narrative_posts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.narrative_posts (
    id bigint NOT NULL,
    campaign_id bigint,
    owner_id bigint NOT NULL,
    channel_username character varying(128) NOT NULL,
    bot_id bigint,
    angle character varying(64),
    content text NOT NULL,
    scheduled_at timestamp with time zone NOT NULL,
    published_at timestamp with time zone,
    status character varying(20) DEFAULT 'pending'::character varying,
    error_text text,
    channel_id bigint,
    acc_id integer
);

--
-- Name: narrative_posts_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.narrative_posts_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: narrative_posts_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.narrative_posts_id_seq OWNED BY public.narrative_posts.id;

--
-- Name: network_edges; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.network_edges (
    id integer NOT NULL,
    instance_id integer,
    source_node_id integer,
    target_node_id integer,
    edge_type text DEFAULT 'admin'::text NOT NULL,
    config jsonb DEFAULT '{}'::jsonb,
    created_at timestamp with time zone DEFAULT now()
);

--
-- Name: network_edges_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.network_edges_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: network_edges_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.network_edges_id_seq OWNED BY public.network_edges.id;

--
-- Name: network_instances; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.network_instances (
    id integer NOT NULL,
    owner_id bigint NOT NULL,
    template_id integer,
    name text NOT NULL,
    status text DEFAULT 'draft'::text,
    nodes jsonb DEFAULT '[]'::jsonb NOT NULL,
    edges jsonb DEFAULT '[]'::jsonb NOT NULL,
    stats jsonb DEFAULT '{}'::jsonb,
    created_at timestamp with time zone DEFAULT now(),
    launched_at timestamp with time zone,
    description text
);

--
-- Name: network_instances_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.network_instances_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: network_instances_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.network_instances_id_seq OWNED BY public.network_instances.id;

--
-- Name: network_nodes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.network_nodes (
    id integer NOT NULL,
    instance_id integer,
    node_type text NOT NULL,
    ref_id bigint,
    label text,
    config jsonb DEFAULT '{}'::jsonb,
    status text DEFAULT 'pending'::text,
    created_at timestamp with time zone DEFAULT now()
);

--
-- Name: network_nodes_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.network_nodes_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: network_nodes_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.network_nodes_id_seq OWNED BY public.network_nodes.id;

--
-- Name: network_templates; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.network_templates (
    id integer NOT NULL,
    owner_id bigint NOT NULL,
    name text NOT NULL,
    description text,
    template_type text DEFAULT 'channel_group'::text NOT NULL,
    nodes jsonb DEFAULT '[]'::jsonb NOT NULL,
    edges jsonb DEFAULT '[]'::jsonb NOT NULL,
    settings jsonb DEFAULT '{}'::jsonb,
    is_active boolean DEFAULT true,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now()
);

--
-- Name: network_templates_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.network_templates_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: network_templates_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.network_templates_id_seq OWNED BY public.network_templates.id;

--
-- Name: niche_growth_targets; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.niche_growth_targets (
    id bigint NOT NULL,
    owner_id bigint NOT NULL,
    group_id bigint NOT NULL,
    title text,
    joined_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: niche_growth_targets_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.niche_growth_targets_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: niche_growth_targets_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.niche_growth_targets_id_seq OWNED BY public.niche_growth_targets.id;

--
-- Name: notary_observations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.notary_observations (
    id bigint NOT NULL,
    watch_id bigint NOT NULL,
    observed_at timestamp with time zone DEFAULT now() NOT NULL,
    state text NOT NULL,
    views integer,
    acc_id bigint,
    geo text,
    detail text,
    sig text
);

--
-- Name: notary_observations_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.notary_observations_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: notary_observations_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.notary_observations_id_seq OWNED BY public.notary_observations.id;

--
-- Name: notary_watches; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.notary_watches (
    id bigint NOT NULL,
    owner_id bigint NOT NULL,
    channel_ref text NOT NULL,
    channel_title text,
    msg_id bigint,
    advertiser text,
    price_paid numeric(12,2),
    currency text DEFAULT 'USD'::text,
    promised_from timestamp with time zone DEFAULT now() NOT NULL,
    promised_until timestamp with time zone NOT NULL,
    status text DEFAULT 'active'::text NOT NULL,
    verdict text,
    first_seen_at timestamp with time zone,
    last_seen_at timestamp with time zone,
    absent_since timestamp with time zone,
    views_first integer,
    views_last integer,
    checks_done integer DEFAULT 0 NOT NULL,
    next_check_at timestamp with time zone DEFAULT now() NOT NULL,
    cert_sig text,
    cert_issued_at timestamp with time zone,
    note text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: notary_watches_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.notary_watches_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: notary_watches_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.notary_watches_id_seq OWNED BY public.notary_watches.id;

--
-- Name: notification_dedup; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.notification_dedup (
    user_id bigint NOT NULL,
    dedup_key text NOT NULL,
    last_sent timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: notification_settings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.notification_settings (
    user_id bigint NOT NULL,
    new_user boolean DEFAULT true NOT NULL,
    flood_warning boolean DEFAULT true NOT NULL,
    position_change boolean DEFAULT true NOT NULL,
    op_complete boolean DEFAULT true NOT NULL,
    restriction boolean DEFAULT true NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: observation_state; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.observation_state (
    keyword_id integer NOT NULL,
    entity_id text NOT NULL,
    account_id integer NOT NULL,
    last_rank integer,
    last_found boolean DEFAULT false NOT NULL,
    last_snapshot_id uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: onboarding_progress; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.onboarding_progress (
    owner_id bigint NOT NULL,
    activated_at timestamp with time zone DEFAULT now() NOT NULL,
    first_result_op_id bigint,
    first_result_op_type text,
    first_result_label text,
    first_result_ok integer
);

--
-- Name: op_circuit_breaker; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.op_circuit_breaker (
    owner_id bigint NOT NULL,
    failures integer DEFAULT 0 NOT NULL,
    tripped_at timestamp with time zone,
    cooldown_until timestamp with time zone,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: op_telemetry; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.op_telemetry (
    id bigint NOT NULL,
    account_id bigint NOT NULL,
    owner_id bigint,
    op_type text NOT NULL,
    outcome text NOT NULL,
    flood_wait_s integer DEFAULT 0 NOT NULL,
    duration_ms integer DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT op_telemetry_outcome_check CHECK ((outcome = ANY (ARRAY['success'::text, 'flood_wait'::text, 'ban'::text, 'error'::text])))
);

--
-- Name: op_telemetry_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.op_telemetry_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: op_telemetry_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.op_telemetry_id_seq OWNED BY public.op_telemetry.id;

--
-- Name: operation_audit; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.operation_audit (
    id bigint NOT NULL,
    owner_id bigint NOT NULL,
    operation_id bigint,
    account_id bigint,
    action text NOT NULL,
    target text,
    result text NOT NULL,
    error_msg text,
    flood_wait_s integer,
    duration_ms integer,
    occurred_at timestamp with time zone DEFAULT now()
);

--
-- Name: operation_audit_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.operation_audit_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: operation_audit_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.operation_audit_id_seq OWNED BY public.operation_audit.id;

--
-- Name: operation_log; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.operation_log (
    id integer NOT NULL,
    op_id integer NOT NULL,
    step_num integer NOT NULL,
    target text,
    status text,
    message text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: operation_log_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.operation_log_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: operation_log_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.operation_log_id_seq OWNED BY public.operation_log.id;

--
-- Name: operation_queue; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.operation_queue (
    id integer NOT NULL,
    owner_id bigint NOT NULL,
    op_type text NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    params jsonb DEFAULT '{}'::jsonb NOT NULL,
    result jsonb,
    total_items integer DEFAULT 0,
    done_items integer DEFAULT 0,
    error_msg text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    started_at timestamp with time zone,
    finished_at timestamp with time zone,
    scheduled_for timestamp with time zone,
    retry_count integer DEFAULT 0,
    max_retries integer DEFAULT 3,
    last_error text,
    requires_approval boolean DEFAULT false,
    approved_at timestamp with time zone,
    approved_by bigint,
    template_id integer,
    notified_at timestamp with time zone,
    label text,
    adaptive_delay double precision,
    hour_of_day integer,
    revive_count integer DEFAULT 0 NOT NULL,
    acct_wait_since timestamp with time zone,
    journal_pruned boolean DEFAULT false NOT NULL,
    pause_requested boolean DEFAULT false NOT NULL
);

--
-- Name: operation_queue_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.operation_queue_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: operation_queue_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.operation_queue_id_seq OWNED BY public.operation_queue.id;

--
-- Name: organism_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.organism_events (
    id bigint NOT NULL,
    owner_id bigint NOT NULL,
    kind text NOT NULL,
    payload jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: organism_events_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.organism_events_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: organism_events_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.organism_events_id_seq OWNED BY public.organism_events.id;

--
-- Name: organism_state; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.organism_state (
    owner_id bigint NOT NULL,
    key text NOT NULL,
    value jsonb DEFAULT '{}'::jsonb NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: parsed_audiences; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.parsed_audiences (
    id bigint NOT NULL,
    owner_id bigint NOT NULL,
    source_type text NOT NULL,
    source_id bigint,
    source_title text,
    source_username text,
    parse_run_id bigint,
    tg_user_id bigint NOT NULL,
    username text,
    first_name text,
    last_name text,
    phone text,
    is_premium boolean DEFAULT false,
    is_bot boolean DEFAULT false,
    is_active boolean,
    last_seen_days integer,
    geo_country text,
    geo_city text,
    parsed_at timestamp with time zone DEFAULT now(),
    meta jsonb DEFAULT '{}'::jsonb,
    gender text
);

--
-- Name: parsed_audiences_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.parsed_audiences_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: parsed_audiences_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.parsed_audiences_id_seq OWNED BY public.parsed_audiences.id;

--
-- Name: parser_runs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.parser_runs (
    id bigint NOT NULL,
    owner_id bigint NOT NULL,
    source_type text NOT NULL,
    source_ref text NOT NULL,
    source_id bigint,
    parse_type text NOT NULL,
    account_id bigint,
    status text DEFAULT 'pending'::text,
    total_found integer DEFAULT 0,
    total_saved integer DEFAULT 0,
    total_skipped integer DEFAULT 0,
    started_at timestamp with time zone DEFAULT now(),
    finished_at timestamp with time zone,
    error text,
    meta jsonb DEFAULT '{}'::jsonb
);

--
-- Name: parser_runs_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.parser_runs_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: parser_runs_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.parser_runs_id_seq OWNED BY public.parser_runs.id;

--
-- Name: payments; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.payments (
    id integer NOT NULL,
    user_id bigint NOT NULL,
    plan text NOT NULL,
    period_months integer DEFAULT 1 NOT NULL,
    currency text NOT NULL,
    amount_crypto numeric(18,6) NOT NULL,
    amount_usd numeric(10,2) NOT NULL,
    wallet_address text NOT NULL,
    tx_hash text,
    status text DEFAULT 'pending'::text NOT NULL,
    reference text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone DEFAULT (now() + '00:30:00'::interval) NOT NULL,
    confirmed_at timestamp with time zone,
    CONSTRAINT payments_currency_check CHECK ((currency = ANY (ARRAY['TON'::text, 'USDT_TRC20'::text]))),
    CONSTRAINT payments_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'confirming'::text, 'confirmed'::text, 'expired'::text, 'failed'::text])))
);

--
-- Name: payments_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.payments_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: payments_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.payments_id_seq OWNED BY public.payments.id;

--
-- Name: payout_requests; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.payout_requests (
    id integer NOT NULL,
    user_id bigint NOT NULL,
    amount_usd numeric(10,4) NOT NULL,
    method text DEFAULT 'usdt_trc20'::text,
    wallet text,
    status text DEFAULT 'pending'::text,
    admin_note text,
    created_at timestamp with time zone DEFAULT now(),
    processed_at timestamp with time zone
);

--
-- Name: payout_requests_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.payout_requests_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: payout_requests_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.payout_requests_id_seq OWNED BY public.payout_requests.id;

--
-- Name: persona_memory; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.persona_memory (
    id bigint NOT NULL,
    persona_id bigint,
    owner_id bigint NOT NULL,
    event_type character varying(32),
    content text,
    entity character varying(255),
    sentiment character varying(16),
    created_at timestamp with time zone DEFAULT now()
);

--
-- Name: persona_memory_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.persona_memory_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: persona_memory_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.persona_memory_id_seq OWNED BY public.persona_memory.id;

--
-- Name: persona_profiles; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.persona_profiles (
    id bigint NOT NULL,
    account_id bigint,
    owner_id bigint NOT NULL,
    persona_name character varying(64) NOT NULL,
    bio text,
    age integer DEFAULT 25,
    interests text[] DEFAULT ARRAY[]::text[],
    speech_style character varying(32) DEFAULT 'neutral'::character varying,
    tone character varying(32) DEFAULT 'positive'::character varying,
    niche character varying(64),
    backstory text,
    is_active boolean DEFAULT true,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now()
);

--
-- Name: persona_profiles_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.persona_profiles_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: persona_profiles_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.persona_profiles_id_seq OWNED BY public.persona_profiles.id;

--
-- Name: phone_owner_links; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.phone_owner_links (
    phone text NOT NULL,
    owner_id bigint NOT NULL,
    linked_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: platform_admin_sessions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.platform_admin_sessions (
    user_id bigint NOT NULL,
    granted_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone NOT NULL
);

--
-- Name: platform_proxy_pool; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.platform_proxy_pool (
    id integer NOT NULL,
    proxy_url text NOT NULL,
    proxy_type text DEFAULT 'socks5'::text NOT NULL,
    is_valid boolean DEFAULT true NOT NULL,
    latency_ms integer,
    fail_count integer DEFAULT 0 NOT NULL,
    success_count integer DEFAULT 0 NOT NULL,
    last_check timestamp with time zone DEFAULT now(),
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: platform_proxy_pool_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.platform_proxy_pool_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: platform_proxy_pool_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.platform_proxy_pool_id_seq OWNED BY public.platform_proxy_pool.id;

--
-- Name: platform_referral_codes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.platform_referral_codes (
    user_id bigint NOT NULL,
    code character varying(20) NOT NULL,
    created_at timestamp with time zone DEFAULT now(),
    total_clicks integer DEFAULT 0
);

--
-- Name: platform_referrals; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.platform_referrals (
    id bigint NOT NULL,
    referrer_id bigint NOT NULL,
    referred_id bigint NOT NULL,
    created_at timestamp with time zone DEFAULT now(),
    activated_at timestamp with time zone,
    paid_at timestamp with time zone,
    welcome_bonus_given boolean DEFAULT false
);

--
-- Name: platform_referrals_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.platform_referrals_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: platform_referrals_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.platform_referrals_id_seq OWNED BY public.platform_referrals.id;

--
-- Name: platform_settings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.platform_settings (
    key text NOT NULL,
    value text NOT NULL,
    updated_at timestamp with time zone DEFAULT now()
);

--
-- Name: platform_users; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.platform_users (
    user_id bigint NOT NULL,
    username text,
    first_name text,
    first_seen timestamp with time zone DEFAULT now() NOT NULL,
    last_active timestamp with time zone DEFAULT now() NOT NULL,
    is_blocked boolean DEFAULT false NOT NULL,
    last_seen timestamp with time zone DEFAULT now(),
    registered_at timestamp with time zone DEFAULT now(),
    current_plan text DEFAULT 'free'::text,
    plan_expires_at timestamp with time zone,
    is_banned boolean DEFAULT false,
    ban_reason text,
    banned_at timestamp with time zone,
    referrer_id bigint,
    notes text,
    last_upsell_at timestamp with time zone,
    trial_started_at timestamp with time zone DEFAULT now()
);

--
-- Name: position_history; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.position_history (
    id integer NOT NULL,
    bot_id integer NOT NULL,
    keyword text NOT NULL,
    "position" integer,
    checked_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: position_history_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.position_history_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: position_history_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.position_history_id_seq OWNED BY public.position_history.id;

--
-- Name: post_templates; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.post_templates (
    id bigint NOT NULL,
    owner_id bigint NOT NULL,
    title text NOT NULL,
    content text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: post_templates_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.post_templates_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: post_templates_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.post_templates_id_seq OWNED BY public.post_templates.id;

--
-- Name: presence_packs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.presence_packs (
    id bigint NOT NULL,
    owner_id bigint NOT NULL,
    name text NOT NULL,
    description text,
    target_url text,
    target_label text,
    bot_id bigint,
    bot_username text,
    channel_ids jsonb DEFAULT '[]'::jsonb,
    group_ids jsonb DEFAULT '[]'::jsonb,
    seed_posted boolean DEFAULT false,
    bot_promoted boolean DEFAULT false,
    created_at timestamp with time zone DEFAULT now()
);

--
-- Name: presence_packs_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.presence_packs_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: presence_packs_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.presence_packs_id_seq OWNED BY public.presence_packs.id;

--
-- Name: process_heartbeats; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.process_heartbeats (
    worker_id text NOT NULL,
    role text DEFAULT 'all'::text NOT NULL,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    last_seen timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: promo_logs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.promo_logs (
    id bigint NOT NULL,
    order_id integer,
    owner_id bigint NOT NULL,
    level text DEFAULT 'INFO'::text NOT NULL,
    event text NOT NULL,
    message text NOT NULL,
    meta jsonb DEFAULT '{}'::jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: promo_logs_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.promo_logs_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: promo_logs_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.promo_logs_id_seq OWNED BY public.promo_logs.id;

--
-- Name: promo_orders; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.promo_orders (
    id integer NOT NULL,
    owner_id bigint NOT NULL,
    keyword text NOT NULL,
    target_position integer DEFAULT 1 NOT NULL,
    bot_id integer,
    status text DEFAULT 'waiting'::text NOT NULL,
    smm_panel_id integer,
    smm_order_id text,
    target_subs integer,
    current_subs integer DEFAULT 0,
    last_position integer,
    notify_on_top boolean DEFAULT true,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    completed_at timestamp with time zone
);

--
-- Name: promo_orders_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.promo_orders_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: promo_orders_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.promo_orders_id_seq OWNED BY public.promo_orders.id;

--
-- Name: proxy_health_log; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.proxy_health_log (
    id bigint NOT NULL,
    proxy_id bigint NOT NULL,
    owner_id bigint NOT NULL,
    checked_at timestamp with time zone DEFAULT now(),
    is_reachable boolean NOT NULL,
    latency_ms integer,
    dc_id integer,
    error text
);

--
-- Name: proxy_health_log_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.proxy_health_log_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: proxy_health_log_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.proxy_health_log_id_seq OWNED BY public.proxy_health_log.id;

--
-- Name: proxy_performance; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.proxy_performance (
    id bigint NOT NULL,
    proxy_url text NOT NULL,
    test_type text DEFAULT 'connectivity'::text NOT NULL,
    latency_ms integer,
    success boolean DEFAULT true NOT NULL,
    error_msg text,
    tested_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: proxy_performance_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.proxy_performance_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: proxy_performance_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.proxy_performance_id_seq OWNED BY public.proxy_performance.id;

--
-- Name: proxy_quality_log; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.proxy_quality_log (
    id bigint NOT NULL,
    proxy_id bigint NOT NULL,
    latency_ms integer,
    success boolean NOT NULL,
    error_msg text,
    checked_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: proxy_quality_log_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.proxy_quality_log_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: proxy_quality_log_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.proxy_quality_log_id_seq OWNED BY public.proxy_quality_log.id;

--
-- Name: ranking_alerts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ranking_alerts (
    id integer NOT NULL,
    owner_id bigint NOT NULL,
    keyword_id integer,
    bot_id bigint,
    keyword text NOT NULL,
    old_position integer,
    new_position integer,
    alert_type text NOT NULL,
    created_at timestamp with time zone DEFAULT now(),
    acknowledged boolean DEFAULT false
);

--
-- Name: ranking_alerts_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.ranking_alerts_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: ranking_alerts_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.ranking_alerts_id_seq OWNED BY public.ranking_alerts.id;

--
-- Name: recovery_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.recovery_events (
    id bigint NOT NULL,
    owner_id bigint NOT NULL,
    recovery_type text NOT NULL,
    target_type text NOT NULL,
    target_id bigint,
    trigger text DEFAULT 'auto'::text NOT NULL,
    action text NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    severity text DEFAULT 'info'::text NOT NULL,
    details jsonb DEFAULT '{}'::jsonb NOT NULL,
    outcome jsonb DEFAULT '{}'::jsonb NOT NULL,
    started_at timestamp with time zone,
    completed_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: recovery_events_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.recovery_events_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: recovery_events_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.recovery_events_id_seq OWNED BY public.recovery_events.id;

--
-- Name: referral_conversions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.referral_conversions (
    id bigint NOT NULL,
    bot_id bigint NOT NULL,
    referrer_id bigint NOT NULL,
    referred_id bigint NOT NULL,
    conversion_type text DEFAULT 'funnel_complete'::text NOT NULL,
    funnel_id integer,
    meta jsonb DEFAULT '{}'::jsonb,
    occurred_at timestamp with time zone DEFAULT now()
);

--
-- Name: referral_conversions_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.referral_conversions_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: referral_conversions_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.referral_conversions_id_seq OWNED BY public.referral_conversions.id;

--
-- Name: referral_rewards; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.referral_rewards (
    id bigint NOT NULL,
    user_id bigint NOT NULL,
    level character varying(20) NOT NULL,
    plan character varying(20) NOT NULL,
    days integer NOT NULL,
    given_at timestamp with time zone DEFAULT now()
);

--
-- Name: referral_rewards_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.referral_rewards_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: referral_rewards_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.referral_rewards_id_seq OWNED BY public.referral_rewards.id;

--
-- Name: referrals; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.referrals (
    id integer NOT NULL,
    bot_id bigint NOT NULL,
    referrer_user_id bigint NOT NULL,
    referred_user_id bigint NOT NULL,
    deep_link_id integer,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: referrals_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.referrals_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: referrals_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.referrals_id_seq OWNED BY public.referrals.id;

--
-- Name: reg_check_cache; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.reg_check_cache (
    entity_id bigint NOT NULL,
    entity_type text NOT NULL,
    entity_name text,
    username text,
    reg_date timestamp with time zone,
    method text DEFAULT 'id_interpolation'::text NOT NULL,
    checked_by bigint,
    checked_at timestamp with time zone DEFAULT now() NOT NULL,
    participants_count integer,
    verified boolean DEFAULT false,
    scam boolean DEFAULT false,
    fake boolean DEFAULT false,
    premium boolean DEFAULT false,
    about text,
    confidence_lo timestamp with time zone,
    confidence_hi timestamp with time zone,
    dc_id smallint,
    is_fragment boolean DEFAULT false,
    confidence_score numeric(3,2),
    oldest_photo_id bigint,
    first_spotted_at timestamp with time zone
);

--
-- Name: relay_messages; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.relay_messages (
    id bigint NOT NULL,
    session_id bigint NOT NULL,
    direction text NOT NULL,
    text text NOT NULL,
    forwarded_msg_id bigint,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT relay_messages_direction_check CHECK ((direction = ANY (ARRAY['in'::text, 'out'::text])))
);

--
-- Name: relay_messages_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.relay_messages_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: relay_messages_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.relay_messages_id_seq OWNED BY public.relay_messages.id;

--
-- Name: relay_sessions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.relay_sessions (
    id bigint NOT NULL,
    bot_id bigint NOT NULL,
    user_id bigint NOT NULL,
    username text,
    first_name text,
    last_activity timestamp with time zone DEFAULT now() NOT NULL,
    messages_count integer DEFAULT 0 NOT NULL
);

--
-- Name: relay_sessions_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.relay_sessions_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: relay_sessions_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.relay_sessions_id_seq OWNED BY public.relay_sessions.id;

--
-- Name: removed_channels; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.removed_channels (
    owner_id bigint NOT NULL,
    channel_id bigint NOT NULL,
    reason text NOT NULL,
    detail text DEFAULT ''::text NOT NULL,
    acc_id integer,
    title text,
    username text,
    type text,
    access_hash bigint DEFAULT 0 NOT NULL,
    removed_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT removed_channels_reason_check CHECK ((reason = ANY (ARRAY['owner'::text, 'gone'::text])))
);

--
-- Name: resource_activity_log; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.resource_activity_log (
    id integer NOT NULL,
    session_id integer NOT NULL,
    account_id bigint NOT NULL,
    action_type text NOT NULL,
    resource_ref text,
    success boolean DEFAULT false NOT NULL,
    error text,
    performed_at timestamp with time zone DEFAULT now()
);

--
-- Name: resource_activity_log_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.resource_activity_log_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: resource_activity_log_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.resource_activity_log_id_seq OWNED BY public.resource_activity_log.id;

--
-- Name: resource_activity_sessions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.resource_activity_sessions (
    id integer NOT NULL,
    owner_id bigint NOT NULL,
    name text,
    account_ids bigint[] DEFAULT '{}'::bigint[] NOT NULL,
    resource_refs text[] DEFAULT '{}'::text[] NOT NULL,
    profile_type text DEFAULT 'mixed'::text NOT NULL,
    status text DEFAULT 'active'::text NOT NULL,
    current_day integer DEFAULT 0 NOT NULL,
    target_days integer DEFAULT 14 NOT NULL,
    daily_actions integer DEFAULT 8 NOT NULL,
    last_run_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now()
);

--
-- Name: resource_activity_sessions_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.resource_activity_sessions_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: resource_activity_sessions_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.resource_activity_sessions_id_seq OWNED BY public.resource_activity_sessions.id;

--
-- Name: restriction_alert_cooldown; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.restriction_alert_cooldown (
    owner_id bigint NOT NULL,
    event_type text NOT NULL,
    entity_id bigint DEFAULT 0 NOT NULL,
    last_alerted timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: restriction_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.restriction_events (
    id bigint NOT NULL,
    owner_id bigint NOT NULL,
    account_id bigint,
    bot_id bigint,
    event_type text NOT NULL,
    severity text DEFAULT 'warning'::text NOT NULL,
    details jsonb,
    alerted_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: restriction_events_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.restriction_events_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: restriction_events_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.restriction_events_id_seq OWNED BY public.restriction_events.id;

--
-- Name: routing_log; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.routing_log (
    id integer NOT NULL,
    from_bot_id bigint NOT NULL,
    to_bot_id bigint,
    user_id bigint NOT NULL,
    decision text NOT NULL,
    system_mode text,
    score_from real,
    score_to real,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: routing_log_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.routing_log_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: routing_log_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.routing_log_id_seq OWNED BY public.routing_log.id;

--
-- Name: routing_rules; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.routing_rules (
    id integer NOT NULL,
    source_bot_id bigint,
    target_bot_id bigint,
    trigger_event text NOT NULL,
    keyword text,
    weight double precision DEFAULT 1.0 NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: routing_rules_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.routing_rules_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: routing_rules_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.routing_rules_id_seq OWNED BY public.routing_rules.id;

--
-- Name: saved_segments; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.saved_segments (
    id bigint NOT NULL,
    owner_id bigint NOT NULL,
    name text NOT NULL,
    filters jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: saved_segments_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.saved_segments_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: saved_segments_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.saved_segments_id_seq OWNED BY public.saved_segments.id;

--
-- Name: scheduled_broadcasts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.scheduled_broadcasts (
    id integer NOT NULL,
    bot_id bigint NOT NULL,
    message_text text NOT NULL,
    execute_at timestamp with time zone NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    created_by bigint NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    repeat_interval_min integer DEFAULT 0,
    claimed_at timestamp with time zone,
    broadcast_id bigint
);

--
-- Name: scheduled_broadcasts_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.scheduled_broadcasts_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: scheduled_broadcasts_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.scheduled_broadcasts_id_seq OWNED BY public.scheduled_broadcasts.id;

--
-- Name: search_alert_cooldown; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.search_alert_cooldown (
    keyword_id integer NOT NULL,
    entity_id text NOT NULL,
    event_type text NOT NULL,
    last_alerted timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: search_change_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.search_change_events (
    id bigint NOT NULL,
    run_id uuid NOT NULL,
    snapshot_id uuid NOT NULL,
    keyword_id integer NOT NULL,
    entity_id text NOT NULL,
    account_id integer NOT NULL,
    event_type text NOT NULL,
    old_rank integer,
    new_rank integer,
    occurred_at timestamp with time zone DEFAULT now() NOT NULL,
    confirmed boolean DEFAULT false NOT NULL,
    confirmed_at timestamp with time zone,
    confirming_snapshot_id uuid,
    alerted boolean DEFAULT false NOT NULL,
    alerted_at timestamp with time zone,
    CONSTRAINT search_change_events_event_type_check CHECK ((event_type = ANY (ARRAY['APPEARED'::text, 'DISAPPEARED'::text, 'POSITION_CHANGED'::text])))
);

--
-- Name: search_change_events_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.search_change_events_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: search_change_events_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.search_change_events_id_seq OWNED BY public.search_change_events.id;

--
-- Name: search_keywords; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.search_keywords (
    id integer NOT NULL,
    bot_id integer NOT NULL,
    owner_id bigint NOT NULL,
    keyword text NOT NULL,
    region text DEFAULT 'all'::text,
    is_active boolean DEFAULT true,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: search_keywords_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.search_keywords_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: search_keywords_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.search_keywords_id_seq OWNED BY public.search_keywords.id;

--
-- Name: search_memory; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.search_memory (
    owner_id bigint NOT NULL,
    keyword text NOT NULL,
    search_count integer DEFAULT 1 NOT NULL,
    last_searched timestamp with time zone DEFAULT now() NOT NULL,
    first_searched timestamp with time zone DEFAULT now() NOT NULL,
    affinity_score numeric(5,2) DEFAULT 0 NOT NULL
);

--
-- Name: search_observations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.search_observations (
    id bigint NOT NULL,
    snapshot_id uuid NOT NULL,
    entity_id text NOT NULL,
    found boolean NOT NULL,
    rank integer,
    observed_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: search_observations_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.search_observations_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: search_observations_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.search_observations_id_seq OWNED BY public.search_observations.id;

--
-- Name: search_rankings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.search_rankings (
    id integer NOT NULL,
    keyword_id integer NOT NULL,
    bot_id bigint NOT NULL,
    "position" integer,
    checked_at timestamp with time zone DEFAULT now()
);

--
-- Name: search_rankings_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.search_rankings_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: search_rankings_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.search_rankings_id_seq OWNED BY public.search_rankings.id;

--
-- Name: search_snapshots; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.search_snapshots (
    snapshot_id uuid DEFAULT gen_random_uuid() NOT NULL,
    run_id uuid NOT NULL,
    keyword_id integer NOT NULL,
    account_id integer NOT NULL,
    keyword text NOT NULL,
    results jsonb NOT NULL,
    result_count integer NOT NULL,
    truncated boolean DEFAULT false NOT NULL,
    search_limit integer DEFAULT 20 NOT NULL,
    captured_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: security_violations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.security_violations (
    id bigint NOT NULL,
    user_id bigint NOT NULL,
    attempt_type text NOT NULL,
    details jsonb,
    ip_address text,
    created_at timestamp with time zone DEFAULT now()
);

--
-- Name: security_violations_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.security_violations_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: security_violations_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.security_violations_id_seq OWNED BY public.security_violations.id;

--
-- Name: seen_entities; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.seen_entities (
    entity_id bigint NOT NULL,
    entity_type text NOT NULL,
    chat_id bigint DEFAULT 0 NOT NULL,
    seen_at timestamp with time zone DEFAULT now() NOT NULL,
    first_seen_at timestamp with time zone DEFAULT now() NOT NULL,
    last_seen_at timestamp with time zone DEFAULT now() NOT NULL,
    sighting_count integer DEFAULT 1 NOT NULL,
    CONSTRAINT seen_entities_entity_type_check CHECK ((entity_type = ANY (ARRAY['user'::text, 'bot'::text, 'channel'::text, 'group'::text, 'supergroup'::text])))
);

--
-- Name: self_promo_runs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.self_promo_runs (
    id bigint NOT NULL,
    template_id integer,
    run_type text DEFAULT 'channel_post'::text NOT NULL,
    initiated_by bigint NOT NULL,
    sent integer DEFAULT 0,
    failed integer DEFAULT 0,
    status text DEFAULT 'running'::text,
    created_at timestamp with time zone DEFAULT now(),
    completed_at timestamp with time zone
);

--
-- Name: self_promo_runs_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.self_promo_runs_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: self_promo_runs_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.self_promo_runs_id_seq OWNED BY public.self_promo_runs.id;

--
-- Name: self_promo_templates; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.self_promo_templates (
    id integer NOT NULL,
    style text DEFAULT 'direct'::text NOT NULL,
    title text NOT NULL,
    content text NOT NULL,
    cta_text text,
    cta_url text,
    add_referral boolean DEFAULT true,
    is_active boolean DEFAULT true,
    use_count integer DEFAULT 0,
    created_at timestamp with time zone DEFAULT now()
);

--
-- Name: self_promo_templates_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.self_promo_templates_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: self_promo_templates_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.self_promo_templates_id_seq OWNED BY public.self_promo_templates.id;

--
-- Name: seo_ai_suggestions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.seo_ai_suggestions (
    owner_id bigint NOT NULL,
    chan_id bigint NOT NULL,
    title text,
    about text,
    username text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: seo_score_history; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.seo_score_history (
    id integer NOT NULL,
    owner_id bigint NOT NULL,
    entity_type text DEFAULT 'bot'::text NOT NULL,
    entity_id bigint NOT NULL,
    score integer NOT NULL,
    tips_json text,
    checked_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: seo_score_history_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.seo_score_history_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: seo_score_history_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.seo_score_history_id_seq OWNED BY public.seo_score_history.id;

--
-- Name: shield_actions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.shield_actions (
    id bigint NOT NULL,
    owner_id bigint NOT NULL,
    account_id bigint NOT NULL,
    action character varying(16) NOT NULL,
    risk_score double precision,
    ban_probability double precision,
    note text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: shield_actions_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.shield_actions_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: shield_actions_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.shield_actions_id_seq OWNED BY public.shield_actions.id;

--
-- Name: shield_configs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.shield_configs (
    owner_id bigint NOT NULL,
    risk_threshold double precision DEFAULT 0.7,
    ban_prob_threshold double precision DEFAULT 0.5,
    auto_pause boolean DEFAULT true,
    notify_admin boolean DEFAULT true,
    cool_duration_hours integer DEFAULT 24,
    updated_at timestamp with time zone DEFAULT now()
);

--
-- Name: short_links; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.short_links (
    code text NOT NULL,
    owner_id bigint NOT NULL,
    target_url text NOT NULL,
    title text,
    clicks bigint DEFAULT 0 NOT NULL,
    disabled boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    last_click_at timestamp with time zone,
    expires_at timestamp with time zone,
    tags text
);

--
-- Name: showcase_layers; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.showcase_layers (
    id bigint NOT NULL,
    owner_id bigint NOT NULL,
    mother_ref text NOT NULL,
    channel_id bigint NOT NULL,
    access_hash bigint DEFAULT 0 NOT NULL,
    showcase_ref text NOT NULL,
    creator_account_id bigint,
    status text DEFAULT 'active'::text NOT NULL,
    last_wave_at timestamp with time zone,
    waves_released integer DEFAULT 0 NOT NULL,
    active_link text,
    active_link_limit integer,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    burned_at timestamp with time zone,
    burn_reason text
);

--
-- Name: showcase_layers_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.showcase_layers_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: showcase_layers_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.showcase_layers_id_seq OWNED BY public.showcase_layers.id;

--
-- Name: smart_tag_rules; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.smart_tag_rules (
    id integer NOT NULL,
    owner_id bigint NOT NULL,
    name text NOT NULL,
    rule_type text DEFAULT 'field_match'::text NOT NULL,
    conditions jsonb DEFAULT '{}'::jsonb NOT NULL,
    tag text NOT NULL,
    is_active boolean DEFAULT true,
    created_at timestamp with time zone DEFAULT now()
);

--
-- Name: smart_tag_rules_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.smart_tag_rules_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: smart_tag_rules_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.smart_tag_rules_id_seq OWNED BY public.smart_tag_rules.id;

--
-- Name: smm_panels; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.smm_panels (
    id integer NOT NULL,
    owner_id bigint NOT NULL,
    name text NOT NULL,
    api_url text NOT NULL,
    api_key_enc text NOT NULL,
    service_id text DEFAULT ''::text,
    is_active boolean DEFAULT true,
    balance numeric(10,2) DEFAULT 0,
    last_checked timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: smm_panels_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.smm_panels_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: smm_panels_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.smm_panels_id_seq OWNED BY public.smm_panels.id;

--
-- Name: stars_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.stars_events (
    id bigint NOT NULL,
    experiment_id bigint,
    bot_id bigint NOT NULL,
    user_id bigint NOT NULL,
    variant character varying(2) NOT NULL,
    event_type character varying(16) NOT NULL,
    stars_amount integer DEFAULT 0,
    created_at timestamp with time zone DEFAULT now()
);

--
-- Name: stars_events_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.stars_events_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: stars_events_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.stars_events_id_seq OWNED BY public.stars_events.id;

--
-- Name: stars_experiments; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.stars_experiments (
    id bigint NOT NULL,
    bot_id bigint NOT NULL,
    owner_id bigint NOT NULL,
    name character varying(128) NOT NULL,
    content_type character varying(64) DEFAULT 'message'::character varying,
    price_a integer NOT NULL,
    price_b integer NOT NULL,
    impressions_a integer DEFAULT 0,
    conversions_a integer DEFAULT 0,
    revenue_a integer DEFAULT 0,
    impressions_b integer DEFAULT 0,
    conversions_b integer DEFAULT 0,
    revenue_b integer DEFAULT 0,
    status character varying(16) DEFAULT 'active'::character varying,
    winner character varying(2),
    significance double precision,
    created_at timestamp with time zone DEFAULT now(),
    completed_at timestamp with time zone
);

--
-- Name: stars_experiments_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.stars_experiments_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: stars_experiments_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.stars_experiments_id_seq OWNED BY public.stars_experiments.id;

--
-- Name: stars_transactions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.stars_transactions (
    id bigint NOT NULL,
    bot_id bigint NOT NULL,
    user_id bigint NOT NULL,
    stars_amount integer NOT NULL,
    description text,
    transaction_id character varying(128),
    created_at timestamp with time zone DEFAULT now()
);

--
-- Name: stars_transactions_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.stars_transactions_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: stars_transactions_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.stars_transactions_id_seq OWNED BY public.stars_transactions.id;

--
-- Name: strike_access; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.strike_access (
    user_id bigint NOT NULL,
    purchased_at timestamp with time zone DEFAULT now(),
    payment_ref text,
    granted_by bigint,
    mode text DEFAULT 'normal'::text
);

--
-- Name: strike_appeals; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.strike_appeals (
    id text NOT NULL,
    owner_id bigint NOT NULL,
    account_id bigint,
    reason text,
    status text DEFAULT 'submitted'::text NOT NULL,
    emails_sent integer DEFAULT 0 NOT NULL,
    details text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: strike_email_accounts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.strike_email_accounts (
    id integer NOT NULL,
    owner_id bigint NOT NULL,
    email text NOT NULL,
    smtp_host text NOT NULL,
    smtp_port integer DEFAULT 587 NOT NULL,
    smtp_pass text NOT NULL,
    display_name text,
    is_active boolean DEFAULT true NOT NULL,
    fail_count integer DEFAULT 0 NOT NULL,
    last_used_at timestamp with time zone,
    added_at timestamp with time zone DEFAULT now() NOT NULL,
    auth_type text DEFAULT 'password'::text NOT NULL,
    oauth_provider text,
    oauth_refresh_token text,
    oauth_access_token text,
    oauth_expires_at timestamp with time zone,
    oauth_scopes text[] DEFAULT '{}'::text[]
);

--
-- Name: strike_email_accounts_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.strike_email_accounts_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: strike_email_accounts_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.strike_email_accounts_id_seq OWNED BY public.strike_email_accounts.id;

--
-- Name: strike_history; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.strike_history (
    id integer NOT NULL,
    owner_id bigint NOT NULL,
    target text NOT NULL,
    reason text DEFAULT 'spam'::text NOT NULL,
    preset text,
    accounts_used integer DEFAULT 0,
    peer_reported integer DEFAULT 0,
    msgs_reported integer DEFAULT 0,
    pinned_reported integer DEFAULT 0,
    admins_reported integer DEFAULT 0,
    network_nodes integer DEFAULT 0,
    network_reports integer DEFAULT 0,
    blocked integer DEFAULT 0,
    verified_down boolean,
    duration_s double precision DEFAULT 0,
    abuse_form_ok boolean DEFAULT false,
    spambot_escalation text DEFAULT 'skipped'::text,
    created_at timestamp with time zone DEFAULT now(),
    msgs_fetched integer DEFAULT 0,
    accounts_ok integer DEFAULT 0,
    accounts_flood integer DEFAULT 0,
    accounts_banned integer DEFAULT 0,
    accounts_failed integer DEFAULT 0,
    infra_domains integer DEFAULT 0,
    infra_apwg_sent integer DEFAULT 0,
    infra_registrar_sent integer DEFAULT 0
);

--
-- Name: strike_history_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.strike_history_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: strike_history_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.strike_history_id_seq OWNED BY public.strike_history.id;

--
-- Name: strike_reports; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.strike_reports (
    id integer NOT NULL,
    owner_id bigint NOT NULL,
    target text NOT NULL,
    category text NOT NULL,
    tg_reports_sent integer DEFAULT 0 NOT NULL,
    emails_sent integer DEFAULT 0 NOT NULL,
    total_reports integer DEFAULT 0 NOT NULL,
    details jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: strike_reports_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.strike_reports_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: strike_reports_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.strike_reports_id_seq OWNED BY public.strike_reports.id;

--
-- Name: subscription_gate_channels; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.subscription_gate_channels (
    id integer NOT NULL,
    channel_username text NOT NULL,
    channel_title text DEFAULT ''::text NOT NULL,
    added_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: subscription_gate_channels_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.subscription_gate_channels_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: subscription_gate_channels_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.subscription_gate_channels_id_seq OWNED BY public.subscription_gate_channels.id;

--
-- Name: subscription_trials; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.subscription_trials (
    owner_id bigint NOT NULL,
    granted_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    days integer NOT NULL,
    account_tg_user_ids bigint[] DEFAULT '{}'::bigint[] NOT NULL
);

--
-- Name: subscriptions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.subscriptions (
    id integer NOT NULL,
    user_id bigint NOT NULL,
    plan text NOT NULL,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    CONSTRAINT subscriptions_plan_check CHECK ((plan = ANY (ARRAY['free'::text, 'paid'::text, 'starter'::text, 'pro'::text, 'enterprise'::text])))
);

--
-- Name: subscriptions_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.subscriptions_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: subscriptions_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.subscriptions_id_seq OWNED BY public.subscriptions.id;

--
-- Name: system_health_snapshots; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.system_health_snapshots (
    id bigint NOT NULL,
    owner_id bigint NOT NULL,
    health_score integer DEFAULT 0 NOT NULL,
    accounts_ready integer DEFAULT 0 NOT NULL,
    accounts_total integer DEFAULT 0 NOT NULL,
    accounts_in_cooldown integer DEFAULT 0 NOT NULL,
    avg_trust_score double precision DEFAULT 0 NOT NULL,
    ops_pending integer DEFAULT 0 NOT NULL,
    ops_running integer DEFAULT 0 NOT NULL,
    ops_failed_24h integer DEFAULT 0 NOT NULL,
    ops_done_24h integer DEFAULT 0 NOT NULL,
    proxies_healthy integer DEFAULT 0 NOT NULL,
    proxies_total integer DEFAULT 0 NOT NULL,
    active_alerts integer DEFAULT 0 NOT NULL,
    active_anomalies integer DEFAULT 0 NOT NULL,
    active_recoveries integer DEFAULT 0 NOT NULL,
    components jsonb DEFAULT '{}'::jsonb NOT NULL,
    snapshot_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: system_health_snapshots_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.system_health_snapshots_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: system_health_snapshots_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.system_health_snapshots_id_seq OWNED BY public.system_health_snapshots.id;

--
-- Name: system_mode; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.system_mode (
    id integer DEFAULT 1 NOT NULL,
    mode text DEFAULT 'manual'::text NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT system_mode_mode_check CHECK ((mode = ANY (ARRAY['manual'::text, 'assisted'::text, 'autopilot'::text, 'growth'::text, 'experiment'::text, 'stability'::text])))
);

--
-- Name: tg_accounts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tg_accounts (
    id integer NOT NULL,
    owner_id bigint NOT NULL,
    phone text NOT NULL,
    session_str text NOT NULL,
    tg_user_id bigint,
    first_name text DEFAULT ''::text,
    username text DEFAULT ''::text,
    added_at timestamp with time zone DEFAULT now(),
    last_used timestamp with time zone,
    is_active boolean DEFAULT true,
    device_model text,
    system_version text,
    app_version text,
    trust_score double precision DEFAULT 1.0 NOT NULL,
    flood_count_7d integer DEFAULT 0 NOT NULL,
    cooldown_until timestamp with time zone,
    last_flood_at timestamp with time zone,
    account_notes text,
    cluster text,
    proxy_id integer,
    last_low_trust_alert timestamp with time zone,
    added_by bigint,
    acc_status text DEFAULT 'active'::text,
    status_checked_at timestamp with time zone,
    status_reason text,
    last_real_check_at timestamp with time zone,
    real_check_status text,
    lang_code text DEFAULT 'ru'::text,
    system_lang_code text DEFAULT 'ru-RU'::text,
    last_warmup_at timestamp with time zone,
    warmup_level text,
    tags text[] DEFAULT '{}'::text[],
    pool text,
    labels text[] DEFAULT '{}'::text[],
    warnings text[] DEFAULT '{}'::text[],
    project text,
    cooldown_cleared_at timestamp with time zone,
    in_operation boolean DEFAULT false,
    session_fp text,
    stage text,
    cf_relay_url text,
    is_premium boolean,
    has_photo boolean,
    profile_checked_at timestamp with time zone,
    gender text,
    op_lease_until timestamp with time zone,
    op_lease_owner text,
    session_conflict_at timestamp with time zone,
    api_id bigint,
    listener_enabled boolean DEFAULT false NOT NULL,
    readiness_level text,
    risk_cleared_at timestamp with time zone
);

--
-- Name: tg_accounts_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.tg_accounts_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: tg_accounts_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.tg_accounts_id_seq OWNED BY public.tg_accounts.id;

--
-- Name: tg_channels; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tg_channels (
    id bigint NOT NULL,
    owner_id bigint NOT NULL,
    title text,
    username text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    is_private boolean DEFAULT false
);

--
-- Name: tg_cloud_blobs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tg_cloud_blobs (
    locator text NOT NULL,
    owner_id bigint NOT NULL,
    data bytea NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: tg_cloud_chunk_locs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tg_cloud_chunk_locs (
    id bigint NOT NULL,
    chunk_id bigint NOT NULL,
    owner_id bigint NOT NULL,
    replica integer DEFAULT 0 NOT NULL,
    acc_id bigint,
    channel_id bigint,
    message_id bigint,
    locator text NOT NULL,
    status text DEFAULT 'stored'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: tg_cloud_chunk_locs_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.tg_cloud_chunk_locs_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: tg_cloud_chunk_locs_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.tg_cloud_chunk_locs_id_seq OWNED BY public.tg_cloud_chunk_locs.id;

--
-- Name: tg_cloud_chunks; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tg_cloud_chunks (
    id bigint NOT NULL,
    file_id bigint NOT NULL,
    owner_id bigint NOT NULL,
    ord integer NOT NULL,
    size_bytes bigint DEFAULT 0 NOT NULL,
    sha256 text DEFAULT ''::text NOT NULL,
    acc_id bigint,
    channel_id bigint,
    message_id bigint,
    locator text DEFAULT ''::text NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL
);

--
-- Name: tg_cloud_chunks_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.tg_cloud_chunks_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: tg_cloud_chunks_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.tg_cloud_chunks_id_seq OWNED BY public.tg_cloud_chunks.id;

--
-- Name: tg_cloud_files; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tg_cloud_files (
    id bigint NOT NULL,
    owner_id bigint NOT NULL,
    name text DEFAULT ''::text NOT NULL,
    size_bytes bigint DEFAULT 0 NOT NULL,
    chunk_count integer DEFAULT 0 NOT NULL,
    sha256 text DEFAULT ''::text NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: tg_cloud_files_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.tg_cloud_files_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: tg_cloud_files_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.tg_cloud_files_id_seq OWNED BY public.tg_cloud_files.id;

--
-- Name: tg_cloud_quota; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tg_cloud_quota (
    owner_id bigint NOT NULL,
    limit_bytes bigint DEFAULT 0 NOT NULL,
    used_bytes bigint DEFAULT 0 NOT NULL,
    paid boolean DEFAULT false NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: tg_cloud_storekeepers; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tg_cloud_storekeepers (
    acc_id bigint NOT NULL,
    owner_id bigint NOT NULL,
    channel_id bigint NOT NULL,
    access_hash bigint DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: tracked_keywords; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tracked_keywords (
    id integer NOT NULL,
    bot_id bigint NOT NULL,
    owner_id bigint NOT NULL,
    keyword text NOT NULL,
    is_active boolean DEFAULT true,
    created_at timestamp with time zone DEFAULT now(),
    notify_enabled boolean DEFAULT true NOT NULL,
    region text DEFAULT 'ru'::text NOT NULL
);

--
-- Name: tracked_keywords_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.tracked_keywords_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: tracked_keywords_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.tracked_keywords_id_seq OWNED BY public.tracked_keywords.id;

--
-- Name: unified_contacts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.unified_contacts (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    owner_id bigint NOT NULL,
    telegram_user_id bigint,
    username text,
    first_name text,
    last_name text,
    patronymic text,
    display_name text,
    phones jsonb DEFAULT '[]'::jsonb,
    emails jsonb DEFAULT '[]'::jsonb,
    company text,
    "position" text,
    websites jsonb DEFAULT '[]'::jsonb,
    addresses jsonb DEFAULT '[]'::jsonb,
    birthday date,
    notes text DEFAULT ''::text,
    tags text[] DEFAULT '{}'::text[],
    color_label text,
    is_favorite boolean DEFAULT false,
    is_premium boolean DEFAULT false,
    custom_avatar_url text,
    importance_level integer DEFAULT 0,
    user_rating integer DEFAULT 0,
    custom_fields jsonb DEFAULT '{}'::jsonb,
    discovered_at timestamp with time zone DEFAULT now(),
    last_synced_at timestamp with time zone,
    last_changed_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    trust_score real DEFAULT 1.0,
    merge_confidence real,
    persona_type text DEFAULT 'personal'::text,
    identity_hash text,
    digital_footprint jsonb DEFAULT '{}'::jsonb,
    last_active_at timestamp with time zone,
    source_accounts_count integer DEFAULT 1,
    is_verified boolean DEFAULT false,
    is_mutual boolean DEFAULT false,
    registered_estimate date,
    last_seen_type text,
    last_seen_at timestamp with time zone,
    gender text,
    excluded boolean DEFAULT false NOT NULL,
    excluded_at timestamp with time zone
);

--
-- Name: user_activity; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.user_activity (
    id integer NOT NULL,
    bot_id bigint NOT NULL,
    user_id bigint NOT NULL,
    message_count integer DEFAULT 1 NOT NULL,
    last_seen timestamp with time zone DEFAULT now() NOT NULL,
    first_seen timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: user_activity_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.user_activity_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: user_activity_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.user_activity_id_seq OWNED BY public.user_activity.id;

--
-- Name: user_payment_methods; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.user_payment_methods (
    id bigint NOT NULL,
    owner_id bigint NOT NULL,
    method_type text NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: user_payment_methods_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.user_payment_methods_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: user_payment_methods_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.user_payment_methods_id_seq OWNED BY public.user_payment_methods.id;

--
-- Name: user_proxies; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.user_proxies (
    id integer NOT NULL,
    owner_id bigint NOT NULL,
    label text,
    proxy_url text NOT NULL,
    proxy_type text DEFAULT 'socks5'::text,
    is_active boolean DEFAULT true,
    last_check timestamp with time zone,
    is_alive boolean,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    geo_country text,
    geo_city text,
    latency_avg_ms integer,
    success_rate numeric(5,2) DEFAULT 100,
    last_checked_at timestamp with time zone,
    dc_affinity integer,
    consecutive_failures integer DEFAULT 0,
    proxy_fp text,
    is_backup boolean DEFAULT false,
    dead_notified_at timestamp with time zone
);

--
-- Name: user_proxies_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.user_proxies_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: user_proxies_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.user_proxies_id_seq OWNED BY public.user_proxies.id;

--
-- Name: user_tags; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.user_tags (
    id integer NOT NULL,
    bot_id bigint NOT NULL,
    user_id bigint NOT NULL,
    tag text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: user_tags_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.user_tags_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: user_tags_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.user_tags_id_seq OWNED BY public.user_tags.id;

--
-- Name: va_admin_drafts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.va_admin_drafts (
    id bigint NOT NULL,
    owner_id bigint NOT NULL,
    channel_id bigint NOT NULL,
    pillar text,
    body text NOT NULL,
    reasons jsonb DEFAULT '[]'::jsonb NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    is_intro boolean DEFAULT false NOT NULL,
    op_id bigint,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    decided_at timestamp with time zone,
    reject_reason text,
    media_id bigint,
    CONSTRAINT va_admin_drafts_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'published'::text, 'rejected'::text, 'expired'::text, 'failed'::text])))
);

--
-- Name: va_admin_drafts_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.va_admin_drafts_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: va_admin_drafts_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.va_admin_drafts_id_seq OWNED BY public.va_admin_drafts.id;

--
-- Name: va_admin_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.va_admin_events (
    id bigint NOT NULL,
    owner_id bigint NOT NULL,
    channel_id bigint NOT NULL,
    kind text NOT NULL,
    text text DEFAULT ''::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: va_admin_events_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.va_admin_events_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: va_admin_events_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.va_admin_events_id_seq OWNED BY public.va_admin_events.id;

--
-- Name: va_admin_plan; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.va_admin_plan (
    id bigint NOT NULL,
    owner_id bigint NOT NULL,
    channel_id bigint NOT NULL,
    slot_at timestamp with time zone NOT NULL,
    pillar text DEFAULT ''::text NOT NULL,
    topic text DEFAULT ''::text NOT NULL,
    status text DEFAULT 'planned'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    body text,
    written_at timestamp with time zone,
    write_attempts integer DEFAULT 0 NOT NULL,
    CONSTRAINT va_admin_plan_status_check CHECK ((status = ANY (ARRAY['planned'::text, 'done'::text, 'skipped'::text])))
);

--
-- Name: va_admin_plan_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.va_admin_plan_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: va_admin_plan_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.va_admin_plan_id_seq OWNED BY public.va_admin_plan.id;

--
-- Name: va_channel_admin; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.va_channel_admin (
    id bigint NOT NULL,
    owner_id bigint NOT NULL,
    channel_id bigint NOT NULL,
    enabled boolean DEFAULT false NOT NULL,
    topic text DEFAULT ''::text NOT NULL,
    audience text DEFAULT ''::text NOT NULL,
    tone text DEFAULT ''::text NOT NULL,
    notes text DEFAULT ''::text NOT NULL,
    project_info text DEFAULT ''::text NOT NULL,
    lead_contact text DEFAULT ''::text NOT NULL,
    brief jsonb DEFAULT '{}'::jsonb NOT NULL,
    posts_per_day integer DEFAULT 2 NOT NULL,
    window_start integer DEFAULT 9 NOT NULL,
    window_end integer DEFAULT 21 NOT NULL,
    tz_offset integer DEFAULT 3 NOT NULL,
    publish_mode text DEFAULT 'auto'::text NOT NULL,
    intro_pending boolean DEFAULT true NOT NULL,
    next_post_at timestamp with time zone,
    last_post_at timestamp with time zone,
    last_error text,
    fail_streak integer DEFAULT 0 NOT NULL,
    setup_done boolean DEFAULT false NOT NULL,
    last_op_id bigint,
    alerted_at timestamp with time zone,
    auto_tune boolean DEFAULT true NOT NULL,
    members_count integer,
    last_stats_at timestamp with time zone,
    last_report_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    business jsonb DEFAULT '{}'::jsonb NOT NULL,
    lang text DEFAULT ''::text NOT NULL,
    cross_promote boolean DEFAULT true NOT NULL,
    CONSTRAINT va_channel_admin_posts_per_day_check CHECK (((posts_per_day >= 1) AND (posts_per_day <= 12))),
    CONSTRAINT va_channel_admin_publish_mode_check CHECK ((publish_mode = ANY (ARRAY['review'::text, 'auto'::text]))),
    CONSTRAINT va_channel_admin_tz_offset_check CHECK (((tz_offset >= '-12'::integer) AND (tz_offset <= 14))),
    CONSTRAINT va_channel_admin_window_end_check CHECK (((window_end >= 1) AND (window_end <= 24))),
    CONSTRAINT va_channel_admin_window_start_check CHECK (((window_start >= 0) AND (window_start <= 23)))
);

--
-- Name: va_channel_admin_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.va_channel_admin_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: va_channel_admin_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.va_channel_admin_id_seq OWNED BY public.va_channel_admin.id;

--
-- Name: va_channel_brain; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.va_channel_brain (
    id bigint NOT NULL,
    owner_id bigint NOT NULL,
    channel_key text NOT NULL,
    brand_rules jsonb DEFAULT '{}'::jsonb NOT NULL,
    pillars jsonb DEFAULT '[]'::jsonb NOT NULL,
    mix_weights jsonb DEFAULT '{}'::jsonb NOT NULL,
    autonomy_mode text DEFAULT 'manual'::text NOT NULL,
    max_streak integer DEFAULT 2 NOT NULL,
    dup_threshold real DEFAULT 0.6 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT va_channel_brain_autonomy_mode_check CHECK ((autonomy_mode = ANY (ARRAY['manual'::text, 'semi'::text, 'autonomous'::text])))
);

--
-- Name: va_channel_brain_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.va_channel_brain_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: va_channel_brain_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.va_channel_brain_id_seq OWNED BY public.va_channel_brain.id;

--
-- Name: va_channel_posts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.va_channel_posts (
    id bigint NOT NULL,
    owner_id bigint NOT NULL,
    channel_key text NOT NULL,
    op_id bigint,
    pillar text,
    body text DEFAULT ''::text NOT NULL,
    published_at timestamp with time zone DEFAULT now() NOT NULL,
    msg_id bigint,
    views integer,
    forwards integer,
    reactions integer,
    stats_at timestamp with time zone,
    learning_views bigint,
    learning_reactions bigint,
    learning_forwards bigint,
    learning_sampled_at timestamp with time zone,
    learned_at timestamp with time zone
);

--
-- Name: va_channel_posts_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.va_channel_posts_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: va_channel_posts_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.va_channel_posts_id_seq OWNED BY public.va_channel_posts.id;

--
-- Name: va_channel_stats; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.va_channel_stats (
    owner_id bigint NOT NULL,
    channel_id bigint NOT NULL,
    day date NOT NULL,
    members_count integer NOT NULL
);

--
-- Name: va_learning_decisions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.va_learning_decisions (
    id bigint NOT NULL,
    owner_id bigint NOT NULL,
    channel_id bigint NOT NULL,
    old_weights jsonb NOT NULL,
    new_weights jsonb NOT NULL,
    post_ids bigint[] NOT NULL,
    explanation text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    reverted_at timestamp with time zone
);

--
-- Name: va_learning_decisions_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.va_learning_decisions_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: va_learning_decisions_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.va_learning_decisions_id_seq OWNED BY public.va_learning_decisions.id;

--
-- Name: va_media; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.va_media (
    id bigint NOT NULL,
    owner_id bigint NOT NULL,
    channel_id bigint NOT NULL,
    file_id text NOT NULL,
    file_unique_id text NOT NULL,
    description text NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    selected_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: va_media_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.va_media_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: va_media_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.va_media_id_seq OWNED BY public.va_media.id;

--
-- Name: va_network_strategy; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.va_network_strategy (
    owner_id bigint NOT NULL,
    settings jsonb DEFAULT '{}'::jsonb NOT NULL,
    revision integer DEFAULT 1 NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT va_network_strategy_settings_check CHECK ((jsonb_typeof(settings) = 'object'::text))
);

--
-- Name: va_news_inbox; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.va_news_inbox (
    id bigint NOT NULL,
    owner_id bigint NOT NULL,
    channel_id bigint NOT NULL,
    signal_hash text NOT NULL,
    source_username text NOT NULL,
    source_message_id bigint,
    published_at timestamp with time zone NOT NULL,
    source_text text NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    attempts integer DEFAULT 0 NOT NULL,
    draft_id bigint,
    claimed_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT va_news_inbox_attempts_check CHECK (((attempts >= 0) AND (attempts <= 5))),
    CONSTRAINT va_news_inbox_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'drafting'::text, 'drafted'::text, 'expired'::text])))
);

--
-- Name: va_news_inbox_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.va_news_inbox_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: va_news_inbox_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.va_news_inbox_id_seq OWNED BY public.va_news_inbox.id;

--
-- Name: va_reference_channels; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.va_reference_channels (
    id bigint NOT NULL,
    owner_id bigint NOT NULL,
    channel_id bigint NOT NULL,
    ref_username text NOT NULL,
    kind text DEFAULT 'competitor'::text NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    error text,
    stats jsonb DEFAULT '{}'::jsonb NOT NULL,
    lessons jsonb DEFAULT '{}'::jsonb NOT NULL,
    attempted_at timestamp with time zone,
    analyzed_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    focus text DEFAULT ''::text NOT NULL,
    peer_id bigint,
    peer_hash bigint,
    CONSTRAINT va_reference_channels_kind_check CHECK ((kind = ANY (ARRAY['competitor'::text, 'own'::text, 'example'::text]))),
    CONSTRAINT va_reference_channels_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'ready'::text, 'error'::text])))
);

--
-- Name: va_reference_channels_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.va_reference_channels_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: va_reference_channels_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.va_reference_channels_id_seq OWNED BY public.va_reference_channels.id;

--
-- Name: va_tracked_links; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.va_tracked_links (
    id bigint NOT NULL,
    owner_id bigint NOT NULL,
    channel_id bigint NOT NULL,
    target_channel_id bigint NOT NULL,
    link text,
    pillar text,
    op_id bigint,
    joins integer DEFAULT 0 NOT NULL,
    requests integer DEFAULT 0 NOT NULL,
    checked_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: va_tracked_links_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.va_tracked_links_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: va_tracked_links_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.va_tracked_links_id_seq OWNED BY public.va_tracked_links.id;

--
-- Name: vault_intent_hits; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.vault_intent_hits (
    id bigint NOT NULL,
    owner_id bigint NOT NULL,
    rule_id bigint NOT NULL,
    chat_id bigint,
    peer_name text,
    text_preview text,
    stage_to text,
    tags_added text[],
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: vault_intent_hits_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.vault_intent_hits_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: vault_intent_hits_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.vault_intent_hits_id_seq OWNED BY public.vault_intent_hits.id;

--
-- Name: vault_intent_rules; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.vault_intent_rules (
    id bigint NOT NULL,
    owner_id bigint NOT NULL,
    phrase text NOT NULL,
    stage text,
    tag text,
    notify boolean DEFAULT true NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    hits integer DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: vault_intent_rules_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.vault_intent_rules_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: vault_intent_rules_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.vault_intent_rules_id_seq OWNED BY public.vault_intent_rules.id;

--
-- Name: vault_key_check; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.vault_key_check (
    id integer DEFAULT 1 NOT NULL,
    canary text NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT vault_key_check_single_row CHECK ((id = 1))
);

--
-- Name: vault_messages; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.vault_messages (
    id bigint NOT NULL,
    owner_id bigint NOT NULL,
    connection_id text,
    chat_id bigint NOT NULL,
    msg_id bigint NOT NULL,
    direction text DEFAULT 'in'::text NOT NULL,
    peer_user_id bigint,
    peer_name text,
    peer_username text,
    text_enc text,
    media_type text,
    media_file_id text,
    media_unique_id text,
    media_size bigint,
    media_mime text,
    media_name text,
    msg_date timestamp with time zone,
    is_deleted boolean DEFAULT false NOT NULL,
    deleted_at timestamp with time zone,
    is_edited boolean DEFAULT false NOT NULL,
    edited_at timestamp with time zone,
    edit_history jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: vault_messages_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.vault_messages_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: vault_messages_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.vault_messages_id_seq OWNED BY public.vault_messages.id;

--
-- Name: virtual_state_history; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.virtual_state_history (
    id bigint NOT NULL,
    owner_id bigint NOT NULL,
    entity_type text NOT NULL,
    entity_id text NOT NULL,
    state_key text NOT NULL,
    from_value text,
    to_value text NOT NULL,
    reason text,
    confidence real,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: virtual_state_history_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.virtual_state_history_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: virtual_state_history_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.virtual_state_history_id_seq OWNED BY public.virtual_state_history.id;

--
-- Name: virtual_states; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.virtual_states (
    owner_id bigint NOT NULL,
    entity_type text NOT NULL,
    entity_id text NOT NULL,
    state_key text NOT NULL,
    value text NOT NULL,
    confidence real DEFAULT 0.5 NOT NULL,
    source text,
    expires_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: visibility_alert_settings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.visibility_alert_settings (
    owner_id bigint NOT NULL,
    drop_threshold integer DEFAULT 10,
    rise_threshold integer DEFAULT 5,
    alerts_enabled boolean DEFAULT true,
    auto_reoptimize boolean DEFAULT false
);

--
-- Name: warmup_session_log; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.warmup_session_log (
    id integer NOT NULL,
    session_id integer NOT NULL,
    account_id bigint NOT NULL,
    action_type text NOT NULL,
    target text,
    success boolean DEFAULT false NOT NULL,
    error text,
    performed_at timestamp with time zone DEFAULT now()
);

--
-- Name: warmup_session_log_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.warmup_session_log_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: warmup_session_log_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.warmup_session_log_id_seq OWNED BY public.warmup_session_log.id;

--
-- Name: warmup_sessions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.warmup_sessions (
    id integer NOT NULL,
    owner_id bigint NOT NULL,
    name text,
    account_ids bigint[] DEFAULT '{}'::bigint[] NOT NULL,
    target_type text DEFAULT 'infra'::text NOT NULL,
    target_refs text[] DEFAULT '{}'::text[] NOT NULL,
    plan_type text DEFAULT 'standard'::text NOT NULL,
    status text DEFAULT 'active'::text NOT NULL,
    current_day integer DEFAULT 0 NOT NULL,
    target_days integer DEFAULT 14 NOT NULL,
    daily_actions integer DEFAULT 10 NOT NULL,
    started_at timestamp with time zone DEFAULT now(),
    last_run_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now()
);

--
-- Name: warmup_sessions_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.warmup_sessions_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: warmup_sessions_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.warmup_sessions_id_seq OWNED BY public.warmup_sessions.id;

--
-- Name: wb_account_budget; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.wb_account_budget (
    account_id bigint NOT NULL,
    action_date date DEFAULT CURRENT_DATE NOT NULL,
    action_type text NOT NULL,
    used integer DEFAULT 0 NOT NULL
);

--
-- Name: wb_accounts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.wb_accounts (
    id bigint NOT NULL,
    owner_id bigint NOT NULL,
    phone text NOT NULL,
    wb_id text,
    user_id text,
    name text,
    session_enc text,
    proxy_enc text,
    device jsonb DEFAULT '{}'::jsonb NOT NULL,
    status text DEFAULT 'new'::text NOT NULL,
    health_score integer DEFAULT 100 NOT NULL,
    cooldown_until timestamp with time zone,
    last_used_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: wb_accounts_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.wb_accounts_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: wb_accounts_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.wb_accounts_id_seq OWNED BY public.wb_accounts.id;

--
-- Name: wb_operation_targets; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.wb_operation_targets (
    id bigint NOT NULL,
    op_id bigint NOT NULL,
    ref text NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    account_id bigint,
    error text,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: wb_operation_targets_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.wb_operation_targets_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: wb_operation_targets_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.wb_operation_targets_id_seq OWNED BY public.wb_operation_targets.id;

--
-- Name: wb_operations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.wb_operations (
    id bigint NOT NULL,
    owner_id bigint NOT NULL,
    op_type text NOT NULL,
    payload jsonb DEFAULT '{}'::jsonb NOT NULL,
    status text DEFAULT 'queued'::text NOT NULL,
    progress integer DEFAULT 0 NOT NULL,
    total integer DEFAULT 0 NOT NULL,
    result jsonb DEFAULT '{}'::jsonb NOT NULL,
    error text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    started_at timestamp with time zone,
    finished_at timestamp with time zone,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: wb_operations_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.wb_operations_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: wb_operations_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.wb_operations_id_seq OWNED BY public.wb_operations.id;

--
-- Name: workflow_definitions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.workflow_definitions (
    id integer NOT NULL,
    owner_id bigint NOT NULL,
    name text NOT NULL,
    description text,
    steps jsonb DEFAULT '[]'::jsonb NOT NULL,
    is_active boolean DEFAULT true,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    bot_id bigint
);

--
-- Name: workflow_definitions_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.workflow_definitions_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: workflow_definitions_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.workflow_definitions_id_seq OWNED BY public.workflow_definitions.id;

--
-- Name: workflow_runs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.workflow_runs (
    id integer NOT NULL,
    owner_id bigint NOT NULL,
    workflow_id integer,
    status text DEFAULT 'pending'::text,
    input_data jsonb DEFAULT '{}'::jsonb,
    output_data jsonb DEFAULT '{}'::jsonb,
    current_step integer DEFAULT 0,
    total_steps integer DEFAULT 0,
    error_message text,
    started_at timestamp with time zone,
    finished_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now()
);

--
-- Name: workflow_runs_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.workflow_runs_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: workflow_runs_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.workflow_runs_id_seq OWNED BY public.workflow_runs.id;

--
-- Name: workspace_invites; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.workspace_invites (
    id bigint NOT NULL,
    workspace_id bigint NOT NULL,
    invite_code text NOT NULL,
    created_by bigint NOT NULL,
    uses_left integer DEFAULT 1,
    expires_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now()
);

--
-- Name: workspace_invites_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.workspace_invites_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: workspace_invites_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.workspace_invites_id_seq OWNED BY public.workspace_invites.id;

--
-- Name: workspace_members; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.workspace_members (
    id bigint NOT NULL,
    workspace_id bigint NOT NULL,
    user_id bigint NOT NULL,
    role text DEFAULT 'member'::text NOT NULL,
    invited_by bigint,
    joined_at timestamp with time zone DEFAULT now()
);

--
-- Name: workspace_members_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.workspace_members_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: workspace_members_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.workspace_members_id_seq OWNED BY public.workspace_members.id;

--
-- Name: workspaces; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.workspaces (
    id bigint NOT NULL,
    owner_id bigint NOT NULL,
    name text NOT NULL,
    description text,
    plan text DEFAULT 'enterprise'::text,
    is_active boolean DEFAULT true,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now()
);

--
-- Name: workspaces_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.workspaces_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: workspaces_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.workspaces_id_seq OWNED BY public.workspaces.id;

--
-- Name: account_flood_log id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.account_flood_log ALTER COLUMN id SET DEFAULT nextval('public.account_flood_log_id_seq'::regclass);

--
-- Name: account_health_history id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.account_health_history ALTER COLUMN id SET DEFAULT nextval('public.account_health_history_id_seq'::regclass);

--
-- Name: account_status_events id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.account_status_events ALTER COLUMN id SET DEFAULT nextval('public.account_status_events_id_seq'::regclass);

--
-- Name: account_trust_history id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.account_trust_history ALTER COLUMN id SET DEFAULT nextval('public.account_trust_history_id_seq'::regclass);

--
-- Name: account_usage_log id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.account_usage_log ALTER COLUMN id SET DEFAULT nextval('public.account_usage_log_id_seq'::regclass);

--
-- Name: account_warmup_log id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.account_warmup_log ALTER COLUMN id SET DEFAULT nextval('public.account_warmup_log_id_seq'::regclass);

--
-- Name: account_warmup_plans id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.account_warmup_plans ALTER COLUMN id SET DEFAULT nextval('public.account_warmup_plans_id_seq'::regclass);

--
-- Name: activity_log id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.activity_log ALTER COLUMN id SET DEFAULT nextval('public.activity_log_id_seq'::regclass);

--
-- Name: ad_advertisers id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ad_advertisers ALTER COLUMN id SET DEFAULT nextval('public.ad_advertisers_id_seq'::regclass);

--
-- Name: ad_placements id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ad_placements ALTER COLUMN id SET DEFAULT nextval('public.ad_placements_id_seq'::regclass);

--
-- Name: ad_posts_log id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ad_posts_log ALTER COLUMN id SET DEFAULT nextval('public.ad_posts_log_id_seq'::regclass);

--
-- Name: adaptive_pacing_log id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.adaptive_pacing_log ALTER COLUMN id SET DEFAULT nextval('public.adaptive_pacing_log_id_seq'::regclass);

--
-- Name: admin_audit_log id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.admin_audit_log ALTER COLUMN id SET DEFAULT nextval('public.admin_audit_log_id_seq'::regclass);

--
-- Name: anomaly_events id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.anomaly_events ALTER COLUMN id SET DEFAULT nextval('public.anomaly_events_id_seq'::regclass);

--
-- Name: api_keys id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.api_keys ALTER COLUMN id SET DEFAULT nextval('public.api_keys_id_seq'::regclass);

--
-- Name: asset_templates id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.asset_templates ALTER COLUMN id SET DEFAULT nextval('public.asset_templates_id_seq'::regclass);

--
-- Name: audience_dna id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.audience_dna ALTER COLUMN id SET DEFAULT nextval('public.audience_dna_id_seq'::regclass);

--
-- Name: audience_overlaps id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.audience_overlaps ALTER COLUMN id SET DEFAULT nextval('public.audience_overlaps_id_seq'::regclass);

--
-- Name: auto_funnel_runs id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auto_funnel_runs ALTER COLUMN id SET DEFAULT nextval('public.auto_funnel_runs_id_seq'::regclass);

--
-- Name: auto_funnel_steps id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auto_funnel_steps ALTER COLUMN id SET DEFAULT nextval('public.auto_funnel_steps_id_seq'::regclass);

--
-- Name: auto_funnels id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auto_funnels ALTER COLUMN id SET DEFAULT nextval('public.auto_funnels_id_seq'::regclass);

--
-- Name: auto_replies id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auto_replies ALTER COLUMN id SET DEFAULT nextval('public.auto_replies_id_seq'::regclass);

--
-- Name: auto_reply_log id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auto_reply_log ALTER COLUMN id SET DEFAULT nextval('public.auto_reply_log_id_seq'::regclass);

--
-- Name: automation_rules id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.automation_rules ALTER COLUMN id SET DEFAULT nextval('public.automation_rules_id_seq'::regclass);

--
-- Name: behavioral_events id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.behavioral_events ALTER COLUMN id SET DEFAULT nextval('public.behavioral_events_id_seq'::regclass);

--
-- Name: bm_node_threads id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bm_node_threads ALTER COLUMN id SET DEFAULT nextval('public.bm_node_threads_id_seq'::regclass);

--
-- Name: bm_telegram_nodes id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bm_telegram_nodes ALTER COLUMN id SET DEFAULT nextval('public.bm_telegram_nodes_id_seq'::regclass);

--
-- Name: booster_sessions id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.booster_sessions ALTER COLUMN id SET DEFAULT nextval('public.booster_sessions_id_seq'::regclass);

--
-- Name: bot_deep_links id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bot_deep_links ALTER COLUMN id SET DEFAULT nextval('public.bot_deep_links_id_seq'::regclass);

--
-- Name: bot_flood_events id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bot_flood_events ALTER COLUMN id SET DEFAULT nextval('public.bot_flood_events_id_seq'::regclass);

--
-- Name: bot_mesh_hops id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bot_mesh_hops ALTER COLUMN id SET DEFAULT nextval('public.bot_mesh_hops_id_seq'::regclass);

--
-- Name: bot_routing_weights id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bot_routing_weights ALTER COLUMN id SET DEFAULT nextval('public.bot_routing_weights_id_seq'::regclass);

--
-- Name: bot_sales_delivery id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bot_sales_delivery ALTER COLUMN id SET DEFAULT nextval('public.bot_sales_delivery_id_seq'::regclass);

--
-- Name: bot_sales_dialogs id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bot_sales_dialogs ALTER COLUMN id SET DEFAULT nextval('public.bot_sales_dialogs_id_seq'::regclass);

--
-- Name: bot_sales_examples id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bot_sales_examples ALTER COLUMN id SET DEFAULT nextval('public.bot_sales_examples_id_seq'::regclass);

--
-- Name: bot_sales_faq id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bot_sales_faq ALTER COLUMN id SET DEFAULT nextval('public.bot_sales_faq_id_seq'::regclass);

--
-- Name: bot_sales_orders id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bot_sales_orders ALTER COLUMN id SET DEFAULT nextval('public.bot_sales_orders_id_seq'::regclass);

--
-- Name: bot_sales_personas id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bot_sales_personas ALTER COLUMN id SET DEFAULT nextval('public.bot_sales_personas_id_seq'::regclass);

--
-- Name: bot_sales_products id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bot_sales_products ALTER COLUMN id SET DEFAULT nextval('public.bot_sales_products_id_seq'::regclass);

--
-- Name: bot_sales_promos id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bot_sales_promos ALTER COLUMN id SET DEFAULT nextval('public.bot_sales_promos_id_seq'::regclass);

--
-- Name: bot_user_facts id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bot_user_facts ALTER COLUMN id SET DEFAULT nextval('public.bot_user_facts_id_seq'::regclass);

--
-- Name: bot_user_memory id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bot_user_memory ALTER COLUMN id SET DEFAULT nextval('public.bot_user_memory_id_seq'::regclass);

--
-- Name: bot_users id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bot_users ALTER COLUMN id SET DEFAULT nextval('public.bot_users_id_seq'::regclass);

--
-- Name: bot_warehouse id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bot_warehouse ALTER COLUMN id SET DEFAULT nextval('public.bot_warehouse_id_seq'::regclass);

--
-- Name: botmother_memory id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.botmother_memory ALTER COLUMN id SET DEFAULT nextval('public.botmother_memory_id_seq'::regclass);

--
-- Name: broadcast_campaigns id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.broadcast_campaigns ALTER COLUMN id SET DEFAULT nextval('public.broadcast_campaigns_id_seq'::regclass);

--
-- Name: broadcasts id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.broadcasts ALTER COLUMN id SET DEFAULT nextval('public.broadcasts_id_seq'::regclass);

--
-- Name: bulk_edit_snapshots id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bulk_edit_snapshots ALTER COLUMN id SET DEFAULT nextval('public.bulk_edit_snapshots_id_seq'::regclass);

--
-- Name: cf_worker_pool id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cf_worker_pool ALTER COLUMN id SET DEFAULT nextval('public.cf_worker_pool_id_seq'::regclass);

--
-- Name: channel_identity_history id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.channel_identity_history ALTER COLUMN id SET DEFAULT nextval('public.channel_identity_history_id_seq'::regclass);

--
-- Name: channel_member_history id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.channel_member_history ALTER COLUMN id SET DEFAULT nextval('public.channel_member_history_id_seq'::regclass);

--
-- Name: channel_search_rankings id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.channel_search_rankings ALTER COLUMN id SET DEFAULT nextval('public.channel_search_rankings_id_seq'::regclass);

--
-- Name: channel_tracked_keywords id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.channel_tracked_keywords ALTER COLUMN id SET DEFAULT nextval('public.channel_tracked_keywords_id_seq'::regclass);

--
-- Name: chat_warmup_log id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chat_warmup_log ALTER COLUMN id SET DEFAULT nextval('public.chat_warmup_log_id_seq'::regclass);

--
-- Name: chat_warmup_sessions id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chat_warmup_sessions ALTER COLUMN id SET DEFAULT nextval('public.chat_warmup_sessions_id_seq'::regclass);

--
-- Name: chatlist_folders id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chatlist_folders ALTER COLUMN id SET DEFAULT nextval('public.chatlist_folders_id_seq'::regclass);

--
-- Name: clone_adapt_history id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.clone_adapt_history ALTER COLUMN id SET DEFAULT nextval('public.clone_adapt_history_id_seq'::regclass);

--
-- Name: clusters id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.clusters ALTER COLUMN id SET DEFAULT nextval('public.clusters_id_seq'::regclass);

--
-- Name: commission_ledger id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.commission_ledger ALTER COLUMN id SET DEFAULT nextval('public.commission_ledger_id_seq'::regclass);

--
-- Name: community_channels id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.community_channels ALTER COLUMN id SET DEFAULT nextval('public.community_channels_id_seq'::regclass);

--
-- Name: community_node_members id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.community_node_members ALTER COLUMN id SET DEFAULT nextval('public.community_node_members_id_seq'::regclass);

--
-- Name: community_nodes id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.community_nodes ALTER COLUMN id SET DEFAULT nextval('public.community_nodes_id_seq'::regclass);

--
-- Name: competitors id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.competitors ALTER COLUMN id SET DEFAULT nextval('public.competitors_id_seq'::regclass);

--
-- Name: compliance_audit id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.compliance_audit ALTER COLUMN id SET DEFAULT nextval('public.compliance_audit_id_seq'::regclass);

--
-- Name: contact_bulk_ops id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.contact_bulk_ops ALTER COLUMN id SET DEFAULT nextval('public.contact_bulk_ops_id_seq'::regclass);

--
-- Name: contact_conflicts id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.contact_conflicts ALTER COLUMN id SET DEFAULT nextval('public.contact_conflicts_id_seq'::regclass);

--
-- Name: contact_crm id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.contact_crm ALTER COLUMN id SET DEFAULT nextval('public.contact_crm_id_seq'::regclass);

--
-- Name: contact_crm_activity id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.contact_crm_activity ALTER COLUMN id SET DEFAULT nextval('public.contact_crm_activity_id_seq'::regclass);

--
-- Name: contact_export_jobs id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.contact_export_jobs ALTER COLUMN id SET DEFAULT nextval('public.contact_export_jobs_id_seq'::regclass);

--
-- Name: contact_groups id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.contact_groups ALTER COLUMN id SET DEFAULT nextval('public.contact_groups_id_seq'::regclass);

--
-- Name: contact_history id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.contact_history ALTER COLUMN id SET DEFAULT nextval('public.contact_history_id_seq'::regclass);

--
-- Name: contact_identity_graph id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.contact_identity_graph ALTER COLUMN id SET DEFAULT nextval('public.contact_identity_graph_id_seq'::regclass);

--
-- Name: contact_relationships id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.contact_relationships ALTER COLUMN id SET DEFAULT nextval('public.contact_relationships_id_seq'::regclass);

--
-- Name: contact_smart_tags id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.contact_smart_tags ALTER COLUMN id SET DEFAULT nextval('public.contact_smart_tags_id_seq'::regclass);

--
-- Name: contact_sources id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.contact_sources ALTER COLUMN id SET DEFAULT nextval('public.contact_sources_id_seq'::regclass);

--
-- Name: contact_sync_log id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.contact_sync_log ALTER COLUMN id SET DEFAULT nextval('public.contact_sync_log_id_seq'::regclass);

--
-- Name: contact_sync_state id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.contact_sync_state ALTER COLUMN id SET DEFAULT nextval('public.contact_sync_state_id_seq'::regclass);

--
-- Name: contact_versions id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.contact_versions ALTER COLUMN id SET DEFAULT nextval('public.contact_versions_id_seq'::regclass);

--
-- Name: content_meshes id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.content_meshes ALTER COLUMN id SET DEFAULT nextval('public.content_meshes_id_seq'::regclass);

--
-- Name: content_performance id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.content_performance ALTER COLUMN id SET DEFAULT nextval('public.content_performance_id_seq'::regclass);

--
-- Name: crm_activity id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.crm_activity ALTER COLUMN id SET DEFAULT nextval('public.crm_activity_id_seq'::regclass);

--
-- Name: crm_contacts id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.crm_contacts ALTER COLUMN id SET DEFAULT nextval('public.crm_contacts_id_seq'::regclass);

--
-- Name: crm_deals id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.crm_deals ALTER COLUMN id SET DEFAULT nextval('public.crm_deals_id_seq'::regclass);

--
-- Name: crosspost_links id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.crosspost_links ALTER COLUMN id SET DEFAULT nextval('public.crosspost_links_id_seq'::regclass);

--
-- Name: daughter_groups id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.daughter_groups ALTER COLUMN id SET DEFAULT nextval('public.daughter_groups_id_seq'::regclass);

--
-- Name: deep_link_visits id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.deep_link_visits ALTER COLUMN id SET DEFAULT nextval('public.deep_link_visits_id_seq'::regclass);

--
-- Name: deploy_log id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.deploy_log ALTER COLUMN id SET DEFAULT nextval('public.deploy_log_id_seq'::regclass);

--
-- Name: device_pairings id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.device_pairings ALTER COLUMN id SET DEFAULT nextval('public.device_pairings_id_seq'::regclass);

--
-- Name: discovered_bots id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.discovered_bots ALTER COLUMN id SET DEFAULT nextval('public.discovered_bots_id_seq'::regclass);

--
-- Name: dm_campaign_log id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dm_campaign_log ALTER COLUMN id SET DEFAULT nextval('public.dm_campaign_log_id_seq'::regclass);

--
-- Name: dm_campaigns id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dm_campaigns ALTER COLUMN id SET DEFAULT nextval('public.dm_campaigns_id_seq'::regclass);

--
-- Name: ecosystem_dna id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ecosystem_dna ALTER COLUMN id SET DEFAULT nextval('public.ecosystem_dna_id_seq'::regclass);

--
-- Name: ecosystem_drift_log id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ecosystem_drift_log ALTER COLUMN id SET DEFAULT nextval('public.ecosystem_drift_log_id_seq'::regclass);

--
-- Name: ecosystem_events id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ecosystem_events ALTER COLUMN id SET DEFAULT nextval('public.ecosystem_events_id_seq'::regclass);

--
-- Name: ecosystem_members id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ecosystem_members ALTER COLUMN id SET DEFAULT nextval('public.ecosystem_members_id_seq'::regclass);

--
-- Name: ecosystems id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ecosystems ALTER COLUMN id SET DEFAULT nextval('public.ecosystems_id_seq'::regclass);

--
-- Name: entity_follow_events id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.entity_follow_events ALTER COLUMN id SET DEFAULT nextval('public.entity_follow_events_id_seq'::regclass);

--
-- Name: entity_follows id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.entity_follows ALTER COLUMN id SET DEFAULT nextval('public.entity_follows_id_seq'::regclass);

--
-- Name: entity_name_history id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.entity_name_history ALTER COLUMN id SET DEFAULT nextval('public.entity_name_history_id_seq'::regclass);

--
-- Name: error_reports id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.error_reports ALTER COLUMN id SET DEFAULT nextval('public.error_reports_id_seq'::regclass);

--
-- Name: experiment_assignments id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.experiment_assignments ALTER COLUMN id SET DEFAULT nextval('public.experiment_assignments_id_seq'::regclass);

--
-- Name: experiment_variants id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.experiment_variants ALTER COLUMN id SET DEFAULT nextval('public.experiment_variants_id_seq'::regclass);

--
-- Name: experiments id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.experiments ALTER COLUMN id SET DEFAULT nextval('public.experiments_id_seq'::regclass);

--
-- Name: funnel_steps id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.funnel_steps ALTER COLUMN id SET DEFAULT nextval('public.funnel_steps_id_seq'::regclass);

--
-- Name: funnel_subscriptions id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.funnel_subscriptions ALTER COLUMN id SET DEFAULT nextval('public.funnel_subscriptions_id_seq'::regclass);

--
-- Name: funnels id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.funnels ALTER COLUMN id SET DEFAULT nextval('public.funnels_id_seq'::regclass);

--
-- Name: ghost_action_log id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ghost_action_log ALTER COLUMN id SET DEFAULT nextval('public.ghost_action_log_id_seq'::regclass);

--
-- Name: ghost_profiles id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ghost_profiles ALTER COLUMN id SET DEFAULT nextval('public.ghost_profiles_id_seq'::regclass);

--
-- Name: gift_inventory id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.gift_inventory ALTER COLUMN id SET DEFAULT nextval('public.gift_inventory_id_seq'::regclass);

--
-- Name: gift_recipients id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.gift_recipients ALTER COLUMN id SET DEFAULT nextval('public.gift_recipients_id_seq'::regclass);

--
-- Name: gift_transfer_items id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.gift_transfer_items ALTER COLUMN id SET DEFAULT nextval('public.gift_transfer_items_id_seq'::regclass);

--
-- Name: gift_transfer_ops id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.gift_transfer_ops ALTER COLUMN id SET DEFAULT nextval('public.gift_transfer_ops_id_seq'::regclass);

--
-- Name: gift_transfer_plans id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.gift_transfer_plans ALTER COLUMN id SET DEFAULT nextval('public.gift_transfer_plans_id_seq'::regclass);

--
-- Name: gift_transfer_reports id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.gift_transfer_reports ALTER COLUMN id SET DEFAULT nextval('public.gift_transfer_reports_id_seq'::regclass);

--
-- Name: global_presence_plans id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.global_presence_plans ALTER COLUMN id SET DEFAULT nextval('public.global_presence_plans_id_seq'::regclass);

--
-- Name: global_presence_targets id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.global_presence_targets ALTER COLUMN id SET DEFAULT nextval('public.global_presence_targets_id_seq'::regclass);

--
-- Name: graph_edges id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.graph_edges ALTER COLUMN id SET DEFAULT nextval('public.graph_edges_id_seq'::regclass);

--
-- Name: graph_nodes id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.graph_nodes ALTER COLUMN id SET DEFAULT nextval('public.graph_nodes_id_seq'::regclass);

--
-- Name: growth_actions id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.growth_actions ALTER COLUMN id SET DEFAULT nextval('public.growth_actions_id_seq'::regclass);

--
-- Name: growth_content_seeds id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.growth_content_seeds ALTER COLUMN id SET DEFAULT nextval('public.growth_content_seeds_id_seq'::regclass);

--
-- Name: growth_goals id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.growth_goals ALTER COLUMN id SET DEFAULT nextval('public.growth_goals_id_seq'::regclass);

--
-- Name: growth_reports id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.growth_reports ALTER COLUMN id SET DEFAULT nextval('public.growth_reports_id_seq'::regclass);

--
-- Name: growth_schedule id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.growth_schedule ALTER COLUMN id SET DEFAULT nextval('public.growth_schedule_id_seq'::regclass);

--
-- Name: guard_chats id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.guard_chats ALTER COLUMN id SET DEFAULT nextval('public.guard_chats_id_seq'::regclass);

--
-- Name: host_offerings id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.host_offerings ALTER COLUMN id SET DEFAULT nextval('public.host_offerings_id_seq'::regclass);

--
-- Name: host_rentals id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.host_rentals ALTER COLUMN id SET DEFAULT nextval('public.host_rentals_id_seq'::regclass);

--
-- Name: immunity_policy id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.immunity_policy ALTER COLUMN id SET DEFAULT nextval('public.immunity_policy_id_seq'::regclass);

--
-- Name: immunity_rules id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.immunity_rules ALTER COLUMN id SET DEFAULT nextval('public.immunity_rules_id_seq'::regclass);

--
-- Name: inactivity_alerts_sent id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.inactivity_alerts_sent ALTER COLUMN id SET DEFAULT nextval('public.inactivity_alerts_sent_id_seq'::regclass);

--
-- Name: infrastructure_alerts id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.infrastructure_alerts ALTER COLUMN id SET DEFAULT nextval('public.infrastructure_alerts_id_seq'::regclass);

--
-- Name: intents id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.intents ALTER COLUMN id SET DEFAULT nextval('public.intents_id_seq'::regclass);

--
-- Name: invite_overflow_channels id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.invite_overflow_channels ALTER COLUMN id SET DEFAULT nextval('public.invite_overflow_channels_id_seq'::regclass);

--
-- Name: keyword_hits id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.keyword_hits ALTER COLUMN id SET DEFAULT nextval('public.keyword_hits_id_seq'::regclass);

--
-- Name: keyword_stats id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.keyword_stats ALTER COLUMN id SET DEFAULT nextval('public.keyword_stats_id_seq'::regclass);

--
-- Name: keyword_watchers id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.keyword_watchers ALTER COLUMN id SET DEFAULT nextval('public.keyword_watchers_id_seq'::regclass);

--
-- Name: managed_bots id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.managed_bots ALTER COLUMN id SET DEFAULT nextval('public.managed_bots_id_seq'::regclass);

--
-- Name: managed_channels id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.managed_channels ALTER COLUMN id SET DEFAULT nextval('public.managed_channels_id_seq'::regclass);

--
-- Name: mesh_queue id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mesh_queue ALTER COLUMN id SET DEFAULT nextval('public.mesh_queue_id_seq'::regclass);

--
-- Name: mesh_targets id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mesh_targets ALTER COLUMN id SET DEFAULT nextval('public.mesh_targets_id_seq'::regclass);

--
-- Name: message_templates id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.message_templates ALTER COLUMN id SET DEFAULT nextval('public.message_templates_id_seq'::regclass);

--
-- Name: mp_audit_log id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mp_audit_log ALTER COLUMN id SET DEFAULT nextval('public.mp_audit_log_id_seq'::regclass);

--
-- Name: mp_provider_api_keys id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mp_provider_api_keys ALTER COLUMN id SET DEFAULT nextval('public.mp_provider_api_keys_id_seq'::regclass);

--
-- Name: mp_providers id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mp_providers ALTER COLUMN id SET DEFAULT nextval('public.mp_providers_id_seq'::regclass);

--
-- Name: mp_services id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mp_services ALTER COLUMN id SET DEFAULT nextval('public.mp_services_id_seq'::regclass);

--
-- Name: narrative_campaigns id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.narrative_campaigns ALTER COLUMN id SET DEFAULT nextval('public.narrative_campaigns_id_seq'::regclass);

--
-- Name: narrative_posts id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.narrative_posts ALTER COLUMN id SET DEFAULT nextval('public.narrative_posts_id_seq'::regclass);

--
-- Name: network_edges id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.network_edges ALTER COLUMN id SET DEFAULT nextval('public.network_edges_id_seq'::regclass);

--
-- Name: network_instances id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.network_instances ALTER COLUMN id SET DEFAULT nextval('public.network_instances_id_seq'::regclass);

--
-- Name: network_nodes id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.network_nodes ALTER COLUMN id SET DEFAULT nextval('public.network_nodes_id_seq'::regclass);

--
-- Name: network_templates id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.network_templates ALTER COLUMN id SET DEFAULT nextval('public.network_templates_id_seq'::regclass);

--
-- Name: niche_growth_targets id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.niche_growth_targets ALTER COLUMN id SET DEFAULT nextval('public.niche_growth_targets_id_seq'::regclass);

--
-- Name: notary_observations id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notary_observations ALTER COLUMN id SET DEFAULT nextval('public.notary_observations_id_seq'::regclass);

--
-- Name: notary_watches id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notary_watches ALTER COLUMN id SET DEFAULT nextval('public.notary_watches_id_seq'::regclass);

--
-- Name: op_telemetry id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.op_telemetry ALTER COLUMN id SET DEFAULT nextval('public.op_telemetry_id_seq'::regclass);

--
-- Name: operation_audit id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.operation_audit ALTER COLUMN id SET DEFAULT nextval('public.operation_audit_id_seq'::regclass);

--
-- Name: operation_log id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.operation_log ALTER COLUMN id SET DEFAULT nextval('public.operation_log_id_seq'::regclass);

--
-- Name: operation_queue id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.operation_queue ALTER COLUMN id SET DEFAULT nextval('public.operation_queue_id_seq'::regclass);

--
-- Name: organism_events id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.organism_events ALTER COLUMN id SET DEFAULT nextval('public.organism_events_id_seq'::regclass);

--
-- Name: parsed_audiences id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.parsed_audiences ALTER COLUMN id SET DEFAULT nextval('public.parsed_audiences_id_seq'::regclass);

--
-- Name: parser_runs id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.parser_runs ALTER COLUMN id SET DEFAULT nextval('public.parser_runs_id_seq'::regclass);

--
-- Name: payments id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payments ALTER COLUMN id SET DEFAULT nextval('public.payments_id_seq'::regclass);

--
-- Name: payout_requests id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payout_requests ALTER COLUMN id SET DEFAULT nextval('public.payout_requests_id_seq'::regclass);

--
-- Name: persona_memory id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.persona_memory ALTER COLUMN id SET DEFAULT nextval('public.persona_memory_id_seq'::regclass);

--
-- Name: persona_profiles id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.persona_profiles ALTER COLUMN id SET DEFAULT nextval('public.persona_profiles_id_seq'::regclass);

--
-- Name: platform_proxy_pool id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.platform_proxy_pool ALTER COLUMN id SET DEFAULT nextval('public.platform_proxy_pool_id_seq'::regclass);

--
-- Name: platform_referrals id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.platform_referrals ALTER COLUMN id SET DEFAULT nextval('public.platform_referrals_id_seq'::regclass);

--
-- Name: position_history id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.position_history ALTER COLUMN id SET DEFAULT nextval('public.position_history_id_seq'::regclass);

--
-- Name: post_templates id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.post_templates ALTER COLUMN id SET DEFAULT nextval('public.post_templates_id_seq'::regclass);

--
-- Name: presence_packs id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.presence_packs ALTER COLUMN id SET DEFAULT nextval('public.presence_packs_id_seq'::regclass);

--
-- Name: promo_logs id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.promo_logs ALTER COLUMN id SET DEFAULT nextval('public.promo_logs_id_seq'::regclass);

--
-- Name: promo_orders id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.promo_orders ALTER COLUMN id SET DEFAULT nextval('public.promo_orders_id_seq'::regclass);

--
-- Name: proxy_health_log id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.proxy_health_log ALTER COLUMN id SET DEFAULT nextval('public.proxy_health_log_id_seq'::regclass);

--
-- Name: proxy_performance id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.proxy_performance ALTER COLUMN id SET DEFAULT nextval('public.proxy_performance_id_seq'::regclass);

--
-- Name: proxy_quality_log id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.proxy_quality_log ALTER COLUMN id SET DEFAULT nextval('public.proxy_quality_log_id_seq'::regclass);

--
-- Name: ranking_alerts id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ranking_alerts ALTER COLUMN id SET DEFAULT nextval('public.ranking_alerts_id_seq'::regclass);

--
-- Name: recovery_events id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.recovery_events ALTER COLUMN id SET DEFAULT nextval('public.recovery_events_id_seq'::regclass);

--
-- Name: referral_conversions id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.referral_conversions ALTER COLUMN id SET DEFAULT nextval('public.referral_conversions_id_seq'::regclass);

--
-- Name: referral_rewards id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.referral_rewards ALTER COLUMN id SET DEFAULT nextval('public.referral_rewards_id_seq'::regclass);

--
-- Name: referrals id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.referrals ALTER COLUMN id SET DEFAULT nextval('public.referrals_id_seq'::regclass);

--
-- Name: relay_messages id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.relay_messages ALTER COLUMN id SET DEFAULT nextval('public.relay_messages_id_seq'::regclass);

--
-- Name: relay_sessions id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.relay_sessions ALTER COLUMN id SET DEFAULT nextval('public.relay_sessions_id_seq'::regclass);

--
-- Name: resource_activity_log id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_activity_log ALTER COLUMN id SET DEFAULT nextval('public.resource_activity_log_id_seq'::regclass);

--
-- Name: resource_activity_sessions id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_activity_sessions ALTER COLUMN id SET DEFAULT nextval('public.resource_activity_sessions_id_seq'::regclass);

--
-- Name: restriction_events id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.restriction_events ALTER COLUMN id SET DEFAULT nextval('public.restriction_events_id_seq'::regclass);

--
-- Name: routing_log id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.routing_log ALTER COLUMN id SET DEFAULT nextval('public.routing_log_id_seq'::regclass);

--
-- Name: routing_rules id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.routing_rules ALTER COLUMN id SET DEFAULT nextval('public.routing_rules_id_seq'::regclass);

--
-- Name: saved_segments id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.saved_segments ALTER COLUMN id SET DEFAULT nextval('public.saved_segments_id_seq'::regclass);

--
-- Name: scheduled_broadcasts id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.scheduled_broadcasts ALTER COLUMN id SET DEFAULT nextval('public.scheduled_broadcasts_id_seq'::regclass);

--
-- Name: search_change_events id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.search_change_events ALTER COLUMN id SET DEFAULT nextval('public.search_change_events_id_seq'::regclass);

--
-- Name: search_keywords id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.search_keywords ALTER COLUMN id SET DEFAULT nextval('public.search_keywords_id_seq'::regclass);

--
-- Name: search_observations id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.search_observations ALTER COLUMN id SET DEFAULT nextval('public.search_observations_id_seq'::regclass);

--
-- Name: search_rankings id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.search_rankings ALTER COLUMN id SET DEFAULT nextval('public.search_rankings_id_seq'::regclass);

--
-- Name: security_violations id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.security_violations ALTER COLUMN id SET DEFAULT nextval('public.security_violations_id_seq'::regclass);

--
-- Name: self_promo_runs id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.self_promo_runs ALTER COLUMN id SET DEFAULT nextval('public.self_promo_runs_id_seq'::regclass);

--
-- Name: self_promo_templates id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.self_promo_templates ALTER COLUMN id SET DEFAULT nextval('public.self_promo_templates_id_seq'::regclass);

--
-- Name: seo_score_history id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.seo_score_history ALTER COLUMN id SET DEFAULT nextval('public.seo_score_history_id_seq'::regclass);

--
-- Name: shield_actions id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.shield_actions ALTER COLUMN id SET DEFAULT nextval('public.shield_actions_id_seq'::regclass);

--
-- Name: showcase_layers id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.showcase_layers ALTER COLUMN id SET DEFAULT nextval('public.showcase_layers_id_seq'::regclass);

--
-- Name: smart_tag_rules id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.smart_tag_rules ALTER COLUMN id SET DEFAULT nextval('public.smart_tag_rules_id_seq'::regclass);

--
-- Name: smm_panels id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.smm_panels ALTER COLUMN id SET DEFAULT nextval('public.smm_panels_id_seq'::regclass);

--
-- Name: stars_events id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.stars_events ALTER COLUMN id SET DEFAULT nextval('public.stars_events_id_seq'::regclass);

--
-- Name: stars_experiments id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.stars_experiments ALTER COLUMN id SET DEFAULT nextval('public.stars_experiments_id_seq'::regclass);

--
-- Name: stars_transactions id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.stars_transactions ALTER COLUMN id SET DEFAULT nextval('public.stars_transactions_id_seq'::regclass);

--
-- Name: strike_email_accounts id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.strike_email_accounts ALTER COLUMN id SET DEFAULT nextval('public.strike_email_accounts_id_seq'::regclass);

--
-- Name: strike_history id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.strike_history ALTER COLUMN id SET DEFAULT nextval('public.strike_history_id_seq'::regclass);

--
-- Name: strike_reports id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.strike_reports ALTER COLUMN id SET DEFAULT nextval('public.strike_reports_id_seq'::regclass);

--
-- Name: subscription_gate_channels id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.subscription_gate_channels ALTER COLUMN id SET DEFAULT nextval('public.subscription_gate_channels_id_seq'::regclass);

--
-- Name: subscriptions id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.subscriptions ALTER COLUMN id SET DEFAULT nextval('public.subscriptions_id_seq'::regclass);

--
-- Name: system_health_snapshots id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_health_snapshots ALTER COLUMN id SET DEFAULT nextval('public.system_health_snapshots_id_seq'::regclass);

--
-- Name: tg_accounts id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tg_accounts ALTER COLUMN id SET DEFAULT nextval('public.tg_accounts_id_seq'::regclass);

--
-- Name: tg_cloud_chunk_locs id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tg_cloud_chunk_locs ALTER COLUMN id SET DEFAULT nextval('public.tg_cloud_chunk_locs_id_seq'::regclass);

--
-- Name: tg_cloud_chunks id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tg_cloud_chunks ALTER COLUMN id SET DEFAULT nextval('public.tg_cloud_chunks_id_seq'::regclass);

--
-- Name: tg_cloud_files id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tg_cloud_files ALTER COLUMN id SET DEFAULT nextval('public.tg_cloud_files_id_seq'::regclass);

--
-- Name: tracked_keywords id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tracked_keywords ALTER COLUMN id SET DEFAULT nextval('public.tracked_keywords_id_seq'::regclass);

--
-- Name: user_activity id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_activity ALTER COLUMN id SET DEFAULT nextval('public.user_activity_id_seq'::regclass);

--
-- Name: user_payment_methods id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_payment_methods ALTER COLUMN id SET DEFAULT nextval('public.user_payment_methods_id_seq'::regclass);

--
-- Name: user_proxies id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_proxies ALTER COLUMN id SET DEFAULT nextval('public.user_proxies_id_seq'::regclass);

--
-- Name: user_tags id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_tags ALTER COLUMN id SET DEFAULT nextval('public.user_tags_id_seq'::regclass);

--
-- Name: va_admin_drafts id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.va_admin_drafts ALTER COLUMN id SET DEFAULT nextval('public.va_admin_drafts_id_seq'::regclass);

--
-- Name: va_admin_events id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.va_admin_events ALTER COLUMN id SET DEFAULT nextval('public.va_admin_events_id_seq'::regclass);

--
-- Name: va_admin_plan id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.va_admin_plan ALTER COLUMN id SET DEFAULT nextval('public.va_admin_plan_id_seq'::regclass);

--
-- Name: va_channel_admin id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.va_channel_admin ALTER COLUMN id SET DEFAULT nextval('public.va_channel_admin_id_seq'::regclass);

--
-- Name: va_channel_brain id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.va_channel_brain ALTER COLUMN id SET DEFAULT nextval('public.va_channel_brain_id_seq'::regclass);

--
-- Name: va_channel_posts id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.va_channel_posts ALTER COLUMN id SET DEFAULT nextval('public.va_channel_posts_id_seq'::regclass);

--
-- Name: va_learning_decisions id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.va_learning_decisions ALTER COLUMN id SET DEFAULT nextval('public.va_learning_decisions_id_seq'::regclass);

--
-- Name: va_media id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.va_media ALTER COLUMN id SET DEFAULT nextval('public.va_media_id_seq'::regclass);

--
-- Name: va_news_inbox id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.va_news_inbox ALTER COLUMN id SET DEFAULT nextval('public.va_news_inbox_id_seq'::regclass);

--
-- Name: va_reference_channels id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.va_reference_channels ALTER COLUMN id SET DEFAULT nextval('public.va_reference_channels_id_seq'::regclass);

--
-- Name: va_tracked_links id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.va_tracked_links ALTER COLUMN id SET DEFAULT nextval('public.va_tracked_links_id_seq'::regclass);

--
-- Name: vault_intent_hits id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vault_intent_hits ALTER COLUMN id SET DEFAULT nextval('public.vault_intent_hits_id_seq'::regclass);

--
-- Name: vault_intent_rules id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vault_intent_rules ALTER COLUMN id SET DEFAULT nextval('public.vault_intent_rules_id_seq'::regclass);

--
-- Name: vault_messages id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vault_messages ALTER COLUMN id SET DEFAULT nextval('public.vault_messages_id_seq'::regclass);

--
-- Name: virtual_state_history id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.virtual_state_history ALTER COLUMN id SET DEFAULT nextval('public.virtual_state_history_id_seq'::regclass);

--
-- Name: warmup_session_log id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.warmup_session_log ALTER COLUMN id SET DEFAULT nextval('public.warmup_session_log_id_seq'::regclass);

--
-- Name: warmup_sessions id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.warmup_sessions ALTER COLUMN id SET DEFAULT nextval('public.warmup_sessions_id_seq'::regclass);

--
-- Name: wb_accounts id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.wb_accounts ALTER COLUMN id SET DEFAULT nextval('public.wb_accounts_id_seq'::regclass);

--
-- Name: wb_operation_targets id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.wb_operation_targets ALTER COLUMN id SET DEFAULT nextval('public.wb_operation_targets_id_seq'::regclass);

--
-- Name: wb_operations id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.wb_operations ALTER COLUMN id SET DEFAULT nextval('public.wb_operations_id_seq'::regclass);

--
-- Name: workflow_definitions id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workflow_definitions ALTER COLUMN id SET DEFAULT nextval('public.workflow_definitions_id_seq'::regclass);

--
-- Name: workflow_runs id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workflow_runs ALTER COLUMN id SET DEFAULT nextval('public.workflow_runs_id_seq'::regclass);

--
-- Name: workspace_invites id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workspace_invites ALTER COLUMN id SET DEFAULT nextval('public.workspace_invites_id_seq'::regclass);

--
-- Name: workspace_members id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workspace_members ALTER COLUMN id SET DEFAULT nextval('public.workspace_members_id_seq'::regclass);

--
-- Name: workspaces id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workspaces ALTER COLUMN id SET DEFAULT nextval('public.workspaces_id_seq'::regclass);

--
-- Name: account_capabilities account_capabilities_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.account_capabilities
    ADD CONSTRAINT account_capabilities_pkey PRIMARY KEY (account_id);

--
-- Name: account_daily_stats account_daily_stats_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.account_daily_stats
    ADD CONSTRAINT account_daily_stats_pkey PRIMARY KEY (account_id, stat_date);

--
-- Name: account_flood_log account_flood_log_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.account_flood_log
    ADD CONSTRAINT account_flood_log_pkey PRIMARY KEY (id);

--
-- Name: account_health_history account_health_history_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.account_health_history
    ADD CONSTRAINT account_health_history_pkey PRIMARY KEY (id);

--
-- Name: account_niche_profiles account_niche_profiles_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.account_niche_profiles
    ADD CONSTRAINT account_niche_profiles_pkey PRIMARY KEY (account_id);

--
-- Name: account_rehab_state account_rehab_state_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.account_rehab_state
    ADD CONSTRAINT account_rehab_state_pkey PRIMARY KEY (acc_id);

--
-- Name: account_risk_scores account_risk_scores_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.account_risk_scores
    ADD CONSTRAINT account_risk_scores_pkey PRIMARY KEY (account_id);

--
-- Name: account_status_events account_status_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.account_status_events
    ADD CONSTRAINT account_status_events_pkey PRIMARY KEY (id);

--
-- Name: account_trust_history account_trust_history_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.account_trust_history
    ADD CONSTRAINT account_trust_history_pkey PRIMARY KEY (id);

--
-- Name: account_usage_log account_usage_log_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.account_usage_log
    ADD CONSTRAINT account_usage_log_pkey PRIMARY KEY (id);

--
-- Name: account_warmup_interests account_warmup_interests_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.account_warmup_interests
    ADD CONSTRAINT account_warmup_interests_pkey PRIMARY KEY (account_id, channel_ref);

--
-- Name: account_warmup_log account_warmup_log_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.account_warmup_log
    ADD CONSTRAINT account_warmup_log_pkey PRIMARY KEY (id);

--
-- Name: account_warmup_plans account_warmup_plans_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.account_warmup_plans
    ADD CONSTRAINT account_warmup_plans_pkey PRIMARY KEY (id);

--
-- Name: activity_log activity_log_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.activity_log
    ADD CONSTRAINT activity_log_pkey PRIMARY KEY (id);

--
-- Name: ad_advertisers ad_advertisers_owner_id_advertiser_username_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ad_advertisers
    ADD CONSTRAINT ad_advertisers_owner_id_advertiser_username_key UNIQUE (owner_id, advertiser_username);

--
-- Name: ad_advertisers ad_advertisers_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ad_advertisers
    ADD CONSTRAINT ad_advertisers_pkey PRIMARY KEY (id);

--
-- Name: ad_placements ad_placements_owner_id_channel_username_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ad_placements
    ADD CONSTRAINT ad_placements_owner_id_channel_username_key UNIQUE (owner_id, channel_username);

--
-- Name: ad_placements ad_placements_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ad_placements
    ADD CONSTRAINT ad_placements_pkey PRIMARY KEY (id);

--
-- Name: ad_posts_log ad_posts_log_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ad_posts_log
    ADD CONSTRAINT ad_posts_log_pkey PRIMARY KEY (id);

--
-- Name: adaptive_pacing_log adaptive_pacing_log_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.adaptive_pacing_log
    ADD CONSTRAINT adaptive_pacing_log_pkey PRIMARY KEY (id);

--
-- Name: admin_audit_log admin_audit_log_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.admin_audit_log
    ADD CONSTRAINT admin_audit_log_pkey PRIMARY KEY (id);

--
-- Name: admin_state admin_state_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.admin_state
    ADD CONSTRAINT admin_state_pkey PRIMARY KEY (admin_id);

--
-- Name: ambassador_status ambassador_status_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ambassador_status
    ADD CONSTRAINT ambassador_status_pkey PRIMARY KEY (user_id);

--
-- Name: ambassador_tiers ambassador_tiers_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ambassador_tiers
    ADD CONSTRAINT ambassador_tiers_pkey PRIMARY KEY (tier_key);

--
-- Name: anomaly_events anomaly_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.anomaly_events
    ADD CONSTRAINT anomaly_events_pkey PRIMARY KEY (id);

--
-- Name: api_keys api_keys_key_hash_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.api_keys
    ADD CONSTRAINT api_keys_key_hash_key UNIQUE (key_hash);

--
-- Name: api_keys api_keys_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.api_keys
    ADD CONSTRAINT api_keys_pkey PRIMARY KEY (id);

--
-- Name: asset_templates asset_templates_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.asset_templates
    ADD CONSTRAINT asset_templates_pkey PRIMARY KEY (id);

--
-- Name: audience_dna audience_dna_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.audience_dna
    ADD CONSTRAINT audience_dna_pkey PRIMARY KEY (id);

--
-- Name: audience_overlaps audience_overlaps_node_a_node_b_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.audience_overlaps
    ADD CONSTRAINT audience_overlaps_node_a_node_b_key UNIQUE (node_a, node_b);

--
-- Name: audience_overlaps audience_overlaps_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.audience_overlaps
    ADD CONSTRAINT audience_overlaps_pkey PRIMARY KEY (id);

--
-- Name: auto_funnel_runs auto_funnel_runs_funnel_id_user_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auto_funnel_runs
    ADD CONSTRAINT auto_funnel_runs_funnel_id_user_id_key UNIQUE (funnel_id, user_id);

--
-- Name: auto_funnel_runs auto_funnel_runs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auto_funnel_runs
    ADD CONSTRAINT auto_funnel_runs_pkey PRIMARY KEY (id);

--
-- Name: auto_funnel_steps auto_funnel_steps_funnel_id_step_num_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auto_funnel_steps
    ADD CONSTRAINT auto_funnel_steps_funnel_id_step_num_key UNIQUE (funnel_id, step_num);

--
-- Name: auto_funnel_steps auto_funnel_steps_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auto_funnel_steps
    ADD CONSTRAINT auto_funnel_steps_pkey PRIMARY KEY (id);

--
-- Name: auto_funnels auto_funnels_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auto_funnels
    ADD CONSTRAINT auto_funnels_pkey PRIMARY KEY (id);

--
-- Name: auto_replies auto_replies_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auto_replies
    ADD CONSTRAINT auto_replies_pkey PRIMARY KEY (id);

--
-- Name: auto_reply_log auto_reply_log_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auto_reply_log
    ADD CONSTRAINT auto_reply_log_pkey PRIMARY KEY (id);

--
-- Name: automation_rules automation_rules_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.automation_rules
    ADD CONSTRAINT automation_rules_pkey PRIMARY KEY (id);

--
-- Name: autoreg_device_profiles autoreg_device_profiles_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.autoreg_device_profiles
    ADD CONSTRAINT autoreg_device_profiles_pkey PRIMARY KEY (owner_id);

--
-- Name: behavioral_events behavioral_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.behavioral_events
    ADD CONSTRAINT behavioral_events_pkey PRIMARY KEY (id);

--
-- Name: blocked_users blocked_users_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.blocked_users
    ADD CONSTRAINT blocked_users_pkey PRIMARY KEY (user_id);

--
-- Name: bm_node_threads bm_node_threads_node_id_entity_type_entity_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bm_node_threads
    ADD CONSTRAINT bm_node_threads_node_id_entity_type_entity_id_key UNIQUE (node_id, entity_type, entity_id);

--
-- Name: bm_node_threads bm_node_threads_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bm_node_threads
    ADD CONSTRAINT bm_node_threads_pkey PRIMARY KEY (id);

--
-- Name: bm_telegram_nodes bm_telegram_nodes_owner_id_tg_chat_id_node_type_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bm_telegram_nodes
    ADD CONSTRAINT bm_telegram_nodes_owner_id_tg_chat_id_node_type_key UNIQUE (owner_id, tg_chat_id, node_type);

--
-- Name: bm_telegram_nodes bm_telegram_nodes_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bm_telegram_nodes
    ADD CONSTRAINT bm_telegram_nodes_pkey PRIMARY KEY (id);

--
-- Name: booster_sessions booster_sessions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.booster_sessions
    ADD CONSTRAINT booster_sessions_pkey PRIMARY KEY (id);

--
-- Name: bot_admin_sessions bot_admin_sessions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bot_admin_sessions
    ADD CONSTRAINT bot_admin_sessions_pkey PRIMARY KEY (bot_id);

--
-- Name: bot_admin_sessions bot_admin_sessions_token_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bot_admin_sessions
    ADD CONSTRAINT bot_admin_sessions_token_key UNIQUE (token);

--
-- Name: bot_deep_links bot_deep_links_bot_id_start_param_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bot_deep_links
    ADD CONSTRAINT bot_deep_links_bot_id_start_param_key UNIQUE (bot_id, start_param);

--
-- Name: bot_deep_links bot_deep_links_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bot_deep_links
    ADD CONSTRAINT bot_deep_links_pkey PRIMARY KEY (id);

--
-- Name: bot_flood_config bot_flood_config_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bot_flood_config
    ADD CONSTRAINT bot_flood_config_pkey PRIMARY KEY (bot_id);

--
-- Name: bot_flood_events bot_flood_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bot_flood_events
    ADD CONSTRAINT bot_flood_events_pkey PRIMARY KEY (id);

--
-- Name: bot_mesh_hops bot_mesh_hops_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bot_mesh_hops
    ADD CONSTRAINT bot_mesh_hops_pkey PRIMARY KEY (id);

--
-- Name: bot_mesh_tasks bot_mesh_tasks_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bot_mesh_tasks
    ADD CONSTRAINT bot_mesh_tasks_pkey PRIMARY KEY (task_id);

--
-- Name: bot_metrics bot_metrics_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bot_metrics
    ADD CONSTRAINT bot_metrics_pkey PRIMARY KEY (bot_id);

--
-- Name: bot_routing_weights bot_routing_weights_bot_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bot_routing_weights
    ADD CONSTRAINT bot_routing_weights_bot_id_key UNIQUE (bot_id);

--
-- Name: bot_routing_weights bot_routing_weights_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bot_routing_weights
    ADD CONSTRAINT bot_routing_weights_pkey PRIMARY KEY (id);

--
-- Name: bot_sales_delivery bot_sales_delivery_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bot_sales_delivery
    ADD CONSTRAINT bot_sales_delivery_pkey PRIMARY KEY (id);

--
-- Name: bot_sales_dialogs bot_sales_dialogs_bot_id_customer_chat_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bot_sales_dialogs
    ADD CONSTRAINT bot_sales_dialogs_bot_id_customer_chat_id_key UNIQUE (bot_id, customer_chat_id);

--
-- Name: bot_sales_dialogs bot_sales_dialogs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bot_sales_dialogs
    ADD CONSTRAINT bot_sales_dialogs_pkey PRIMARY KEY (id);

--
-- Name: bot_sales_examples bot_sales_examples_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bot_sales_examples
    ADD CONSTRAINT bot_sales_examples_pkey PRIMARY KEY (id);

--
-- Name: bot_sales_faq bot_sales_faq_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bot_sales_faq
    ADD CONSTRAINT bot_sales_faq_pkey PRIMARY KEY (id);

--
-- Name: bot_sales_orders bot_sales_orders_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bot_sales_orders
    ADD CONSTRAINT bot_sales_orders_pkey PRIMARY KEY (id);

--
-- Name: bot_sales_personas bot_sales_personas_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bot_sales_personas
    ADD CONSTRAINT bot_sales_personas_pkey PRIMARY KEY (id);

--
-- Name: bot_sales_products bot_sales_products_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bot_sales_products
    ADD CONSTRAINT bot_sales_products_pkey PRIMARY KEY (id);

--
-- Name: bot_sales_promos bot_sales_promos_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bot_sales_promos
    ADD CONSTRAINT bot_sales_promos_pkey PRIMARY KEY (id);

--
-- Name: bot_seo_suggestions bot_seo_suggestions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bot_seo_suggestions
    ADD CONSTRAINT bot_seo_suggestions_pkey PRIMARY KEY (owner_id, bot_id);

--
-- Name: bot_update_offsets bot_update_offsets_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bot_update_offsets
    ADD CONSTRAINT bot_update_offsets_pkey PRIMARY KEY (bot_id);

--
-- Name: bot_user_facts bot_user_facts_bot_id_user_id_fact_key_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bot_user_facts
    ADD CONSTRAINT bot_user_facts_bot_id_user_id_fact_key_key UNIQUE (bot_id, user_id, fact_key);

--
-- Name: bot_user_facts bot_user_facts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bot_user_facts
    ADD CONSTRAINT bot_user_facts_pkey PRIMARY KEY (id);

--
-- Name: bot_user_memory bot_user_memory_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bot_user_memory
    ADD CONSTRAINT bot_user_memory_pkey PRIMARY KEY (id);

--
-- Name: bot_users bot_users_bot_id_user_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bot_users
    ADD CONSTRAINT bot_users_bot_id_user_id_key UNIQUE (bot_id, user_id);

--
-- Name: bot_users bot_users_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bot_users
    ADD CONSTRAINT bot_users_pkey PRIMARY KEY (id);

--
-- Name: bot_warehouse bot_warehouse_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bot_warehouse
    ADD CONSTRAINT bot_warehouse_pkey PRIMARY KEY (id);

--
-- Name: botmother_memory botmother_memory_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.botmother_memory
    ADD CONSTRAINT botmother_memory_pkey PRIMARY KEY (id);

--
-- Name: broadcast_campaign_ops broadcast_campaign_ops_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.broadcast_campaign_ops
    ADD CONSTRAINT broadcast_campaign_ops_pkey PRIMARY KEY (campaign_id, op_id);

--
-- Name: broadcast_campaigns broadcast_campaigns_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.broadcast_campaigns
    ADD CONSTRAINT broadcast_campaigns_pkey PRIMARY KEY (id);

--
-- Name: broadcast_delivery_log broadcast_delivery_log_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.broadcast_delivery_log
    ADD CONSTRAINT broadcast_delivery_log_pkey PRIMARY KEY (broadcast_id, user_id);

--
-- Name: broadcasts broadcasts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.broadcasts
    ADD CONSTRAINT broadcasts_pkey PRIMARY KEY (id);

--
-- Name: bulk_edit_snapshots bulk_edit_snapshots_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bulk_edit_snapshots
    ADD CONSTRAINT bulk_edit_snapshots_pkey PRIMARY KEY (id);

--
-- Name: business_connections business_connections_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.business_connections
    ADD CONSTRAINT business_connections_pkey PRIMARY KEY (connection_id);

--
-- Name: cf_worker_pool cf_worker_pool_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cf_worker_pool
    ADD CONSTRAINT cf_worker_pool_pkey PRIMARY KEY (id);

--
-- Name: channel_identity_history channel_identity_history_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.channel_identity_history
    ADD CONSTRAINT channel_identity_history_pkey PRIMARY KEY (id);

--
-- Name: channel_member_history channel_member_history_owner_id_channel_id_captured_on_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.channel_member_history
    ADD CONSTRAINT channel_member_history_owner_id_channel_id_captured_on_key UNIQUE (owner_id, channel_id, captured_on);

--
-- Name: channel_member_history channel_member_history_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.channel_member_history
    ADD CONSTRAINT channel_member_history_pkey PRIMARY KEY (id);

--
-- Name: channel_members channel_members_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.channel_members
    ADD CONSTRAINT channel_members_pkey PRIMARY KEY (channel_id, user_id);

--
-- Name: channel_search_rankings channel_search_rankings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.channel_search_rankings
    ADD CONSTRAINT channel_search_rankings_pkey PRIMARY KEY (id);

--
-- Name: channel_tracked_keywords channel_tracked_keywords_channel_id_keyword_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.channel_tracked_keywords
    ADD CONSTRAINT channel_tracked_keywords_channel_id_keyword_key UNIQUE (channel_id, keyword);

--
-- Name: channel_tracked_keywords channel_tracked_keywords_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.channel_tracked_keywords
    ADD CONSTRAINT channel_tracked_keywords_pkey PRIMARY KEY (id);

--
-- Name: chat_invite_state chat_invite_state_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chat_invite_state
    ADD CONSTRAINT chat_invite_state_pkey PRIMARY KEY (owner_id, chat_key);

--
-- Name: chat_warmup_log chat_warmup_log_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chat_warmup_log
    ADD CONSTRAINT chat_warmup_log_pkey PRIMARY KEY (id);

--
-- Name: chat_warmup_sessions chat_warmup_sessions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chat_warmup_sessions
    ADD CONSTRAINT chat_warmup_sessions_pkey PRIMARY KEY (id);

--
-- Name: chatlist_folders chatlist_folders_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chatlist_folders
    ADD CONSTRAINT chatlist_folders_pkey PRIMARY KEY (id);

--
-- Name: clone_adapt_history clone_adapt_history_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.clone_adapt_history
    ADD CONSTRAINT clone_adapt_history_pkey PRIMARY KEY (id);

--
-- Name: clusters clusters_owner_id_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.clusters
    ADD CONSTRAINT clusters_owner_id_name_key UNIQUE (owner_id, name);

--
-- Name: clusters clusters_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.clusters
    ADD CONSTRAINT clusters_pkey PRIMARY KEY (id);

--
-- Name: commission_ledger commission_ledger_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.commission_ledger
    ADD CONSTRAINT commission_ledger_pkey PRIMARY KEY (id);

--
-- Name: community_channels community_channels_node_id_tg_topic_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.community_channels
    ADD CONSTRAINT community_channels_node_id_tg_topic_id_key UNIQUE (node_id, tg_topic_id);

--
-- Name: community_channels community_channels_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.community_channels
    ADD CONSTRAINT community_channels_pkey PRIMARY KEY (id);

--
-- Name: community_node_members community_node_members_node_id_account_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.community_node_members
    ADD CONSTRAINT community_node_members_node_id_account_id_key UNIQUE (node_id, account_id);

--
-- Name: community_node_members community_node_members_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.community_node_members
    ADD CONSTRAINT community_node_members_pkey PRIMARY KEY (id);

--
-- Name: community_nodes community_nodes_owner_id_tg_chat_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.community_nodes
    ADD CONSTRAINT community_nodes_owner_id_tg_chat_id_key UNIQUE (owner_id, tg_chat_id);

--
-- Name: community_nodes community_nodes_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.community_nodes
    ADD CONSTRAINT community_nodes_pkey PRIMARY KEY (id);

--
-- Name: competitors competitors_owner_id_username_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.competitors
    ADD CONSTRAINT competitors_owner_id_username_key UNIQUE (owner_id, username);

--
-- Name: competitors competitors_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.competitors
    ADD CONSTRAINT competitors_pkey PRIMARY KEY (id);

--
-- Name: compliance_audit compliance_audit_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.compliance_audit
    ADD CONSTRAINT compliance_audit_pkey PRIMARY KEY (id);

--
-- Name: contact_bulk_ops contact_bulk_ops_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.contact_bulk_ops
    ADD CONSTRAINT contact_bulk_ops_pkey PRIMARY KEY (id);

--
-- Name: contact_conflicts contact_conflicts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.contact_conflicts
    ADD CONSTRAINT contact_conflicts_pkey PRIMARY KEY (id);

--
-- Name: contact_crm_activity contact_crm_activity_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.contact_crm_activity
    ADD CONSTRAINT contact_crm_activity_pkey PRIMARY KEY (id);

--
-- Name: contact_crm contact_crm_owner_id_contact_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.contact_crm
    ADD CONSTRAINT contact_crm_owner_id_contact_id_key UNIQUE (owner_id, contact_id);

--
-- Name: contact_crm contact_crm_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.contact_crm
    ADD CONSTRAINT contact_crm_pkey PRIMARY KEY (id);

--
-- Name: contact_export_jobs contact_export_jobs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.contact_export_jobs
    ADD CONSTRAINT contact_export_jobs_pkey PRIMARY KEY (id);

--
-- Name: contact_group_members contact_group_members_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.contact_group_members
    ADD CONSTRAINT contact_group_members_pkey PRIMARY KEY (group_id, contact_id);

--
-- Name: contact_groups contact_groups_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.contact_groups
    ADD CONSTRAINT contact_groups_pkey PRIMARY KEY (id);

--
-- Name: contact_history contact_history_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.contact_history
    ADD CONSTRAINT contact_history_pkey PRIMARY KEY (id);

--
-- Name: contact_identity_graph contact_identity_graph_owner_id_contact_id_identity_type_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.contact_identity_graph
    ADD CONSTRAINT contact_identity_graph_owner_id_contact_id_identity_type_id_key UNIQUE (owner_id, contact_id, identity_type, identity_value);

--
-- Name: contact_identity_graph contact_identity_graph_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.contact_identity_graph
    ADD CONSTRAINT contact_identity_graph_pkey PRIMARY KEY (id);

--
-- Name: contact_opt_out contact_opt_out_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.contact_opt_out
    ADD CONSTRAINT contact_opt_out_pkey PRIMARY KEY (owner_id, target);

--
-- Name: contact_relationships contact_relationships_owner_id_contact_a_id_contact_b_id_re_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.contact_relationships
    ADD CONSTRAINT contact_relationships_owner_id_contact_a_id_contact_b_id_re_key UNIQUE (owner_id, contact_a_id, contact_b_id, relationship_type);

--
-- Name: contact_relationships contact_relationships_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.contact_relationships
    ADD CONSTRAINT contact_relationships_pkey PRIMARY KEY (id);

--
-- Name: contact_smart_tags contact_smart_tags_owner_id_contact_id_tag_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.contact_smart_tags
    ADD CONSTRAINT contact_smart_tags_owner_id_contact_id_tag_key UNIQUE (owner_id, contact_id, tag);

--
-- Name: contact_smart_tags contact_smart_tags_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.contact_smart_tags
    ADD CONSTRAINT contact_smart_tags_pkey PRIMARY KEY (id);

--
-- Name: contact_sources contact_sources_contact_id_account_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.contact_sources
    ADD CONSTRAINT contact_sources_contact_id_account_id_key UNIQUE (contact_id, account_id);

--
-- Name: contact_sources contact_sources_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.contact_sources
    ADD CONSTRAINT contact_sources_pkey PRIMARY KEY (id);

--
-- Name: contact_sync_log contact_sync_log_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.contact_sync_log
    ADD CONSTRAINT contact_sync_log_pkey PRIMARY KEY (id);

--
-- Name: contact_sync_state contact_sync_state_owner_id_account_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.contact_sync_state
    ADD CONSTRAINT contact_sync_state_owner_id_account_id_key UNIQUE (owner_id, account_id);

--
-- Name: contact_sync_state contact_sync_state_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.contact_sync_state
    ADD CONSTRAINT contact_sync_state_pkey PRIMARY KEY (id);

--
-- Name: contact_versions contact_versions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.contact_versions
    ADD CONSTRAINT contact_versions_pkey PRIMARY KEY (id);

--
-- Name: content_meshes content_meshes_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.content_meshes
    ADD CONSTRAINT content_meshes_pkey PRIMARY KEY (id);

--
-- Name: content_performance content_performance_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.content_performance
    ADD CONSTRAINT content_performance_pkey PRIMARY KEY (id);

--
-- Name: crm_activity crm_activity_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.crm_activity
    ADD CONSTRAINT crm_activity_pkey PRIMARY KEY (id);

--
-- Name: crm_contacts crm_contacts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.crm_contacts
    ADD CONSTRAINT crm_contacts_pkey PRIMARY KEY (id);

--
-- Name: crm_deals crm_deals_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.crm_deals
    ADD CONSTRAINT crm_deals_pkey PRIMARY KEY (id);

--
-- Name: crosspost_links crosspost_links_owner_id_source_channel_id_target_channel_i_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.crosspost_links
    ADD CONSTRAINT crosspost_links_owner_id_source_channel_id_target_channel_i_key UNIQUE (owner_id, source_channel_id, target_channel_id);

--
-- Name: crosspost_links crosspost_links_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.crosspost_links
    ADD CONSTRAINT crosspost_links_pkey PRIMARY KEY (id);

--
-- Name: daughter_groups daughter_groups_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.daughter_groups
    ADD CONSTRAINT daughter_groups_pkey PRIMARY KEY (id);

--
-- Name: deep_link_visits deep_link_visits_link_id_user_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.deep_link_visits
    ADD CONSTRAINT deep_link_visits_link_id_user_id_key UNIQUE (link_id, user_id);

--
-- Name: deep_link_visits deep_link_visits_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.deep_link_visits
    ADD CONSTRAINT deep_link_visits_pkey PRIMARY KEY (id);

--
-- Name: deploy_log deploy_log_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.deploy_log
    ADD CONSTRAINT deploy_log_pkey PRIMARY KEY (id);

--
-- Name: device_pairings device_pairings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.device_pairings
    ADD CONSTRAINT device_pairings_pkey PRIMARY KEY (id);

--
-- Name: discovered_bots discovered_bots_owner_id_username_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.discovered_bots
    ADD CONSTRAINT discovered_bots_owner_id_username_key UNIQUE (owner_id, username);

--
-- Name: discovered_bots discovered_bots_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.discovered_bots
    ADD CONSTRAINT discovered_bots_pkey PRIMARY KEY (id);

--
-- Name: dm_campaign_log dm_campaign_log_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dm_campaign_log
    ADD CONSTRAINT dm_campaign_log_pkey PRIMARY KEY (id);

--
-- Name: dm_campaigns dm_campaigns_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dm_campaigns
    ADD CONSTRAINT dm_campaigns_pkey PRIMARY KEY (id);

--
-- Name: ecosystem_bots ecosystem_bots_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ecosystem_bots
    ADD CONSTRAINT ecosystem_bots_pkey PRIMARY KEY (ecosystem_id, bot_id);

--
-- Name: ecosystem_channels ecosystem_channels_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ecosystem_channels
    ADD CONSTRAINT ecosystem_channels_pkey PRIMARY KEY (ecosystem_id, channel_id);

--
-- Name: ecosystem_dna ecosystem_dna_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ecosystem_dna
    ADD CONSTRAINT ecosystem_dna_pkey PRIMARY KEY (id);

--
-- Name: ecosystem_drift_log ecosystem_drift_log_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ecosystem_drift_log
    ADD CONSTRAINT ecosystem_drift_log_pkey PRIMARY KEY (id);

--
-- Name: ecosystem_events ecosystem_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ecosystem_events
    ADD CONSTRAINT ecosystem_events_pkey PRIMARY KEY (id);

--
-- Name: ecosystem_global_presence ecosystem_global_presence_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ecosystem_global_presence
    ADD CONSTRAINT ecosystem_global_presence_pkey PRIMARY KEY (ecosystem_id, plan_id);

--
-- Name: ecosystem_members ecosystem_members_ecosystem_id_object_type_object_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ecosystem_members
    ADD CONSTRAINT ecosystem_members_ecosystem_id_object_type_object_id_key UNIQUE (ecosystem_id, object_type, object_id);

--
-- Name: ecosystem_members ecosystem_members_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ecosystem_members
    ADD CONSTRAINT ecosystem_members_pkey PRIMARY KEY (id);

--
-- Name: ecosystems ecosystems_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ecosystems
    ADD CONSTRAINT ecosystems_pkey PRIMARY KEY (id);

--
-- Name: entity_behavioral_score entity_behavioral_score_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.entity_behavioral_score
    ADD CONSTRAINT entity_behavioral_score_pkey PRIMARY KEY (owner_id, entity_type, entity_id);

--
-- Name: entity_follow_events entity_follow_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.entity_follow_events
    ADD CONSTRAINT entity_follow_events_pkey PRIMARY KEY (id);

--
-- Name: entity_follows entity_follows_owner_id_entity_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.entity_follows
    ADD CONSTRAINT entity_follows_owner_id_entity_id_key UNIQUE (owner_id, entity_id);

--
-- Name: entity_follows entity_follows_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.entity_follows
    ADD CONSTRAINT entity_follows_pkey PRIMARY KEY (id);

--
-- Name: entity_last_known entity_last_known_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.entity_last_known
    ADD CONSTRAINT entity_last_known_pkey PRIMARY KEY (entity_id);

--
-- Name: entity_name_history entity_name_history_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.entity_name_history
    ADD CONSTRAINT entity_name_history_pkey PRIMARY KEY (id);

--
-- Name: entity_radar_stats entity_radar_stats_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.entity_radar_stats
    ADD CONSTRAINT entity_radar_stats_pkey PRIMARY KEY (entity_id);

--
-- Name: error_reports error_reports_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.error_reports
    ADD CONSTRAINT error_reports_pkey PRIMARY KEY (id);

--
-- Name: executor_lease executor_lease_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.executor_lease
    ADD CONSTRAINT executor_lease_pkey PRIMARY KEY (id);

--
-- Name: experiment_assignments experiment_assignments_bot_id_user_id_experiment_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.experiment_assignments
    ADD CONSTRAINT experiment_assignments_bot_id_user_id_experiment_id_key UNIQUE (bot_id, user_id, experiment_id);

--
-- Name: experiment_assignments experiment_assignments_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.experiment_assignments
    ADD CONSTRAINT experiment_assignments_pkey PRIMARY KEY (id);

--
-- Name: experiment_variants experiment_variants_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.experiment_variants
    ADD CONSTRAINT experiment_variants_pkey PRIMARY KEY (id);

--
-- Name: experiments experiments_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.experiments
    ADD CONSTRAINT experiments_pkey PRIMARY KEY (id);

--
-- Name: funnel_steps funnel_steps_funnel_id_step_order_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.funnel_steps
    ADD CONSTRAINT funnel_steps_funnel_id_step_order_key UNIQUE (funnel_id, step_order);

--
-- Name: funnel_steps funnel_steps_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.funnel_steps
    ADD CONSTRAINT funnel_steps_pkey PRIMARY KEY (id);

--
-- Name: funnel_subscriptions funnel_subscriptions_funnel_id_user_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.funnel_subscriptions
    ADD CONSTRAINT funnel_subscriptions_funnel_id_user_id_key UNIQUE (funnel_id, user_id);

--
-- Name: funnel_subscriptions funnel_subscriptions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.funnel_subscriptions
    ADD CONSTRAINT funnel_subscriptions_pkey PRIMARY KEY (id);

--
-- Name: funnels funnels_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.funnels
    ADD CONSTRAINT funnels_pkey PRIMARY KEY (id);

--
-- Name: ghost_action_log ghost_action_log_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ghost_action_log
    ADD CONSTRAINT ghost_action_log_pkey PRIMARY KEY (id);

--
-- Name: ghost_profiles ghost_profiles_owner_id_account_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ghost_profiles
    ADD CONSTRAINT ghost_profiles_owner_id_account_id_key UNIQUE (owner_id, account_id);

--
-- Name: ghost_profiles ghost_profiles_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ghost_profiles
    ADD CONSTRAINT ghost_profiles_pkey PRIMARY KEY (id);

--
-- Name: gift_inventory gift_inventory_account_id_gift_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.gift_inventory
    ADD CONSTRAINT gift_inventory_account_id_gift_id_key UNIQUE (account_id, gift_id);

--
-- Name: gift_inventory gift_inventory_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.gift_inventory
    ADD CONSTRAINT gift_inventory_pkey PRIMARY KEY (id);

--
-- Name: gift_recipients gift_recipients_owner_id_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.gift_recipients
    ADD CONSTRAINT gift_recipients_owner_id_name_key UNIQUE (owner_id, name);

--
-- Name: gift_recipients gift_recipients_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.gift_recipients
    ADD CONSTRAINT gift_recipients_pkey PRIMARY KEY (id);

--
-- Name: gift_transfer_items gift_transfer_items_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.gift_transfer_items
    ADD CONSTRAINT gift_transfer_items_pkey PRIMARY KEY (id);

--
-- Name: gift_transfer_items gift_transfer_items_plan_id_inventory_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.gift_transfer_items
    ADD CONSTRAINT gift_transfer_items_plan_id_inventory_id_key UNIQUE (plan_id, inventory_id);

--
-- Name: gift_transfer_ops gift_transfer_ops_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.gift_transfer_ops
    ADD CONSTRAINT gift_transfer_ops_pkey PRIMARY KEY (id);

--
-- Name: gift_transfer_plans gift_transfer_plans_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.gift_transfer_plans
    ADD CONSTRAINT gift_transfer_plans_pkey PRIMARY KEY (id);

--
-- Name: gift_transfer_reports gift_transfer_reports_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.gift_transfer_reports
    ADD CONSTRAINT gift_transfer_reports_pkey PRIMARY KEY (id);

--
-- Name: global_presence_plans global_presence_plans_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.global_presence_plans
    ADD CONSTRAINT global_presence_plans_pkey PRIMARY KEY (id);

--
-- Name: global_presence_targets global_presence_targets_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.global_presence_targets
    ADD CONSTRAINT global_presence_targets_pkey PRIMARY KEY (id);

--
-- Name: graph_edges graph_edges_from_node_to_node_edge_type_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.graph_edges
    ADD CONSTRAINT graph_edges_from_node_to_node_edge_type_key UNIQUE (from_node, to_node, edge_type);

--
-- Name: graph_edges graph_edges_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.graph_edges
    ADD CONSTRAINT graph_edges_pkey PRIMARY KEY (id);

--
-- Name: graph_nodes graph_nodes_entity_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.graph_nodes
    ADD CONSTRAINT graph_nodes_entity_id_key UNIQUE (entity_id);

--
-- Name: graph_nodes graph_nodes_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.graph_nodes
    ADD CONSTRAINT graph_nodes_pkey PRIMARY KEY (id);

--
-- Name: growth_actions growth_actions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.growth_actions
    ADD CONSTRAINT growth_actions_pkey PRIMARY KEY (id);

--
-- Name: growth_content_seeds growth_content_seeds_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.growth_content_seeds
    ADD CONSTRAINT growth_content_seeds_pkey PRIMARY KEY (id);

--
-- Name: growth_goals growth_goals_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.growth_goals
    ADD CONSTRAINT growth_goals_pkey PRIMARY KEY (id);

--
-- Name: growth_reports growth_reports_goal_id_report_date_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.growth_reports
    ADD CONSTRAINT growth_reports_goal_id_report_date_key UNIQUE (goal_id, report_date);

--
-- Name: growth_reports growth_reports_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.growth_reports
    ADD CONSTRAINT growth_reports_pkey PRIMARY KEY (id);

--
-- Name: growth_schedule growth_schedule_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.growth_schedule
    ADD CONSTRAINT growth_schedule_pkey PRIMARY KEY (id);

--
-- Name: growth_settings growth_settings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.growth_settings
    ADD CONSTRAINT growth_settings_pkey PRIMARY KEY (user_id);

--
-- Name: guard_captcha_pending guard_captcha_pending_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.guard_captcha_pending
    ADD CONSTRAINT guard_captcha_pending_pkey PRIMARY KEY (chat_id, user_id);

--
-- Name: guard_chats guard_chats_chat_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.guard_chats
    ADD CONSTRAINT guard_chats_chat_id_key UNIQUE (chat_id);

--
-- Name: guard_chats guard_chats_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.guard_chats
    ADD CONSTRAINT guard_chats_pkey PRIMARY KEY (id);

--
-- Name: guard_warnings guard_warnings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.guard_warnings
    ADD CONSTRAINT guard_warnings_pkey PRIMARY KEY (chat_id, user_id);

--
-- Name: host_offerings host_offerings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.host_offerings
    ADD CONSTRAINT host_offerings_pkey PRIMARY KEY (id);

--
-- Name: host_rentals host_rentals_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.host_rentals
    ADD CONSTRAINT host_rentals_pkey PRIMARY KEY (id);

--
-- Name: host_server_access host_server_access_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.host_server_access
    ADD CONSTRAINT host_server_access_pkey PRIMARY KEY (user_id);

--
-- Name: immunity_policy immunity_policy_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.immunity_policy
    ADD CONSTRAINT immunity_policy_pkey PRIMARY KEY (id);

--
-- Name: immunity_rules immunity_rules_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.immunity_rules
    ADD CONSTRAINT immunity_rules_pkey PRIMARY KEY (id);

--
-- Name: immunity_signatures immunity_signatures_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.immunity_signatures
    ADD CONSTRAINT immunity_signatures_pkey PRIMARY KEY (signature, owner_id);

--
-- Name: inactivity_alerts_sent inactivity_alerts_sent_bot_id_chat_id_rule_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.inactivity_alerts_sent
    ADD CONSTRAINT inactivity_alerts_sent_bot_id_chat_id_rule_id_key UNIQUE (bot_id, chat_id, rule_id);

--
-- Name: inactivity_alerts_sent inactivity_alerts_sent_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.inactivity_alerts_sent
    ADD CONSTRAINT inactivity_alerts_sent_pkey PRIMARY KEY (id);

--
-- Name: infra_memory_accounts infra_memory_accounts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.infra_memory_accounts
    ADD CONSTRAINT infra_memory_accounts_pkey PRIMARY KEY (account_id, action_type);

--
-- Name: infra_memory_proxies infra_memory_proxies_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.infra_memory_proxies
    ADD CONSTRAINT infra_memory_proxies_pkey PRIMARY KEY (proxy_url, action_type);

--
-- Name: infra_pressure_cache infra_pressure_cache_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.infra_pressure_cache
    ADD CONSTRAINT infra_pressure_cache_pkey PRIMARY KEY (owner_id);

--
-- Name: infrastructure_alerts infrastructure_alerts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.infrastructure_alerts
    ADD CONSTRAINT infrastructure_alerts_pkey PRIMARY KEY (id);

--
-- Name: intent_operation_links intent_operation_links_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.intent_operation_links
    ADD CONSTRAINT intent_operation_links_pkey PRIMARY KEY (intent_id, op_id);

--
-- Name: intents intents_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.intents
    ADD CONSTRAINT intents_pkey PRIMARY KEY (id);

--
-- Name: invite_overflow_channels invite_overflow_channels_owner_id_chain_key_channel_ref_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.invite_overflow_channels
    ADD CONSTRAINT invite_overflow_channels_owner_id_chain_key_channel_ref_key UNIQUE (owner_id, chain_key, channel_ref);

--
-- Name: invite_overflow_channels invite_overflow_channels_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.invite_overflow_channels
    ADD CONSTRAINT invite_overflow_channels_pkey PRIMARY KEY (id);

--
-- Name: invite_target_log invite_target_log_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.invite_target_log
    ADD CONSTRAINT invite_target_log_pkey PRIMARY KEY (owner_id, group_key, target);

--
-- Name: keyword_hits keyword_hits_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.keyword_hits
    ADD CONSTRAINT keyword_hits_pkey PRIMARY KEY (id);

--
-- Name: keyword_hits keyword_hits_watcher_id_message_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.keyword_hits
    ADD CONSTRAINT keyword_hits_watcher_id_message_id_key UNIQUE (watcher_id, message_id);

--
-- Name: keyword_stats keyword_stats_bot_id_keyword_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.keyword_stats
    ADD CONSTRAINT keyword_stats_bot_id_keyword_key UNIQUE (bot_id, keyword);

--
-- Name: keyword_stats keyword_stats_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.keyword_stats
    ADD CONSTRAINT keyword_stats_pkey PRIMARY KEY (id);

--
-- Name: keyword_watchers keyword_watchers_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.keyword_watchers
    ADD CONSTRAINT keyword_watchers_pkey PRIMARY KEY (id);

--
-- Name: linked_platform_users linked_platform_users_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.linked_platform_users
    ADD CONSTRAINT linked_platform_users_pkey PRIMARY KEY (owner_id_a, owner_id_b);

--
-- Name: llm_cooldowns llm_cooldowns_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.llm_cooldowns
    ADD CONSTRAINT llm_cooldowns_pkey PRIMARY KEY (key);

--
-- Name: llm_usage llm_usage_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.llm_usage
    ADD CONSTRAINT llm_usage_pkey PRIMARY KEY (day, provider);

--
-- Name: managed_bots managed_bots_bot_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.managed_bots
    ADD CONSTRAINT managed_bots_bot_id_key UNIQUE (bot_id);

--
-- Name: managed_bots managed_bots_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.managed_bots
    ADD CONSTRAINT managed_bots_pkey PRIMARY KEY (id);

--
-- Name: managed_bots managed_bots_token_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.managed_bots
    ADD CONSTRAINT managed_bots_token_key UNIQUE (token);

--
-- Name: managed_channels managed_channels_owner_id_channel_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.managed_channels
    ADD CONSTRAINT managed_channels_owner_id_channel_id_key UNIQUE (owner_id, channel_id);

--
-- Name: managed_channels managed_channels_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.managed_channels
    ADD CONSTRAINT managed_channels_pkey PRIMARY KEY (id);

--
-- Name: memory_settings memory_settings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.memory_settings
    ADD CONSTRAINT memory_settings_pkey PRIMARY KEY (bot_id);

--
-- Name: mesh_queue mesh_queue_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mesh_queue
    ADD CONSTRAINT mesh_queue_pkey PRIMARY KEY (id);

--
-- Name: mesh_targets mesh_targets_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mesh_targets
    ADD CONSTRAINT mesh_targets_pkey PRIMARY KEY (id);

--
-- Name: message_templates message_templates_owner_id_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.message_templates
    ADD CONSTRAINT message_templates_owner_id_name_key UNIQUE (owner_id, name);

--
-- Name: message_templates message_templates_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.message_templates
    ADD CONSTRAINT message_templates_pkey PRIMARY KEY (id);

--
-- Name: mp_audit_log mp_audit_log_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mp_audit_log
    ADD CONSTRAINT mp_audit_log_pkey PRIMARY KEY (id);

--
-- Name: mp_provider_api_keys mp_provider_api_keys_key_hash_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mp_provider_api_keys
    ADD CONSTRAINT mp_provider_api_keys_key_hash_key UNIQUE (key_hash);

--
-- Name: mp_provider_api_keys mp_provider_api_keys_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mp_provider_api_keys
    ADD CONSTRAINT mp_provider_api_keys_pkey PRIMARY KEY (id);

--
-- Name: mp_providers mp_providers_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mp_providers
    ADD CONSTRAINT mp_providers_pkey PRIMARY KEY (id);

--
-- Name: mp_providers mp_providers_slug_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mp_providers
    ADD CONSTRAINT mp_providers_slug_key UNIQUE (slug);

--
-- Name: mp_services mp_services_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mp_services
    ADD CONSTRAINT mp_services_pkey PRIMARY KEY (id);

--
-- Name: narrative_campaigns narrative_campaigns_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.narrative_campaigns
    ADD CONSTRAINT narrative_campaigns_pkey PRIMARY KEY (id);

--
-- Name: narrative_posts narrative_posts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.narrative_posts
    ADD CONSTRAINT narrative_posts_pkey PRIMARY KEY (id);

--
-- Name: network_edges network_edges_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.network_edges
    ADD CONSTRAINT network_edges_pkey PRIMARY KEY (id);

--
-- Name: network_instances network_instances_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.network_instances
    ADD CONSTRAINT network_instances_pkey PRIMARY KEY (id);

--
-- Name: network_nodes network_nodes_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.network_nodes
    ADD CONSTRAINT network_nodes_pkey PRIMARY KEY (id);

--
-- Name: network_templates network_templates_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.network_templates
    ADD CONSTRAINT network_templates_pkey PRIMARY KEY (id);

--
-- Name: niche_growth_targets niche_growth_targets_owner_id_group_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.niche_growth_targets
    ADD CONSTRAINT niche_growth_targets_owner_id_group_id_key UNIQUE (owner_id, group_id);

--
-- Name: niche_growth_targets niche_growth_targets_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.niche_growth_targets
    ADD CONSTRAINT niche_growth_targets_pkey PRIMARY KEY (id);

--
-- Name: notary_observations notary_observations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notary_observations
    ADD CONSTRAINT notary_observations_pkey PRIMARY KEY (id);

--
-- Name: notary_watches notary_watches_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notary_watches
    ADD CONSTRAINT notary_watches_pkey PRIMARY KEY (id);

--
-- Name: notification_dedup notification_dedup_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notification_dedup
    ADD CONSTRAINT notification_dedup_pkey PRIMARY KEY (user_id, dedup_key);

--
-- Name: notification_settings notification_settings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notification_settings
    ADD CONSTRAINT notification_settings_pkey PRIMARY KEY (user_id);

--
-- Name: observation_state observation_state_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.observation_state
    ADD CONSTRAINT observation_state_pkey PRIMARY KEY (keyword_id, entity_id, account_id);

--
-- Name: onboarding_progress onboarding_progress_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.onboarding_progress
    ADD CONSTRAINT onboarding_progress_pkey PRIMARY KEY (owner_id);

--
-- Name: op_circuit_breaker op_circuit_breaker_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.op_circuit_breaker
    ADD CONSTRAINT op_circuit_breaker_pkey PRIMARY KEY (owner_id);

--
-- Name: op_telemetry op_telemetry_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.op_telemetry
    ADD CONSTRAINT op_telemetry_pkey PRIMARY KEY (id);

--
-- Name: operation_audit operation_audit_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.operation_audit
    ADD CONSTRAINT operation_audit_pkey PRIMARY KEY (id);

--
-- Name: operation_log operation_log_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.operation_log
    ADD CONSTRAINT operation_log_pkey PRIMARY KEY (id);

--
-- Name: operation_queue operation_queue_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.operation_queue
    ADD CONSTRAINT operation_queue_pkey PRIMARY KEY (id);

--
-- Name: organism_events organism_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.organism_events
    ADD CONSTRAINT organism_events_pkey PRIMARY KEY (id);

--
-- Name: organism_state organism_state_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.organism_state
    ADD CONSTRAINT organism_state_pkey PRIMARY KEY (owner_id, key);

--
-- Name: parsed_audiences parsed_audiences_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.parsed_audiences
    ADD CONSTRAINT parsed_audiences_pkey PRIMARY KEY (id);

--
-- Name: parser_runs parser_runs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.parser_runs
    ADD CONSTRAINT parser_runs_pkey PRIMARY KEY (id);

--
-- Name: payments payments_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payments
    ADD CONSTRAINT payments_pkey PRIMARY KEY (id);

--
-- Name: payments payments_reference_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payments
    ADD CONSTRAINT payments_reference_key UNIQUE (reference);

--
-- Name: payout_requests payout_requests_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payout_requests
    ADD CONSTRAINT payout_requests_pkey PRIMARY KEY (id);

--
-- Name: persona_memory persona_memory_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.persona_memory
    ADD CONSTRAINT persona_memory_pkey PRIMARY KEY (id);

--
-- Name: persona_profiles persona_profiles_account_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.persona_profiles
    ADD CONSTRAINT persona_profiles_account_id_key UNIQUE (account_id);

--
-- Name: persona_profiles persona_profiles_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.persona_profiles
    ADD CONSTRAINT persona_profiles_pkey PRIMARY KEY (id);

--
-- Name: phone_owner_links phone_owner_links_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.phone_owner_links
    ADD CONSTRAINT phone_owner_links_pkey PRIMARY KEY (phone, owner_id);

--
-- Name: platform_admin_sessions platform_admin_sessions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.platform_admin_sessions
    ADD CONSTRAINT platform_admin_sessions_pkey PRIMARY KEY (user_id);

--
-- Name: platform_proxy_pool platform_proxy_pool_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.platform_proxy_pool
    ADD CONSTRAINT platform_proxy_pool_pkey PRIMARY KEY (id);

--
-- Name: platform_proxy_pool platform_proxy_pool_proxy_url_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.platform_proxy_pool
    ADD CONSTRAINT platform_proxy_pool_proxy_url_key UNIQUE (proxy_url);

--
-- Name: platform_referral_codes platform_referral_codes_code_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.platform_referral_codes
    ADD CONSTRAINT platform_referral_codes_code_key UNIQUE (code);

--
-- Name: platform_referral_codes platform_referral_codes_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.platform_referral_codes
    ADD CONSTRAINT platform_referral_codes_pkey PRIMARY KEY (user_id);

--
-- Name: platform_referrals platform_referrals_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.platform_referrals
    ADD CONSTRAINT platform_referrals_pkey PRIMARY KEY (id);

--
-- Name: platform_referrals platform_referrals_referrer_id_referred_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.platform_referrals
    ADD CONSTRAINT platform_referrals_referrer_id_referred_id_key UNIQUE (referrer_id, referred_id);

--
-- Name: platform_settings platform_settings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.platform_settings
    ADD CONSTRAINT platform_settings_pkey PRIMARY KEY (key);

--
-- Name: platform_users platform_users_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.platform_users
    ADD CONSTRAINT platform_users_pkey PRIMARY KEY (user_id);

--
-- Name: position_history position_history_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.position_history
    ADD CONSTRAINT position_history_pkey PRIMARY KEY (id);

--
-- Name: post_templates post_templates_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.post_templates
    ADD CONSTRAINT post_templates_pkey PRIMARY KEY (id);

--
-- Name: presence_packs presence_packs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.presence_packs
    ADD CONSTRAINT presence_packs_pkey PRIMARY KEY (id);

--
-- Name: process_heartbeats process_heartbeats_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.process_heartbeats
    ADD CONSTRAINT process_heartbeats_pkey PRIMARY KEY (worker_id);

--
-- Name: promo_logs promo_logs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.promo_logs
    ADD CONSTRAINT promo_logs_pkey PRIMARY KEY (id);

--
-- Name: promo_orders promo_orders_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.promo_orders
    ADD CONSTRAINT promo_orders_pkey PRIMARY KEY (id);

--
-- Name: proxy_health_log proxy_health_log_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.proxy_health_log
    ADD CONSTRAINT proxy_health_log_pkey PRIMARY KEY (id);

--
-- Name: proxy_performance proxy_performance_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.proxy_performance
    ADD CONSTRAINT proxy_performance_pkey PRIMARY KEY (id);

--
-- Name: proxy_quality_log proxy_quality_log_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.proxy_quality_log
    ADD CONSTRAINT proxy_quality_log_pkey PRIMARY KEY (id);

--
-- Name: ranking_alerts ranking_alerts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ranking_alerts
    ADD CONSTRAINT ranking_alerts_pkey PRIMARY KEY (id);

--
-- Name: recovery_events recovery_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.recovery_events
    ADD CONSTRAINT recovery_events_pkey PRIMARY KEY (id);

--
-- Name: referral_conversions referral_conversions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.referral_conversions
    ADD CONSTRAINT referral_conversions_pkey PRIMARY KEY (id);

--
-- Name: referral_rewards referral_rewards_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.referral_rewards
    ADD CONSTRAINT referral_rewards_pkey PRIMARY KEY (id);

--
-- Name: referral_rewards referral_rewards_user_id_level_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.referral_rewards
    ADD CONSTRAINT referral_rewards_user_id_level_key UNIQUE (user_id, level);

--
-- Name: referrals referrals_bot_id_referred_user_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.referrals
    ADD CONSTRAINT referrals_bot_id_referred_user_id_key UNIQUE (bot_id, referred_user_id);

--
-- Name: referrals referrals_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.referrals
    ADD CONSTRAINT referrals_pkey PRIMARY KEY (id);

--
-- Name: reg_check_cache reg_check_cache_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.reg_check_cache
    ADD CONSTRAINT reg_check_cache_pkey PRIMARY KEY (entity_id, entity_type);

--
-- Name: relay_messages relay_messages_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.relay_messages
    ADD CONSTRAINT relay_messages_pkey PRIMARY KEY (id);

--
-- Name: relay_sessions relay_sessions_bot_id_user_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.relay_sessions
    ADD CONSTRAINT relay_sessions_bot_id_user_id_key UNIQUE (bot_id, user_id);

--
-- Name: relay_sessions relay_sessions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.relay_sessions
    ADD CONSTRAINT relay_sessions_pkey PRIMARY KEY (id);

--
-- Name: removed_channels removed_channels_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.removed_channels
    ADD CONSTRAINT removed_channels_pkey PRIMARY KEY (owner_id, channel_id);

--
-- Name: resource_activity_log resource_activity_log_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_activity_log
    ADD CONSTRAINT resource_activity_log_pkey PRIMARY KEY (id);

--
-- Name: resource_activity_sessions resource_activity_sessions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_activity_sessions
    ADD CONSTRAINT resource_activity_sessions_pkey PRIMARY KEY (id);

--
-- Name: restriction_alert_cooldown restriction_alert_cooldown_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.restriction_alert_cooldown
    ADD CONSTRAINT restriction_alert_cooldown_pkey PRIMARY KEY (owner_id, event_type, entity_id);

--
-- Name: restriction_events restriction_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.restriction_events
    ADD CONSTRAINT restriction_events_pkey PRIMARY KEY (id);

--
-- Name: routing_log routing_log_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.routing_log
    ADD CONSTRAINT routing_log_pkey PRIMARY KEY (id);

--
-- Name: routing_rules routing_rules_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.routing_rules
    ADD CONSTRAINT routing_rules_pkey PRIMARY KEY (id);

--
-- Name: saved_segments saved_segments_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.saved_segments
    ADD CONSTRAINT saved_segments_pkey PRIMARY KEY (id);

--
-- Name: scheduled_broadcasts scheduled_broadcasts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.scheduled_broadcasts
    ADD CONSTRAINT scheduled_broadcasts_pkey PRIMARY KEY (id);

--
-- Name: search_alert_cooldown search_alert_cooldown_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.search_alert_cooldown
    ADD CONSTRAINT search_alert_cooldown_pkey PRIMARY KEY (keyword_id, entity_id, event_type);

--
-- Name: search_change_events search_change_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.search_change_events
    ADD CONSTRAINT search_change_events_pkey PRIMARY KEY (id);

--
-- Name: search_keywords search_keywords_bot_id_keyword_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.search_keywords
    ADD CONSTRAINT search_keywords_bot_id_keyword_key UNIQUE (bot_id, keyword);

--
-- Name: search_keywords search_keywords_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.search_keywords
    ADD CONSTRAINT search_keywords_pkey PRIMARY KEY (id);

--
-- Name: search_memory search_memory_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.search_memory
    ADD CONSTRAINT search_memory_pkey PRIMARY KEY (owner_id, keyword);

--
-- Name: search_observations search_observations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.search_observations
    ADD CONSTRAINT search_observations_pkey PRIMARY KEY (id);

--
-- Name: search_rankings search_rankings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.search_rankings
    ADD CONSTRAINT search_rankings_pkey PRIMARY KEY (id);

--
-- Name: search_snapshots search_snapshots_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.search_snapshots
    ADD CONSTRAINT search_snapshots_pkey PRIMARY KEY (snapshot_id);

--
-- Name: security_violations security_violations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.security_violations
    ADD CONSTRAINT security_violations_pkey PRIMARY KEY (id);

--
-- Name: seen_entities seen_entities_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.seen_entities
    ADD CONSTRAINT seen_entities_pkey PRIMARY KEY (entity_id, chat_id);

--
-- Name: self_promo_runs self_promo_runs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.self_promo_runs
    ADD CONSTRAINT self_promo_runs_pkey PRIMARY KEY (id);

--
-- Name: self_promo_templates self_promo_templates_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.self_promo_templates
    ADD CONSTRAINT self_promo_templates_pkey PRIMARY KEY (id);

--
-- Name: seo_ai_suggestions seo_ai_suggestions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.seo_ai_suggestions
    ADD CONSTRAINT seo_ai_suggestions_pkey PRIMARY KEY (owner_id, chan_id);

--
-- Name: seo_score_history seo_score_history_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.seo_score_history
    ADD CONSTRAINT seo_score_history_pkey PRIMARY KEY (id);

--
-- Name: shield_actions shield_actions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.shield_actions
    ADD CONSTRAINT shield_actions_pkey PRIMARY KEY (id);

--
-- Name: shield_configs shield_configs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.shield_configs
    ADD CONSTRAINT shield_configs_pkey PRIMARY KEY (owner_id);

--
-- Name: short_links short_links_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.short_links
    ADD CONSTRAINT short_links_pkey PRIMARY KEY (code);

--
-- Name: showcase_layers showcase_layers_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.showcase_layers
    ADD CONSTRAINT showcase_layers_pkey PRIMARY KEY (id);

--
-- Name: smart_tag_rules smart_tag_rules_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.smart_tag_rules
    ADD CONSTRAINT smart_tag_rules_pkey PRIMARY KEY (id);

--
-- Name: smm_panels smm_panels_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.smm_panels
    ADD CONSTRAINT smm_panels_pkey PRIMARY KEY (id);

--
-- Name: stars_events stars_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.stars_events
    ADD CONSTRAINT stars_events_pkey PRIMARY KEY (id);

--
-- Name: stars_experiments stars_experiments_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.stars_experiments
    ADD CONSTRAINT stars_experiments_pkey PRIMARY KEY (id);

--
-- Name: stars_transactions stars_transactions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.stars_transactions
    ADD CONSTRAINT stars_transactions_pkey PRIMARY KEY (id);

--
-- Name: strike_access strike_access_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.strike_access
    ADD CONSTRAINT strike_access_pkey PRIMARY KEY (user_id);

--
-- Name: strike_appeals strike_appeals_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.strike_appeals
    ADD CONSTRAINT strike_appeals_pkey PRIMARY KEY (id);

--
-- Name: strike_email_accounts strike_email_accounts_owner_id_email_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.strike_email_accounts
    ADD CONSTRAINT strike_email_accounts_owner_id_email_key UNIQUE (owner_id, email);

--
-- Name: strike_email_accounts strike_email_accounts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.strike_email_accounts
    ADD CONSTRAINT strike_email_accounts_pkey PRIMARY KEY (id);

--
-- Name: strike_history strike_history_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.strike_history
    ADD CONSTRAINT strike_history_pkey PRIMARY KEY (id);

--
-- Name: strike_reports strike_reports_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.strike_reports
    ADD CONSTRAINT strike_reports_pkey PRIMARY KEY (id);

--
-- Name: subscription_gate_channels subscription_gate_channels_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.subscription_gate_channels
    ADD CONSTRAINT subscription_gate_channels_pkey PRIMARY KEY (id);

--
-- Name: subscription_trials subscription_trials_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.subscription_trials
    ADD CONSTRAINT subscription_trials_pkey PRIMARY KEY (owner_id);

--
-- Name: subscriptions subscriptions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.subscriptions
    ADD CONSTRAINT subscriptions_pkey PRIMARY KEY (id);

--
-- Name: subscriptions subscriptions_user_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.subscriptions
    ADD CONSTRAINT subscriptions_user_id_key UNIQUE (user_id);

--
-- Name: system_health_snapshots system_health_snapshots_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_health_snapshots
    ADD CONSTRAINT system_health_snapshots_pkey PRIMARY KEY (id);

--
-- Name: system_mode system_mode_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_mode
    ADD CONSTRAINT system_mode_pkey PRIMARY KEY (id);

--
-- Name: tg_accounts tg_accounts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tg_accounts
    ADD CONSTRAINT tg_accounts_pkey PRIMARY KEY (id);

--
-- Name: tg_channels tg_channels_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tg_channels
    ADD CONSTRAINT tg_channels_pkey PRIMARY KEY (id);

--
-- Name: tg_cloud_blobs tg_cloud_blobs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tg_cloud_blobs
    ADD CONSTRAINT tg_cloud_blobs_pkey PRIMARY KEY (locator);

--
-- Name: tg_cloud_chunk_locs tg_cloud_chunk_locs_chunk_id_locator_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tg_cloud_chunk_locs
    ADD CONSTRAINT tg_cloud_chunk_locs_chunk_id_locator_key UNIQUE (chunk_id, locator);

--
-- Name: tg_cloud_chunk_locs tg_cloud_chunk_locs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tg_cloud_chunk_locs
    ADD CONSTRAINT tg_cloud_chunk_locs_pkey PRIMARY KEY (id);

--
-- Name: tg_cloud_chunks tg_cloud_chunks_file_id_ord_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tg_cloud_chunks
    ADD CONSTRAINT tg_cloud_chunks_file_id_ord_key UNIQUE (file_id, ord);

--
-- Name: tg_cloud_chunks tg_cloud_chunks_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tg_cloud_chunks
    ADD CONSTRAINT tg_cloud_chunks_pkey PRIMARY KEY (id);

--
-- Name: tg_cloud_files tg_cloud_files_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tg_cloud_files
    ADD CONSTRAINT tg_cloud_files_pkey PRIMARY KEY (id);

--
-- Name: tg_cloud_quota tg_cloud_quota_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tg_cloud_quota
    ADD CONSTRAINT tg_cloud_quota_pkey PRIMARY KEY (owner_id);

--
-- Name: tg_cloud_storekeepers tg_cloud_storekeepers_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tg_cloud_storekeepers
    ADD CONSTRAINT tg_cloud_storekeepers_pkey PRIMARY KEY (acc_id);

--
-- Name: tracked_keywords tracked_keywords_bot_id_keyword_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tracked_keywords
    ADD CONSTRAINT tracked_keywords_bot_id_keyword_key UNIQUE (bot_id, keyword);

--
-- Name: tracked_keywords tracked_keywords_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tracked_keywords
    ADD CONSTRAINT tracked_keywords_pkey PRIMARY KEY (id);

--
-- Name: unified_contacts unified_contacts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.unified_contacts
    ADD CONSTRAINT unified_contacts_pkey PRIMARY KEY (id);

--
-- Name: user_activity user_activity_bot_id_user_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_activity
    ADD CONSTRAINT user_activity_bot_id_user_id_key UNIQUE (bot_id, user_id);

--
-- Name: user_activity user_activity_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_activity
    ADD CONSTRAINT user_activity_pkey PRIMARY KEY (id);

--
-- Name: user_payment_methods user_payment_methods_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_payment_methods
    ADD CONSTRAINT user_payment_methods_pkey PRIMARY KEY (id);

--
-- Name: user_proxies user_proxies_owner_id_proxy_url_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_proxies
    ADD CONSTRAINT user_proxies_owner_id_proxy_url_key UNIQUE (owner_id, proxy_url);

--
-- Name: user_proxies user_proxies_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_proxies
    ADD CONSTRAINT user_proxies_pkey PRIMARY KEY (id);

--
-- Name: user_tags user_tags_bot_id_user_id_tag_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_tags
    ADD CONSTRAINT user_tags_bot_id_user_id_tag_key UNIQUE (bot_id, user_id, tag);

--
-- Name: user_tags user_tags_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_tags
    ADD CONSTRAINT user_tags_pkey PRIMARY KEY (id);

--
-- Name: va_admin_drafts va_admin_drafts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.va_admin_drafts
    ADD CONSTRAINT va_admin_drafts_pkey PRIMARY KEY (id);

--
-- Name: va_admin_events va_admin_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.va_admin_events
    ADD CONSTRAINT va_admin_events_pkey PRIMARY KEY (id);

--
-- Name: va_admin_plan va_admin_plan_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.va_admin_plan
    ADD CONSTRAINT va_admin_plan_pkey PRIMARY KEY (id);

--
-- Name: va_channel_admin va_channel_admin_owner_id_channel_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.va_channel_admin
    ADD CONSTRAINT va_channel_admin_owner_id_channel_id_key UNIQUE (owner_id, channel_id);

--
-- Name: va_channel_admin va_channel_admin_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.va_channel_admin
    ADD CONSTRAINT va_channel_admin_pkey PRIMARY KEY (id);

--
-- Name: va_channel_brain va_channel_brain_owner_id_channel_key_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.va_channel_brain
    ADD CONSTRAINT va_channel_brain_owner_id_channel_key_key UNIQUE (owner_id, channel_key);

--
-- Name: va_channel_brain va_channel_brain_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.va_channel_brain
    ADD CONSTRAINT va_channel_brain_pkey PRIMARY KEY (id);

--
-- Name: va_channel_posts va_channel_posts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.va_channel_posts
    ADD CONSTRAINT va_channel_posts_pkey PRIMARY KEY (id);

--
-- Name: va_channel_stats va_channel_stats_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.va_channel_stats
    ADD CONSTRAINT va_channel_stats_pkey PRIMARY KEY (owner_id, channel_id, day);

--
-- Name: va_learning_decisions va_learning_decisions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.va_learning_decisions
    ADD CONSTRAINT va_learning_decisions_pkey PRIMARY KEY (id);

--
-- Name: va_media va_media_owner_id_channel_id_file_unique_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.va_media
    ADD CONSTRAINT va_media_owner_id_channel_id_file_unique_id_key UNIQUE (owner_id, channel_id, file_unique_id);

--
-- Name: va_media va_media_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.va_media
    ADD CONSTRAINT va_media_pkey PRIMARY KEY (id);

--
-- Name: va_network_strategy va_network_strategy_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.va_network_strategy
    ADD CONSTRAINT va_network_strategy_pkey PRIMARY KEY (owner_id);

--
-- Name: va_news_inbox va_news_inbox_owner_id_channel_id_signal_hash_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.va_news_inbox
    ADD CONSTRAINT va_news_inbox_owner_id_channel_id_signal_hash_key UNIQUE (owner_id, channel_id, signal_hash);

--
-- Name: va_news_inbox va_news_inbox_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.va_news_inbox
    ADD CONSTRAINT va_news_inbox_pkey PRIMARY KEY (id);

--
-- Name: va_reference_channels va_reference_channels_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.va_reference_channels
    ADD CONSTRAINT va_reference_channels_pkey PRIMARY KEY (id);

--
-- Name: va_tracked_links va_tracked_links_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.va_tracked_links
    ADD CONSTRAINT va_tracked_links_pkey PRIMARY KEY (id);

--
-- Name: vault_intent_hits vault_intent_hits_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vault_intent_hits
    ADD CONSTRAINT vault_intent_hits_pkey PRIMARY KEY (id);

--
-- Name: vault_intent_rules vault_intent_rules_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vault_intent_rules
    ADD CONSTRAINT vault_intent_rules_pkey PRIMARY KEY (id);

--
-- Name: vault_key_check vault_key_check_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vault_key_check
    ADD CONSTRAINT vault_key_check_pkey PRIMARY KEY (id);

--
-- Name: vault_messages vault_messages_owner_id_chat_id_msg_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vault_messages
    ADD CONSTRAINT vault_messages_owner_id_chat_id_msg_id_key UNIQUE (owner_id, chat_id, msg_id);

--
-- Name: vault_messages vault_messages_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vault_messages
    ADD CONSTRAINT vault_messages_pkey PRIMARY KEY (id);

--
-- Name: virtual_state_history virtual_state_history_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.virtual_state_history
    ADD CONSTRAINT virtual_state_history_pkey PRIMARY KEY (id);

--
-- Name: virtual_states virtual_states_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.virtual_states
    ADD CONSTRAINT virtual_states_pkey PRIMARY KEY (owner_id, entity_type, entity_id, state_key);

--
-- Name: visibility_alert_settings visibility_alert_settings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.visibility_alert_settings
    ADD CONSTRAINT visibility_alert_settings_pkey PRIMARY KEY (owner_id);

--
-- Name: warmup_session_log warmup_session_log_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.warmup_session_log
    ADD CONSTRAINT warmup_session_log_pkey PRIMARY KEY (id);

--
-- Name: warmup_sessions warmup_sessions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.warmup_sessions
    ADD CONSTRAINT warmup_sessions_pkey PRIMARY KEY (id);

--
-- Name: wb_account_budget wb_account_budget_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.wb_account_budget
    ADD CONSTRAINT wb_account_budget_pkey PRIMARY KEY (account_id, action_date, action_type);

--
-- Name: wb_accounts wb_accounts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.wb_accounts
    ADD CONSTRAINT wb_accounts_pkey PRIMARY KEY (id);

--
-- Name: wb_operation_targets wb_operation_targets_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.wb_operation_targets
    ADD CONSTRAINT wb_operation_targets_pkey PRIMARY KEY (id);

--
-- Name: wb_operations wb_operations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.wb_operations
    ADD CONSTRAINT wb_operations_pkey PRIMARY KEY (id);

--
-- Name: workflow_definitions workflow_definitions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workflow_definitions
    ADD CONSTRAINT workflow_definitions_pkey PRIMARY KEY (id);

--
-- Name: workflow_runs workflow_runs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workflow_runs
    ADD CONSTRAINT workflow_runs_pkey PRIMARY KEY (id);

--
-- Name: workspace_invites workspace_invites_invite_code_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workspace_invites
    ADD CONSTRAINT workspace_invites_invite_code_key UNIQUE (invite_code);

--
-- Name: workspace_invites workspace_invites_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workspace_invites
    ADD CONSTRAINT workspace_invites_pkey PRIMARY KEY (id);

--
-- Name: workspace_members workspace_members_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workspace_members
    ADD CONSTRAINT workspace_members_pkey PRIMARY KEY (id);

--
-- Name: workspace_members workspace_members_workspace_id_user_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workspace_members
    ADD CONSTRAINT workspace_members_workspace_id_user_id_key UNIQUE (workspace_id, user_id);

--
-- Name: workspaces workspaces_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workspaces
    ADD CONSTRAINT workspaces_pkey PRIMARY KEY (id);

--
-- Name: api_keys_hash_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX api_keys_hash_idx ON public.api_keys USING btree (key_hash) WHERE (is_active = true);

--
-- Name: api_keys_user_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX api_keys_user_idx ON public.api_keys USING btree (user_id) WHERE (is_active = true);

--
-- Name: audience_overlaps_a_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX audience_overlaps_a_idx ON public.audience_overlaps USING btree (node_a);

--
-- Name: audience_overlaps_b_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX audience_overlaps_b_idx ON public.audience_overlaps USING btree (node_b);

--
-- Name: auto_funnel_runs_funnel_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX auto_funnel_runs_funnel_idx ON public.auto_funnel_runs USING btree (funnel_id);

--
-- Name: auto_funnel_runs_pending_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX auto_funnel_runs_pending_idx ON public.auto_funnel_runs USING btree (next_send_at) WHERE (status = 'active'::text);

--
-- Name: auto_funnel_steps_funnel_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX auto_funnel_steps_funnel_idx ON public.auto_funnel_steps USING btree (funnel_id);

--
-- Name: auto_funnels_owner_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX auto_funnels_owner_idx ON public.auto_funnels USING btree (owner_id);

--
-- Name: booster_sessions_owner_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX booster_sessions_owner_idx ON public.booster_sessions USING btree (owner_id);

--
-- Name: bot_warehouse_owner_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX bot_warehouse_owner_idx ON public.bot_warehouse USING btree (owner_id);

--
-- Name: bot_warehouse_status_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX bot_warehouse_status_idx ON public.bot_warehouse USING btree (status);

--
-- Name: bsd_owner_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX bsd_owner_idx ON public.bot_sales_dialogs USING btree (owner_id);

--
-- Name: bso_bot_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX bso_bot_idx ON public.bot_sales_orders USING btree (bot_id);

--
-- Name: bso_owner_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX bso_owner_idx ON public.bot_sales_orders USING btree (owner_id);

--
-- Name: bso_status_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX bso_status_idx ON public.bot_sales_orders USING btree (status);

--
-- Name: bsp_bot_active_uniq; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX bsp_bot_active_uniq ON public.bot_sales_personas USING btree (bot_id) WHERE ((bot_id IS NOT NULL) AND is_active);

--
-- Name: bsp_owner_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX bsp_owner_idx ON public.bot_sales_personas USING btree (owner_id);

--
-- Name: bsp_prod_persona_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX bsp_prod_persona_idx ON public.bot_sales_products USING btree (persona_id);

--
-- Name: clone_adapt_history_created_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX clone_adapt_history_created_idx ON public.clone_adapt_history USING btree (created_at DESC);

--
-- Name: clone_adapt_history_owner_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX clone_adapt_history_owner_idx ON public.clone_adapt_history USING btree (owner_id);

--
-- Name: commission_ledger_referrer_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX commission_ledger_referrer_idx ON public.commission_ledger USING btree (referrer_id, status);

--
-- Name: compliance_audit_created_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX compliance_audit_created_idx ON public.compliance_audit USING btree (created_at DESC);

--
-- Name: compliance_audit_op_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX compliance_audit_op_idx ON public.compliance_audit USING btree (op_id) WHERE (op_id IS NOT NULL);

--
-- Name: compliance_audit_user_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX compliance_audit_user_idx ON public.compliance_audit USING btree (user_id, created_at DESC);

--
-- Name: content_meshes_enabled_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX content_meshes_enabled_idx ON public.content_meshes USING btree (enabled) WHERE (enabled = true);

--
-- Name: content_meshes_owner_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX content_meshes_owner_idx ON public.content_meshes USING btree (owner_id);

--
-- Name: ghost_action_log_executed_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ghost_action_log_executed_idx ON public.ghost_action_log USING btree (executed_at DESC);

--
-- Name: ghost_action_log_profile_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ghost_action_log_profile_idx ON public.ghost_action_log USING btree (ghost_profile_id);

--
-- Name: ghost_profiles_account_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ghost_profiles_account_idx ON public.ghost_profiles USING btree (account_id);

--
-- Name: ghost_profiles_enabled_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ghost_profiles_enabled_idx ON public.ghost_profiles USING btree (enabled) WHERE (enabled = true);

--
-- Name: ghost_profiles_owner_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ghost_profiles_owner_idx ON public.ghost_profiles USING btree (owner_id);

--
-- Name: graph_edges_from_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX graph_edges_from_idx ON public.graph_edges USING btree (from_node);

--
-- Name: graph_edges_to_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX graph_edges_to_idx ON public.graph_edges USING btree (to_node);

--
-- Name: graph_nodes_entity_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX graph_nodes_entity_idx ON public.graph_nodes USING btree (entity_id);

--
-- Name: graph_nodes_type_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX graph_nodes_type_idx ON public.graph_nodes USING btree (entity_type);

--
-- Name: growth_schedule_active_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX growth_schedule_active_idx ON public.growth_schedule USING btree (is_active, next_run_at) WHERE (is_active = true);

--
-- Name: idx_account_rehab_due; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_account_rehab_due ON public.account_rehab_state USING btree (next_action_at) WHERE (phase = ANY (ARRAY['appeal'::text, 'warming'::text, 'recheck'::text]));

--
-- Name: idx_activity_log_errors; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_activity_log_errors ON public.activity_log USING btree (status, occurred_at DESC) WHERE (status = 'error'::text);

--
-- Name: idx_activity_log_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_activity_log_owner ON public.activity_log USING btree (owner_id, occurred_at DESC);

--
-- Name: idx_activity_log_recent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_activity_log_recent ON public.activity_log USING btree (occurred_at DESC);

--
-- Name: idx_ad_advertisers_owner_seen; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_ad_advertisers_owner_seen ON public.ad_advertisers USING btree (owner_id, last_seen_at DESC);

--
-- Name: idx_ad_placements_niche; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_ad_placements_niche ON public.ad_placements USING gin (niches);

--
-- Name: idx_ad_placements_owner_score; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_ad_placements_owner_score ON public.ad_placements USING btree (owner_id, quality_score DESC);

--
-- Name: idx_ad_posts_placement; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_ad_posts_placement ON public.ad_posts_log USING btree (placement_id, detected_at DESC);

--
-- Name: idx_admin_audit_action; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_admin_audit_action ON public.admin_audit_log USING btree (action);

--
-- Name: idx_admin_audit_admin_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_admin_audit_admin_id ON public.admin_audit_log USING btree (admin_id);

--
-- Name: idx_admin_audit_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_admin_audit_created ON public.admin_audit_log USING btree (created_at DESC);

--
-- Name: idx_admin_audit_target; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_admin_audit_target ON public.admin_audit_log USING btree (target_user_id);

--
-- Name: idx_anomaly_events_detected; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_anomaly_events_detected ON public.anomaly_events USING btree (detected_at);

--
-- Name: idx_anomaly_events_owner_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_anomaly_events_owner_active ON public.anomaly_events USING btree (owner_id, is_active, detected_at DESC);

--
-- Name: idx_anomaly_events_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_anomaly_events_type ON public.anomaly_events USING btree (owner_id, anomaly_type, detected_at DESC);

--
-- Name: idx_ase_acc; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_ase_acc ON public.account_status_events USING btree (acc_id, created_at DESC);

--
-- Name: idx_ase_death_sig; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_ase_death_sig ON public.account_status_events USING btree (signature, created_at DESC) WHERE is_death;

--
-- Name: idx_ase_owner_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_ase_owner_created ON public.account_status_events USING btree (owner_id, created_at DESC);

--
-- Name: idx_ase_unprocessed; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_ase_unprocessed ON public.account_status_events USING btree (created_at) WHERE (processed_at IS NULL);

--
-- Name: idx_asset_templates_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_asset_templates_owner ON public.asset_templates USING btree (owner_id, asset_type, created_at DESC);

--
-- Name: idx_audience_dna_bot; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_audience_dna_bot ON public.audience_dna USING btree (bot_id, computed_at DESC);

--
-- Name: idx_auto_replies_bot_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_auto_replies_bot_active ON public.auto_replies USING btree (bot_id) WHERE (is_active = true);

--
-- Name: idx_auto_reply_log_bot_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_auto_reply_log_bot_id ON public.auto_reply_log USING btree (bot_id, fired_at DESC);

--
-- Name: idx_auto_reply_log_chat_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_auto_reply_log_chat_id ON public.auto_reply_log USING btree (bot_id, chat_id, fired_at DESC);

--
-- Name: idx_automation_rules_bot_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_automation_rules_bot_active ON public.automation_rules USING btree (bot_id) WHERE (is_active = true);

--
-- Name: idx_bas_token; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_bas_token ON public.bot_admin_sessions USING btree (token);

--
-- Name: idx_bdl_broadcast_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_bdl_broadcast_id ON public.broadcast_delivery_log USING btree (broadcast_id);

--
-- Name: idx_be_owner_entity; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_be_owner_entity ON public.behavioral_events USING btree (owner_id, entity_type, entity_id);

--
-- Name: idx_be_owner_occurred; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_be_owner_occurred ON public.behavioral_events USING btree (owner_id, occurred_at DESC);

--
-- Name: idx_be_owner_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_be_owner_type ON public.behavioral_events USING btree (owner_id, event_type, occurred_at DESC);

--
-- Name: idx_behav_events_occurred; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_behav_events_occurred ON public.behavioral_events USING btree (occurred_at);

--
-- Name: idx_bes_owner_entity; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_bes_owner_entity ON public.bulk_edit_snapshots USING btree (owner_id, entity_type, created_at DESC);

--
-- Name: idx_bes_owner_op; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_bes_owner_op ON public.bulk_edit_snapshots USING btree (owner_id, op_id);

--
-- Name: idx_bizconn_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_bizconn_owner ON public.business_connections USING btree (owner_id);

--
-- Name: idx_bm_nodes_chat; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_bm_nodes_chat ON public.bm_telegram_nodes USING btree (tg_chat_id);

--
-- Name: idx_bm_nodes_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_bm_nodes_owner ON public.bm_telegram_nodes USING btree (owner_id);

--
-- Name: idx_bm_threads_entity; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_bm_threads_entity ON public.bm_node_threads USING btree (entity_type, entity_id);

--
-- Name: idx_bm_threads_lookup; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_bm_threads_lookup ON public.bm_node_threads USING btree (node_id, tg_thread_id);

--
-- Name: idx_bm_threads_node; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_bm_threads_node ON public.bm_node_threads USING btree (node_id, status);

--
-- Name: idx_bot_deep_links_bot_param; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_bot_deep_links_bot_param ON public.bot_deep_links USING btree (bot_id, start_param);

--
-- Name: idx_bot_flood_events_bot; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_bot_flood_events_bot ON public.bot_flood_events USING btree (bot_id, status);

--
-- Name: idx_bot_mesh_hops_task; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_bot_mesh_hops_task ON public.bot_mesh_hops USING btree (task_id, created_at);

--
-- Name: idx_bot_mesh_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_bot_mesh_owner ON public.bot_mesh_tasks USING btree (owner_id, created_at DESC);

--
-- Name: idx_bot_sales_delivery_persona; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_bot_sales_delivery_persona ON public.bot_sales_delivery USING btree (persona_id);

--
-- Name: idx_bot_sales_examples_persona; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_bot_sales_examples_persona ON public.bot_sales_examples USING btree (persona_id);

--
-- Name: idx_bot_sales_faq_persona; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_bot_sales_faq_persona ON public.bot_sales_faq USING btree (persona_id);

--
-- Name: idx_bot_sales_promos_persona; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_bot_sales_promos_persona ON public.bot_sales_promos USING btree (persona_id);

--
-- Name: idx_bot_user_facts_lookup; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_bot_user_facts_lookup ON public.bot_user_facts USING btree (bot_id, user_id);

--
-- Name: idx_bot_user_memory_lookup; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_bot_user_memory_lookup ON public.bot_user_memory USING btree (bot_id, user_id, created_at DESC);

--
-- Name: idx_bot_users_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_bot_users_active ON public.bot_users USING btree (bot_id, is_active);

--
-- Name: idx_bot_users_active_bot; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_bot_users_active_bot ON public.bot_users USING btree (bot_id, is_active) WHERE (is_active = true);

--
-- Name: idx_bot_users_bot_first_seen; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_bot_users_bot_first_seen ON public.bot_users USING btree (bot_id, first_seen DESC);

--
-- Name: idx_bot_users_bot_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_bot_users_bot_id ON public.bot_users USING btree (bot_id);

--
-- Name: idx_bot_users_phone; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_bot_users_phone ON public.bot_users USING btree (phone) WHERE (phone IS NOT NULL);

--
-- Name: idx_bot_users_suspect; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_bot_users_suspect ON public.bot_users USING btree (bot_id) WHERE suspect;

--
-- Name: idx_bot_users_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_bot_users_user_id ON public.bot_users USING btree (user_id);

--
-- Name: idx_bot_warehouse_owner_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_bot_warehouse_owner_status ON public.bot_warehouse USING btree (owner_id, status);

--
-- Name: idx_botmother_memory_owner_kind; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_botmother_memory_owner_kind ON public.botmother_memory USING btree (owner_id, kind);

--
-- Name: idx_botmother_memory_owner_updated; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_botmother_memory_owner_updated ON public.botmother_memory USING btree (owner_id, updated_at DESC);

--
-- Name: idx_botmother_memory_tags; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_botmother_memory_tags ON public.botmother_memory USING gin (tags);

--
-- Name: idx_bots_broken; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_bots_broken ON public.managed_bots USING btree (added_by) WHERE (fail_streak > 0);

--
-- Name: idx_broadcast_campaign_ops_op; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_broadcast_campaign_ops_op ON public.broadcast_campaign_ops USING btree (op_id);

--
-- Name: idx_broadcast_campaigns_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_broadcast_campaigns_owner ON public.broadcast_campaigns USING btree (owner_id, created_at DESC);

--
-- Name: idx_broadcasts_bot; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_broadcasts_bot ON public.broadcasts USING btree (bot_id);

--
-- Name: idx_broadcasts_bot_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_broadcasts_bot_created ON public.broadcasts USING btree (bot_id, created_at DESC);

--
-- Name: idx_broadcasts_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_broadcasts_status ON public.broadcasts USING btree (status);

--
-- Name: idx_bulk_ops_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_bulk_ops_owner ON public.contact_bulk_ops USING btree (owner_id, status);

--
-- Name: idx_cf_worker_pool_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_cf_worker_pool_owner ON public.cf_worker_pool USING btree (owner_id);

--
-- Name: idx_cg_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_cg_owner ON public.contact_groups USING btree (owner_id);

--
-- Name: idx_ch_contact; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_ch_contact ON public.contact_history USING btree (contact_id);

--
-- Name: idx_ch_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_ch_owner ON public.contact_history USING btree (owner_id);

--
-- Name: idx_change_events_keyword; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_change_events_keyword ON public.search_change_events USING btree (keyword_id, occurred_at DESC);

--
-- Name: idx_change_events_pending; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_change_events_pending ON public.search_change_events USING btree (confirmed, occurred_at) WHERE (confirmed = false);

--
-- Name: idx_channel_identity_history_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_channel_identity_history_owner ON public.channel_identity_history USING btree (owner_id, channel_id, created_at DESC);

--
-- Name: idx_channel_members_channel; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_channel_members_channel ON public.channel_members USING btree (channel_id);

--
-- Name: idx_channel_members_user; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_channel_members_user ON public.channel_members USING btree (user_id);

--
-- Name: idx_chat_warmup_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_chat_warmup_active ON public.chat_warmup_sessions USING btree (status) WHERE (status = 'active'::text);

--
-- Name: idx_chat_warmup_log_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_chat_warmup_log_created ON public.chat_warmup_log USING btree (created_at);

--
-- Name: idx_chat_warmup_log_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_chat_warmup_log_owner ON public.chat_warmup_log USING btree (owner_id, created_at DESC);

--
-- Name: idx_chat_warmup_log_session; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_chat_warmup_log_session ON public.chat_warmup_log USING btree (session_id, created_at DESC);

--
-- Name: idx_chat_warmup_owner_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_chat_warmup_owner_status ON public.chat_warmup_sessions USING btree (owner_id, status);

--
-- Name: idx_chatlist_folders_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_chatlist_folders_owner ON public.chatlist_folders USING btree (owner_id, created_at DESC);

--
-- Name: idx_chkw_channel; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_chkw_channel ON public.channel_tracked_keywords USING btree (channel_id);

--
-- Name: idx_chkw_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_chkw_owner ON public.channel_tracked_keywords USING btree (owner_id);

--
-- Name: idx_chrank_keyword; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_chrank_keyword ON public.channel_search_rankings USING btree (keyword_id, checked_at DESC);

--
-- Name: idx_clusters_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_clusters_owner ON public.clusters USING btree (owner_id);

--
-- Name: idx_cmh_owner_channel; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_cmh_owner_channel ON public.channel_member_history USING btree (owner_id, channel_id, captured_on DESC);

--
-- Name: idx_community_channels_node; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_community_channels_node ON public.community_channels USING btree (node_id);

--
-- Name: idx_community_members_node; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_community_members_node ON public.community_node_members USING btree (node_id, role);

--
-- Name: idx_community_nodes_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_community_nodes_owner ON public.community_nodes USING btree (owner_id, is_active);

--
-- Name: idx_competitors_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_competitors_owner ON public.competitors USING btree (owner_id);

--
-- Name: idx_contact_conflicts_contact; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_contact_conflicts_contact ON public.contact_conflicts USING btree (contact_id);

--
-- Name: idx_contact_conflicts_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_contact_conflicts_owner ON public.contact_conflicts USING btree (owner_id);

--
-- Name: idx_contact_conflicts_pending; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_contact_conflicts_pending ON public.contact_conflicts USING btree (owner_id, resolution) WHERE (resolution = 'pending'::text);

--
-- Name: idx_contact_crm_reminder; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_contact_crm_reminder ON public.contact_crm USING btree (owner_id, next_reminder_at) WHERE (next_reminder_at IS NOT NULL);

--
-- Name: idx_contact_crm_stage; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_contact_crm_stage ON public.contact_crm USING btree (owner_id, stage);

--
-- Name: idx_contact_rel_a; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_contact_rel_a ON public.contact_relationships USING btree (contact_a_id);

--
-- Name: idx_contact_rel_b; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_contact_rel_b ON public.contact_relationships USING btree (contact_b_id);

--
-- Name: idx_contact_rel_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_contact_rel_owner ON public.contact_relationships USING btree (owner_id);

--
-- Name: idx_contact_versions_contact; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_contact_versions_contact ON public.contact_versions USING btree (contact_id);

--
-- Name: idx_contact_versions_num; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_contact_versions_num ON public.contact_versions USING btree (contact_id, version_num);

--
-- Name: idx_contact_versions_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_contact_versions_owner ON public.contact_versions USING btree (owner_id);

--
-- Name: idx_content_perf_bot; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_content_perf_bot ON public.content_performance USING btree (bot_id, published_at DESC);

--
-- Name: idx_content_perf_engagement; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_content_perf_engagement ON public.content_performance USING btree (bot_id, engagement_rate DESC);

--
-- Name: idx_crm_activity_contact; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_crm_activity_contact ON public.contact_crm_activity USING btree (contact_id);

--
-- Name: idx_crm_activity_deal; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_crm_activity_deal ON public.crm_activity USING btree (deal_id);

--
-- Name: idx_crm_activity_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_crm_activity_owner ON public.contact_crm_activity USING btree (owner_id);

--
-- Name: idx_crm_contacts_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_crm_contacts_owner ON public.crm_contacts USING btree (owner_id);

--
-- Name: idx_crm_contacts_owner_user; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_crm_contacts_owner_user ON public.crm_contacts USING btree (owner_id, tg_user_id);

--
-- Name: idx_crm_deals_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_crm_deals_owner ON public.crm_deals USING btree (owner_id);

--
-- Name: idx_crm_deals_stage; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_crm_deals_stage ON public.crm_deals USING btree (owner_id, stage);

--
-- Name: idx_crosspost_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_crosspost_owner ON public.crosspost_links USING btree (owner_id, enabled);

--
-- Name: idx_cs_account; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_cs_account ON public.contact_sources USING btree (account_id);

--
-- Name: idx_cs_contact; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_cs_contact ON public.contact_sources USING btree (contact_id);

--
-- Name: idx_csl_account; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_csl_account ON public.contact_sync_log USING btree (account_id);

--
-- Name: idx_csl_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_csl_owner ON public.contact_sync_log USING btree (owner_id);

--
-- Name: idx_daughter_groups_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_daughter_groups_active ON public.daughter_groups USING btree (owner_id, mother_ref, status);

--
-- Name: idx_deep_link_visits_link; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_deep_link_visits_link ON public.deep_link_visits USING btree (link_id);

--
-- Name: idx_deep_link_visits_link_user; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_deep_link_visits_link_user ON public.deep_link_visits USING btree (link_id, user_id);

--
-- Name: idx_deep_links_bot; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_deep_links_bot ON public.bot_deep_links USING btree (bot_id);

--
-- Name: idx_device_pairings_code; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_device_pairings_code ON public.device_pairings USING btree (code) WHERE (code IS NOT NULL);

--
-- Name: idx_device_pairings_fp; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_device_pairings_fp ON public.device_pairings USING btree (token_fp) WHERE (token_fp IS NOT NULL);

--
-- Name: idx_device_pairings_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_device_pairings_owner ON public.device_pairings USING btree (owner_id);

--
-- Name: idx_discovered_bots_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_discovered_bots_owner ON public.discovered_bots USING btree (owner_id, last_seen DESC);

--
-- Name: idx_dl_build; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_dl_build ON public.deploy_log USING btree (build);

--
-- Name: idx_dm_campaigns_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_dm_campaigns_owner ON public.dm_campaigns USING btree (owner_id, status);

--
-- Name: idx_dm_log_campaign; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_dm_log_campaign ON public.dm_campaign_log USING btree (campaign_id);

--
-- Name: idx_dm_log_dedup; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_dm_log_dedup ON public.dm_campaign_log USING btree (campaign_id, tg_user_id) WHERE (status = 'sent'::text);

--
-- Name: idx_eco_dna_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_eco_dna_owner ON public.ecosystem_dna USING btree (owner_id);

--
-- Name: idx_eco_drift_eco; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_eco_drift_eco ON public.ecosystem_drift_log USING btree (ecosystem_id, detected_at DESC);

--
-- Name: idx_eco_drift_unresolved; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_eco_drift_unresolved ON public.ecosystem_drift_log USING btree (ecosystem_id, resolved);

--
-- Name: idx_eco_events_eco; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_eco_events_eco ON public.ecosystem_events USING btree (ecosystem_id, occurred_at DESC);

--
-- Name: idx_eco_events_own; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_eco_events_own ON public.ecosystem_events USING btree (owner_id, occurred_at DESC);

--
-- Name: idx_eco_members_eco; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_eco_members_eco ON public.ecosystem_members USING btree (ecosystem_id);

--
-- Name: idx_eco_members_obj; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_eco_members_obj ON public.ecosystem_members USING btree (owner_id, object_type, object_id);

--
-- Name: idx_ecosystems_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_ecosystems_owner ON public.ecosystems USING btree (owner_id);

--
-- Name: idx_ecosystems_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_ecosystems_status ON public.ecosystems USING btree (owner_id, status);

--
-- Name: idx_entity_follows_check; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_entity_follows_check ON public.entity_follows USING btree (last_checked_at NULLS FIRST);

--
-- Name: idx_entity_follows_entity; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_entity_follows_entity ON public.entity_follows USING btree (entity_id);

--
-- Name: idx_entity_follows_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_entity_follows_owner ON public.entity_follows USING btree (owner_id);

--
-- Name: idx_entity_radar_chats; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_entity_radar_chats ON public.entity_radar_stats USING btree (distinct_chats DESC);

--
-- Name: idx_entity_radar_first; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_entity_radar_first ON public.entity_radar_stats USING btree (first_seen_at);

--
-- Name: idx_error_reports_new; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_error_reports_new ON public.error_reports USING btree (created_at DESC) WHERE ((status)::text = ANY ((ARRAY['new'::character varying, 'viewing'::character varying])::text[]));

--
-- Name: idx_error_reports_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_error_reports_status ON public.error_reports USING btree (status, created_at DESC);

--
-- Name: idx_error_reports_user; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_error_reports_user ON public.error_reports USING btree (user_id, created_at DESC);

--
-- Name: idx_exp_assignments_bot_user; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_exp_assignments_bot_user ON public.experiment_assignments USING btree (bot_id, user_id);

--
-- Name: idx_exp_assignments_bot_user_exp; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_exp_assignments_bot_user_exp ON public.experiment_assignments USING btree (bot_id, user_id, experiment_id);

--
-- Name: idx_exp_assignments_variant_converted; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_exp_assignments_variant_converted ON public.experiment_assignments USING btree (variant_id, converted);

--
-- Name: idx_experiments_active_sweep; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_experiments_active_sweep ON public.experiments USING btree (status, started_at) WHERE (status = 'active'::text);

--
-- Name: idx_export_jobs_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_export_jobs_owner ON public.contact_export_jobs USING btree (owner_id, status);

--
-- Name: idx_flood_log_account; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_flood_log_account ON public.account_flood_log USING btree (account_id, created_at DESC);

--
-- Name: idx_flood_log_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_flood_log_created ON public.account_flood_log USING btree (created_at);

--
-- Name: idx_follow_events_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_follow_events_owner ON public.entity_follow_events USING btree (owner_id, detected_at DESC);

--
-- Name: idx_follow_events_unnotified; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_follow_events_unnotified ON public.entity_follow_events USING btree (notified) WHERE (NOT notified);

--
-- Name: idx_funnel_steps_funnel; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_funnel_steps_funnel ON public.funnel_steps USING btree (funnel_id);

--
-- Name: idx_funnel_steps_funnel_order; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_funnel_steps_funnel_order ON public.funnel_steps USING btree (funnel_id, step_order);

--
-- Name: idx_funnel_subs_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_funnel_subs_active ON public.funnel_subscriptions USING btree (next_send_at) WHERE ((completed = false) AND (dropped = false));

--
-- Name: idx_funnel_subs_dropped; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_funnel_subs_dropped ON public.funnel_subscriptions USING btree (funnel_id, dropped) WHERE (dropped = true);

--
-- Name: idx_funnel_subs_due; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_funnel_subs_due ON public.funnel_subscriptions USING btree (next_send_at) WHERE (completed = false);

--
-- Name: idx_funnel_subs_funnel_completed; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_funnel_subs_funnel_completed ON public.funnel_subscriptions USING btree (funnel_id, completed) WHERE (completed = false);

--
-- Name: idx_funnel_subs_funnel_user; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_funnel_subs_funnel_user ON public.funnel_subscriptions USING btree (funnel_id, user_id);

--
-- Name: idx_funnel_subs_next_send; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_funnel_subs_next_send ON public.funnel_subscriptions USING btree (next_send_at) WHERE ((completed = false) AND ((dropped IS NULL) OR (dropped = false)));

--
-- Name: idx_funnels_bot_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_funnels_bot_active ON public.funnels USING btree (bot_id) WHERE (is_active = true);

--
-- Name: idx_ghost_action_log_profile_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_ghost_action_log_profile_time ON public.ghost_action_log USING btree (ghost_profile_id, executed_at DESC);

--
-- Name: idx_gift_inventory_account; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_gift_inventory_account ON public.gift_inventory USING btree (account_id);

--
-- Name: idx_gift_inventory_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_gift_inventory_owner ON public.gift_inventory USING btree (owner_id);

--
-- Name: idx_gift_inventory_transferable; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_gift_inventory_transferable ON public.gift_inventory USING btree (owner_id, is_transferable);

--
-- Name: idx_gift_recipients_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_gift_recipients_owner ON public.gift_recipients USING btree (owner_id);

--
-- Name: idx_gift_transfer_items_plan; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_gift_transfer_items_plan ON public.gift_transfer_items USING btree (plan_id);

--
-- Name: idx_gift_transfer_items_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_gift_transfer_items_status ON public.gift_transfer_items USING btree (plan_id, status);

--
-- Name: idx_gift_transfer_ops_operation; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_gift_transfer_ops_operation ON public.gift_transfer_ops USING btree (operation_id);

--
-- Name: idx_gift_transfer_ops_plan; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_gift_transfer_ops_plan ON public.gift_transfer_ops USING btree (plan_id);

--
-- Name: idx_gift_transfer_plans_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_gift_transfer_plans_owner ON public.gift_transfer_plans USING btree (owner_id);

--
-- Name: idx_gift_transfer_plans_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_gift_transfer_plans_status ON public.gift_transfer_plans USING btree (owner_id, status);

--
-- Name: idx_gift_transfer_reports_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_gift_transfer_reports_owner ON public.gift_transfer_reports USING btree (owner_id);

--
-- Name: idx_gift_transfer_reports_plan; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_gift_transfer_reports_plan ON public.gift_transfer_reports USING btree (plan_id);

--
-- Name: idx_gp_plans_ecosystem; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_gp_plans_ecosystem ON public.global_presence_plans USING btree (ecosystem_id) WHERE (ecosystem_id IS NOT NULL);

--
-- Name: idx_gpp_owner_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_gpp_owner_status ON public.global_presence_plans USING btree (owner_id, status, created_at DESC);

--
-- Name: idx_gpt_done_assets; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_gpt_done_assets ON public.global_presence_targets USING btree (plan_id, asset_type) WHERE (status = 'done'::text);

--
-- Name: idx_gpt_final_username; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_gpt_final_username ON public.global_presence_targets USING btree (final_username) WHERE (final_username IS NOT NULL);

--
-- Name: idx_gpt_plan_pending; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_gpt_plan_pending ON public.global_presence_targets USING btree (plan_id, id) WHERE (status = 'pending'::text);

--
-- Name: idx_gpt_plan_role; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_gpt_plan_role ON public.global_presence_targets USING btree (plan_id, role);

--
-- Name: idx_gpt_plan_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_gpt_plan_status ON public.global_presence_targets USING btree (plan_id, status);

--
-- Name: idx_growth_actions_goal; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_growth_actions_goal ON public.growth_actions USING btree (goal_id, executed_at DESC);

--
-- Name: idx_growth_goals_owner_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_growth_goals_owner_active ON public.growth_goals USING btree (owner_id) WHERE ((status)::text = 'active'::text);

--
-- Name: idx_growth_goals_updated; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_growth_goals_updated ON public.growth_goals USING btree (updated_at NULLS FIRST) WHERE ((status)::text = 'active'::text);

--
-- Name: idx_guard_captcha_expires; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_guard_captcha_expires ON public.guard_captcha_pending USING btree (expires_at);

--
-- Name: idx_guard_chats_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_guard_chats_owner ON public.guard_chats USING btree (owner_id);

--
-- Name: idx_health_hist_account; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_health_hist_account ON public.account_health_history USING btree (account_id, recorded_at DESC);

--
-- Name: idx_health_hist_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_health_hist_owner ON public.account_health_history USING btree (owner_id, recorded_at DESC);

--
-- Name: idx_health_snapshots_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_health_snapshots_at ON public.system_health_snapshots USING btree (snapshot_at);

--
-- Name: idx_host_offerings_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_host_offerings_active ON public.host_offerings USING btree (is_active, kind);

--
-- Name: idx_host_offerings_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_host_offerings_owner ON public.host_offerings USING btree (owner_id);

--
-- Name: idx_host_rentals_provider; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_host_rentals_provider ON public.host_rentals USING btree (provider_id, status);

--
-- Name: idx_host_rentals_tenant; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_host_rentals_tenant ON public.host_rentals USING btree (tenant_id, status);

--
-- Name: idx_identity_contact; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_identity_contact ON public.contact_identity_graph USING btree (contact_id);

--
-- Name: idx_identity_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_identity_type ON public.contact_identity_graph USING btree (owner_id, identity_type);

--
-- Name: idx_impolicy_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_impolicy_active ON public.immunity_policy USING btree (owner_id, until);

--
-- Name: idx_imrules_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_imrules_owner ON public.immunity_rules USING btree (owner_id, enabled);

--
-- Name: idx_imsig_threat; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_imsig_threat ON public.immunity_signatures USING btree (owner_id, threat_level, updated_at DESC);

--
-- Name: idx_inactivity_alerts_bot_rule; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_inactivity_alerts_bot_rule ON public.inactivity_alerts_sent USING btree (bot_id, rule_id, sent_at DESC);

--
-- Name: idx_infra_alerts_first_seen; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_infra_alerts_first_seen ON public.infrastructure_alerts USING btree (first_seen_at);

--
-- Name: idx_infra_memory_accounts_account; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_infra_memory_accounts_account ON public.infra_memory_accounts USING btree (account_id);

--
-- Name: idx_infra_memory_accounts_updated; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_infra_memory_accounts_updated ON public.infra_memory_accounts USING btree (updated_at);

--
-- Name: idx_infra_memory_proxies_updated; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_infra_memory_proxies_updated ON public.infra_memory_proxies USING btree (updated_at);

--
-- Name: idx_infrastructure_alerts_owner_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_infrastructure_alerts_owner_active ON public.infrastructure_alerts USING btree (owner_id, is_active, first_seen_at DESC);

--
-- Name: idx_intent_hits_owner_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_intent_hits_owner_time ON public.vault_intent_hits USING btree (owner_id, created_at DESC);

--
-- Name: idx_intent_hits_rule; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_intent_hits_rule ON public.vault_intent_hits USING btree (rule_id, created_at DESC);

--
-- Name: idx_invite_overflow_owner_channel; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_invite_overflow_owner_channel ON public.invite_overflow_channels USING btree (owner_id, channel_id);

--
-- Name: idx_invite_target_log_owner_group; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_invite_target_log_owner_group ON public.invite_target_log USING btree (owner_id, group_key);

--
-- Name: idx_iol_op_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_iol_op_id ON public.intent_operation_links USING btree (op_id);

--
-- Name: idx_keyword_stats_bot; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_keyword_stats_bot ON public.keyword_stats USING btree (bot_id, count DESC);

--
-- Name: idx_keywords_bot; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_keywords_bot ON public.tracked_keywords USING btree (bot_id);

--
-- Name: idx_keywords_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_keywords_owner ON public.tracked_keywords USING btree (owner_id);

--
-- Name: idx_kw_hits_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_kw_hits_owner ON public.keyword_hits USING btree (owner_id, caught_at DESC);

--
-- Name: idx_kw_watchers_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_kw_watchers_active ON public.keyword_watchers USING btree (status) WHERE (status = 'active'::text);

--
-- Name: idx_kw_watchers_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_kw_watchers_owner ON public.keyword_watchers USING btree (owner_id, status);

--
-- Name: idx_linked_users_a; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_linked_users_a ON public.linked_platform_users USING btree (owner_id_a);

--
-- Name: idx_linked_users_b; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_linked_users_b ON public.linked_platform_users USING btree (owner_id_b);

--
-- Name: idx_llm_cooldowns_until; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_llm_cooldowns_until ON public.llm_cooldowns USING btree (until);

--
-- Name: idx_llm_usage_day; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_llm_usage_day ON public.llm_usage USING btree (day);

--
-- Name: idx_managed_bots_acc_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_managed_bots_acc_id ON public.managed_bots USING btree (acc_id);

--
-- Name: idx_managed_bots_added_by; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_managed_bots_added_by ON public.managed_bots USING btree (added_by);

--
-- Name: idx_managed_bots_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_managed_bots_owner ON public.managed_bots USING btree (added_by, added_at DESC);

--
-- Name: idx_managed_bots_profile_checked; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_managed_bots_profile_checked ON public.managed_bots USING btree (profile_checked_at NULLS FIRST) WHERE (is_active = true);

--
-- Name: idx_managed_channels_influence; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_managed_channels_influence ON public.managed_channels USING btree (owner_id, influence DESC);

--
-- Name: idx_managed_channels_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_managed_channels_owner ON public.managed_channels USING btree (owner_id);

--
-- Name: idx_managed_channels_owner_admin; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_managed_channels_owner_admin ON public.managed_channels USING btree (owner_id, is_admin);

--
-- Name: idx_managed_channels_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_managed_channels_type ON public.managed_channels USING btree (owner_id, type);

--
-- Name: idx_mesh_queue_pending_mesh; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_mesh_queue_pending_mesh ON public.mesh_queue USING btree (mesh_id, target_id, source_msg_id) WHERE (status = 'pending'::text);

--
-- Name: idx_name_history_entity; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_name_history_entity ON public.entity_name_history USING btree (entity_id, seen_at DESC);

--
-- Name: idx_name_history_username; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_name_history_username ON public.entity_name_history USING btree (username) WHERE (username IS NOT NULL);

--
-- Name: idx_narrative_campaigns_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_narrative_campaigns_owner ON public.narrative_campaigns USING btree (owner_id, status);

--
-- Name: idx_narrative_campaigns_owner_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_narrative_campaigns_owner_time ON public.narrative_campaigns USING btree (owner_id, created_at DESC);

--
-- Name: idx_narrative_posts_campaign; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_narrative_posts_campaign ON public.narrative_posts USING btree (campaign_id, status);

--
-- Name: idx_narrative_posts_channel; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_narrative_posts_channel ON public.narrative_posts USING btree (channel_id);

--
-- Name: idx_narrative_posts_scheduled; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_narrative_posts_scheduled ON public.narrative_posts USING btree (scheduled_at) WHERE ((status)::text = 'pending'::text);

--
-- Name: idx_network_edges_instance; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_network_edges_instance ON public.network_edges USING btree (instance_id);

--
-- Name: idx_network_instances_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_network_instances_owner ON public.network_instances USING btree (owner_id, created_at DESC);

--
-- Name: idx_network_nodes_instance; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_network_nodes_instance ON public.network_nodes USING btree (instance_id);

--
-- Name: idx_niche_growth_targets_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_niche_growth_targets_owner ON public.niche_growth_targets USING btree (owner_id);

--
-- Name: idx_niche_profiles_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_niche_profiles_owner ON public.account_niche_profiles USING btree (owner_id);

--
-- Name: idx_notary_obs_watch; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_notary_obs_watch ON public.notary_observations USING btree (watch_id, observed_at);

--
-- Name: idx_notary_watches_due; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_notary_watches_due ON public.notary_watches USING btree (next_check_at) WHERE (status = 'active'::text);

--
-- Name: idx_notary_watches_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_notary_watches_owner ON public.notary_watches USING btree (owner_id, created_at DESC);

--
-- Name: idx_notification_dedup_sent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_notification_dedup_sent ON public.notification_dedup USING btree (last_sent);

--
-- Name: idx_obs_state_keyword; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_obs_state_keyword ON public.observation_state USING btree (keyword_id);

--
-- Name: idx_observations_entity_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_observations_entity_time ON public.search_observations USING btree (entity_id, observed_at DESC);

--
-- Name: idx_observations_snapshot_entity; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_observations_snapshot_entity ON public.search_observations USING btree (snapshot_id, entity_id);

--
-- Name: idx_op_audit_account_action; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_op_audit_account_action ON public.operation_audit USING btree (account_id, action, occurred_at DESC);

--
-- Name: idx_op_audit_op; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_op_audit_op ON public.operation_audit USING btree (operation_id, occurred_at DESC);

--
-- Name: idx_op_audit_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_op_audit_owner ON public.operation_audit USING btree (owner_id, occurred_at DESC);

--
-- Name: idx_op_circuit_breaker_open; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_op_circuit_breaker_open ON public.op_circuit_breaker USING btree (cooldown_until) WHERE (tripped_at IS NOT NULL);

--
-- Name: idx_op_log_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_op_log_created ON public.operation_log USING btree (created_at);

--
-- Name: idx_op_log_op; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_op_log_op ON public.operation_log USING btree (op_id);

--
-- Name: idx_op_queue_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_op_queue_active ON public.operation_queue USING btree (owner_id, created_at DESC) WHERE (status = ANY (ARRAY['pending'::text, 'running'::text]));

--
-- Name: idx_op_queue_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_op_queue_owner ON public.operation_queue USING btree (owner_id, status);

--
-- Name: idx_op_queue_owner_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_op_queue_owner_created ON public.operation_queue USING btree (owner_id, created_at DESC);

--
-- Name: idx_op_queue_owner_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_op_queue_owner_status ON public.operation_queue USING btree (owner_id, status, created_at DESC);

--
-- Name: idx_op_queue_scheduled; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_op_queue_scheduled ON public.operation_queue USING btree (scheduled_for) WHERE ((scheduled_for IS NOT NULL) AND (status = 'pending'::text));

--
-- Name: idx_op_queue_status_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_op_queue_status_created ON public.operation_queue USING btree (status, created_at) WHERE (status = ANY (ARRAY['pending'::text, 'running'::text]));

--
-- Name: idx_operation_audit_account; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_operation_audit_account ON public.operation_audit USING btree (account_id, action);

--
-- Name: idx_operation_audit_occurred; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_operation_audit_occurred ON public.operation_audit USING btree (occurred_at);

--
-- Name: idx_operation_log_target_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_operation_log_target_created ON public.operation_log USING btree (target, created_at);

--
-- Name: idx_operation_queue_cancel_orphans; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_operation_queue_cancel_orphans ON public.operation_queue USING btree (finished_at) WHERE ((status = 'cancelled'::text) AND (result IS NULL));

--
-- Name: idx_operation_queue_notified; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_operation_queue_notified ON public.operation_queue USING btree (notified_at) WHERE (notified_at IS NOT NULL);

--
-- Name: idx_operation_queue_pause_requested; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_operation_queue_pause_requested ON public.operation_queue USING btree (id) WHERE (pause_requested AND (status = 'pending'::text));

--
-- Name: idx_operation_queue_scheduled; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_operation_queue_scheduled ON public.operation_queue USING btree (scheduled_for) WHERE (status = 'pending'::text);

--
-- Name: idx_opq_approval; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_opq_approval ON public.operation_queue USING btree (requires_approval, status) WHERE (requires_approval = true);

--
-- Name: idx_org_events_kind; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_org_events_kind ON public.organism_events USING btree (owner_id, kind, created_at DESC);

--
-- Name: idx_org_events_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_org_events_owner ON public.organism_events USING btree (owner_id, created_at DESC);

--
-- Name: idx_pacing_log_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_pacing_log_owner ON public.adaptive_pacing_log USING btree (owner_id, created_at);

--
-- Name: idx_parsed_audience_dedup; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_parsed_audience_dedup ON public.parsed_audiences USING btree (owner_id, source_id, tg_user_id);

--
-- Name: idx_parsed_audience_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_parsed_audience_owner ON public.parsed_audiences USING btree (owner_id, parsed_at DESC);

--
-- Name: idx_parser_runs_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_parser_runs_owner ON public.parser_runs USING btree (owner_id, started_at DESC);

--
-- Name: idx_payments_reference; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_payments_reference ON public.payments USING btree (reference);

--
-- Name: idx_payments_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_payments_status ON public.payments USING btree (status);

--
-- Name: idx_payments_user; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_payments_user ON public.payments USING btree (user_id);

--
-- Name: idx_persona_memory_persona_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_persona_memory_persona_time ON public.persona_memory USING btree (persona_id, created_at DESC);

--
-- Name: idx_persona_profiles_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_persona_profiles_owner ON public.persona_profiles USING btree (owner_id) WHERE (is_active = true);

--
-- Name: idx_phone_owner_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_phone_owner_owner ON public.phone_owner_links USING btree (owner_id);

--
-- Name: idx_phone_owner_phone; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_phone_owner_phone ON public.phone_owner_links USING btree (phone);

--
-- Name: idx_plat_ref_referred; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_plat_ref_referred ON public.platform_referrals USING btree (referred_id);

--
-- Name: idx_plat_ref_referrer; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_plat_ref_referrer ON public.platform_referrals USING btree (referrer_id);

--
-- Name: idx_platform_admin_sessions_alive; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_platform_admin_sessions_alive ON public.platform_admin_sessions USING btree (expires_at);

--
-- Name: idx_platform_proxy_valid; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_platform_proxy_valid ON public.platform_proxy_pool USING btree (is_valid, latency_ms);

--
-- Name: idx_platform_referrals_referrer; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_platform_referrals_referrer ON public.platform_referrals USING btree (referrer_id, created_at DESC);

--
-- Name: idx_platform_users_is_banned; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_platform_users_is_banned ON public.platform_users USING btree (is_banned);

--
-- Name: idx_platform_users_last_seen; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_platform_users_last_seen ON public.platform_users USING btree (last_seen DESC);

--
-- Name: idx_platform_users_plan; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_platform_users_plan ON public.platform_users USING btree (current_plan);

--
-- Name: idx_platform_users_plan_expires; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_platform_users_plan_expires ON public.platform_users USING btree (plan_expires_at);

--
-- Name: idx_platform_users_registered; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_platform_users_registered ON public.platform_users USING btree (registered_at DESC);

--
-- Name: idx_platform_users_seen; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_platform_users_seen ON public.platform_users USING btree (first_seen);

--
-- Name: idx_platform_users_uid; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_platform_users_uid ON public.platform_users USING btree (user_id);

--
-- Name: idx_pos_hist_bot; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_pos_hist_bot ON public.position_history USING btree (bot_id, keyword, checked_at DESC);

--
-- Name: idx_post_templates_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_post_templates_owner ON public.post_templates USING btree (owner_id);

--
-- Name: idx_pp_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_pp_owner ON public.presence_packs USING btree (owner_id);

--
-- Name: idx_process_heartbeats_seen; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_process_heartbeats_seen ON public.process_heartbeats USING btree (last_seen);

--
-- Name: idx_promo_logs_owner_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_promo_logs_owner_time ON public.promo_logs USING btree (owner_id, created_at DESC);

--
-- Name: idx_promo_orders_owner_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_promo_orders_owner_status ON public.promo_orders USING btree (owner_id, status, created_at DESC);

--
-- Name: idx_proxies_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_proxies_active ON public.user_proxies USING btree (is_active) WHERE is_active;

--
-- Name: idx_proxies_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_proxies_owner ON public.user_proxies USING btree (owner_id);

--
-- Name: idx_proxy_health_checked; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_proxy_health_checked ON public.proxy_health_log USING btree (checked_at);

--
-- Name: idx_proxy_health_proxy; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_proxy_health_proxy ON public.proxy_health_log USING btree (proxy_id, checked_at DESC);

--
-- Name: idx_proxy_perf_url; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_proxy_perf_url ON public.proxy_performance USING btree (proxy_url, tested_at);

--
-- Name: idx_proxy_quality_proxy; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_proxy_quality_proxy ON public.proxy_quality_log USING btree (proxy_id, checked_at DESC);

--
-- Name: idx_proxy_quality_recent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_proxy_quality_recent ON public.proxy_quality_log USING btree (checked_at DESC);

--
-- Name: idx_ract_log_session; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_ract_log_session ON public.resource_activity_log USING btree (session_id);

--
-- Name: idx_ract_sessions_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_ract_sessions_owner ON public.resource_activity_sessions USING btree (owner_id);

--
-- Name: idx_ract_sessions_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_ract_sessions_status ON public.resource_activity_sessions USING btree (status);

--
-- Name: idx_ranking_alerts_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_ranking_alerts_owner ON public.ranking_alerts USING btree (owner_id, created_at DESC);

--
-- Name: idx_ranking_alerts_pending; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_ranking_alerts_pending ON public.ranking_alerts USING btree (owner_id) WHERE (acknowledged = false);

--
-- Name: idx_rankings_bot; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_rankings_bot ON public.search_rankings USING btree (bot_id, checked_at DESC);

--
-- Name: idx_rankings_keyword; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_rankings_keyword ON public.search_rankings USING btree (keyword_id, checked_at DESC);

--
-- Name: idx_recovery_events_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_recovery_events_created ON public.recovery_events USING btree (created_at);

--
-- Name: idx_recovery_events_owner_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_recovery_events_owner_created ON public.recovery_events USING btree (owner_id, created_at DESC);

--
-- Name: idx_recovery_events_owner_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_recovery_events_owner_status ON public.recovery_events USING btree (owner_id, status);

--
-- Name: idx_ref_conv_occurred; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_ref_conv_occurred ON public.referral_conversions USING btree (occurred_at);

--
-- Name: idx_ref_conv_referrer; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_ref_conv_referrer ON public.referral_conversions USING btree (referrer_id, bot_id);

--
-- Name: idx_referrals_bot_referrer; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_referrals_bot_referrer ON public.referrals USING btree (bot_id, referrer_user_id);

--
-- Name: idx_reg_check_cache_checked_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_reg_check_cache_checked_at ON public.reg_check_cache USING btree (checked_at DESC);

--
-- Name: idx_rehab_unannounced; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_rehab_unannounced ON public.account_rehab_state USING btree (owner_id) WHERE (phase = ANY (ARRAY['freed'::text, 'stuck'::text]));

--
-- Name: idx_relay_messages_fwd; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_relay_messages_fwd ON public.relay_messages USING btree (forwarded_msg_id) WHERE (forwarded_msg_id IS NOT NULL);

--
-- Name: idx_relay_messages_session; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_relay_messages_session ON public.relay_messages USING btree (session_id, created_at DESC);

--
-- Name: idx_relay_sessions_bot; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_relay_sessions_bot ON public.relay_sessions USING btree (bot_id, last_activity DESC);

--
-- Name: idx_resource_activity_log_performed; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_resource_activity_log_performed ON public.resource_activity_log USING btree (performed_at);

--
-- Name: idx_restriction_events_account_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_restriction_events_account_created ON public.restriction_events USING btree (account_id, created_at);

--
-- Name: idx_restriction_events_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_restriction_events_created ON public.restriction_events USING btree (created_at);

--
-- Name: idx_restriction_events_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_restriction_events_owner ON public.restriction_events USING btree (owner_id, created_at DESC);

--
-- Name: idx_restriction_events_owner_recent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_restriction_events_owner_recent ON public.restriction_events USING btree (owner_id, created_at DESC) WHERE (severity = ANY (ARRAY['critical'::text, 'warning'::text]));

--
-- Name: idx_routing_log_from_bot; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_routing_log_from_bot ON public.routing_log USING btree (from_bot_id, created_at);

--
-- Name: idx_saved_segments_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_saved_segments_owner ON public.saved_segments USING btree (owner_id, created_at DESC);

--
-- Name: idx_scheduled_bot_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_scheduled_bot_id ON public.scheduled_broadcasts USING btree (bot_id);

--
-- Name: idx_scheduled_broadcasts_processing; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_scheduled_broadcasts_processing ON public.scheduled_broadcasts USING btree (claimed_at) WHERE (status = 'processing'::text);

--
-- Name: idx_scheduled_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_scheduled_status ON public.scheduled_broadcasts USING btree (status);

--
-- Name: idx_search_kw_bot; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_search_kw_bot ON public.search_keywords USING btree (bot_id);

--
-- Name: idx_search_kw_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_search_kw_owner ON public.search_keywords USING btree (owner_id);

--
-- Name: idx_search_memory_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_search_memory_owner ON public.search_memory USING btree (owner_id, affinity_score DESC);

--
-- Name: idx_search_rankings_checked; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_search_rankings_checked ON public.search_rankings USING btree (checked_at);

--
-- Name: idx_search_snapshots_checked; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_search_snapshots_checked ON public.search_snapshots USING btree (captured_at);

--
-- Name: idx_security_violations_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_security_violations_created ON public.security_violations USING btree (created_at DESC);

--
-- Name: idx_security_violations_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_security_violations_type ON public.security_violations USING btree (attempt_type);

--
-- Name: idx_security_violations_user; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_security_violations_user ON public.security_violations USING btree (user_id);

--
-- Name: idx_seen_entities_chat; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_seen_entities_chat ON public.seen_entities USING btree (chat_id);

--
-- Name: idx_seen_entities_entity; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_seen_entities_entity ON public.seen_entities USING btree (entity_id);

--
-- Name: idx_seen_entities_last; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_seen_entities_last ON public.seen_entities USING btree (last_seen_at);

--
-- Name: idx_seo_score_history_entity; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_seo_score_history_entity ON public.seo_score_history USING btree (owner_id, entity_type, entity_id, checked_at DESC);

--
-- Name: idx_shield_actions_account; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_shield_actions_account ON public.shield_actions USING btree (account_id, created_at DESC);

--
-- Name: idx_shield_actions_owner_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_shield_actions_owner_time ON public.shield_actions USING btree (owner_id, created_at DESC);

--
-- Name: idx_short_links_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_short_links_owner ON public.short_links USING btree (owner_id, created_at DESC);

--
-- Name: idx_showcase_due; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_showcase_due ON public.showcase_layers USING btree (last_wave_at) WHERE (status = 'active'::text);

--
-- Name: idx_showcase_owner_mother; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_showcase_owner_mother ON public.showcase_layers USING btree (owner_id, mother_ref, status);

--
-- Name: idx_smart_tag_rules_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_smart_tag_rules_owner ON public.smart_tag_rules USING btree (owner_id);

--
-- Name: idx_smart_tags_contact; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_smart_tags_contact ON public.contact_smart_tags USING btree (contact_id);

--
-- Name: idx_smart_tags_tag; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_smart_tags_tag ON public.contact_smart_tags USING btree (owner_id, tag);

--
-- Name: idx_snapshots_keyword_account; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_snapshots_keyword_account ON public.search_snapshots USING btree (keyword_id, account_id, captured_at DESC);

--
-- Name: idx_snapshots_keyword_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_snapshots_keyword_time ON public.search_snapshots USING btree (keyword_id, captured_at DESC);

--
-- Name: idx_snapshots_run; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_snapshots_run ON public.search_snapshots USING btree (run_id);

--
-- Name: idx_stars_events_exp; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_stars_events_exp ON public.stars_events USING btree (experiment_id, created_at DESC);

--
-- Name: idx_stars_experiments_bot; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_stars_experiments_bot ON public.stars_experiments USING btree (bot_id, status);

--
-- Name: idx_stars_transactions_bot; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_stars_transactions_bot ON public.stars_transactions USING btree (bot_id, created_at DESC);

--
-- Name: idx_strike_appeals_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_strike_appeals_owner ON public.strike_appeals USING btree (owner_id, created_at DESC);

--
-- Name: idx_strike_email_oauth; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_strike_email_oauth ON public.strike_email_accounts USING btree (owner_id, oauth_provider) WHERE (auth_type = 'oauth'::text);

--
-- Name: idx_strike_email_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_strike_email_owner ON public.strike_email_accounts USING btree (owner_id, is_active);

--
-- Name: idx_strike_history_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_strike_history_owner ON public.strike_history USING btree (owner_id, created_at);

--
-- Name: idx_strike_history_target; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_strike_history_target ON public.strike_history USING btree (target, created_at);

--
-- Name: idx_strike_reports_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_strike_reports_owner ON public.strike_reports USING btree (owner_id, created_at DESC);

--
-- Name: idx_strike_reports_target; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_strike_reports_target ON public.strike_reports USING btree (target, created_at DESC);

--
-- Name: idx_subscription_trials_tg_ids; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_subscription_trials_tg_ids ON public.subscription_trials USING gin (account_tg_user_ids);

--
-- Name: idx_subscriptions_plan_lookup; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_subscriptions_plan_lookup ON public.subscriptions USING btree (user_id, is_active, expires_at) WHERE (is_active = true);

--
-- Name: idx_subscriptions_user; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_subscriptions_user ON public.subscriptions USING btree (user_id);

--
-- Name: idx_sync_state_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_sync_state_owner ON public.contact_sync_state USING btree (owner_id);

--
-- Name: idx_system_health_snapshots_owner_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_system_health_snapshots_owner_time ON public.system_health_snapshots USING btree (owner_id, snapshot_at DESC);

--
-- Name: idx_templates_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_templates_owner ON public.message_templates USING btree (owner_id);

--
-- Name: idx_tg_acc_owner_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_tg_acc_owner_active ON public.tg_accounts USING btree (owner_id, is_active, trust_score DESC NULLS LAST);

--
-- Name: idx_tg_accounts_cf_relay; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_tg_accounts_cf_relay ON public.tg_accounts USING btree (cf_relay_url) WHERE (cf_relay_url IS NOT NULL);

--
-- Name: idx_tg_accounts_cluster; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_tg_accounts_cluster ON public.tg_accounts USING btree (owner_id, cluster) WHERE (cluster IS NOT NULL);

--
-- Name: idx_tg_accounts_cooldown; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_tg_accounts_cooldown ON public.tg_accounts USING btree (owner_id, cooldown_until) WHERE (cooldown_until IS NOT NULL);

--
-- Name: idx_tg_accounts_cooldown_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_tg_accounts_cooldown_active ON public.tg_accounts USING btree (cooldown_until);

--
-- Name: idx_tg_accounts_cooldown_cleared; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_tg_accounts_cooldown_cleared ON public.tg_accounts USING btree (owner_id, cooldown_cleared_at) WHERE (cooldown_cleared_at IS NOT NULL);

--
-- Name: idx_tg_accounts_in_operation; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_tg_accounts_in_operation ON public.tg_accounts USING btree (in_operation) WHERE (in_operation = true);

--
-- Name: idx_tg_accounts_last_used; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_tg_accounts_last_used ON public.tg_accounts USING btree (owner_id, last_used DESC NULLS LAST) WHERE (is_active = true);

--
-- Name: idx_tg_accounts_lease; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_tg_accounts_lease ON public.tg_accounts USING btree (op_lease_until) WHERE (in_operation = true);

--
-- Name: idx_tg_accounts_lease_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_tg_accounts_lease_owner ON public.tg_accounts USING btree (op_lease_owner) WHERE (in_operation = true);

--
-- Name: idx_tg_accounts_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_tg_accounts_owner ON public.tg_accounts USING btree (owner_id);

--
-- Name: idx_tg_accounts_owner_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_tg_accounts_owner_active ON public.tg_accounts USING btree (owner_id, is_active);

--
-- Name: idx_tg_accounts_phone_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_tg_accounts_phone_owner ON public.tg_accounts USING btree (owner_id, phone);

--
-- Name: idx_tg_accounts_pool; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_tg_accounts_pool ON public.tg_accounts USING btree (owner_id, pool) WHERE (pool IS NOT NULL);

--
-- Name: idx_tg_accounts_profile_checked; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_tg_accounts_profile_checked ON public.tg_accounts USING btree (profile_checked_at);

--
-- Name: idx_tg_accounts_proxy; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_tg_accounts_proxy ON public.tg_accounts USING btree (proxy_id) WHERE (proxy_id IS NOT NULL);

--
-- Name: idx_tg_accounts_session_conflict; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_tg_accounts_session_conflict ON public.tg_accounts USING btree (owner_id) WHERE (session_conflict_at IS NOT NULL);

--
-- Name: idx_tg_accounts_session_fp; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_tg_accounts_session_fp ON public.tg_accounts USING btree (session_fp);

--
-- Name: idx_tg_accounts_stage; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_tg_accounts_stage ON public.tg_accounts USING btree (owner_id, stage) WHERE (stage IS NOT NULL);

--
-- Name: idx_tg_accounts_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_tg_accounts_status ON public.tg_accounts USING btree (owner_id, acc_status);

--
-- Name: idx_tg_accounts_tags; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_tg_accounts_tags ON public.tg_accounts USING gin (tags) WHERE (array_length(tags, 1) > 0);

--
-- Name: idx_tg_accounts_trust; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_tg_accounts_trust ON public.tg_accounts USING btree (owner_id, is_active, trust_score DESC) WHERE (is_active = true);

--
-- Name: idx_tg_channels_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_tg_channels_owner ON public.tg_channels USING btree (owner_id);

--
-- Name: idx_tg_channels_private; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_tg_channels_private ON public.tg_channels USING btree (owner_id, is_private) WHERE (is_private = true);

--
-- Name: idx_tg_cloud_chunks_file; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_tg_cloud_chunks_file ON public.tg_cloud_chunks USING btree (file_id, ord);

--
-- Name: idx_tg_cloud_files_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_tg_cloud_files_owner ON public.tg_cloud_files USING btree (owner_id, id DESC);

--
-- Name: idx_tg_cloud_locs_acc; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_tg_cloud_locs_acc ON public.tg_cloud_chunk_locs USING btree (acc_id) WHERE (acc_id IS NOT NULL);

--
-- Name: idx_tg_cloud_locs_chunk; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_tg_cloud_locs_chunk ON public.tg_cloud_chunk_locs USING btree (chunk_id, status);

--
-- Name: idx_tg_cloud_storekeepers_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_tg_cloud_storekeepers_owner ON public.tg_cloud_storekeepers USING btree (owner_id);

--
-- Name: idx_trust_history_account; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_trust_history_account ON public.account_trust_history USING btree (account_id, recorded_at DESC);

--
-- Name: idx_trust_history_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_trust_history_owner ON public.account_trust_history USING btree (owner_id, recorded_at DESC);

--
-- Name: idx_uc_excluded; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_uc_excluded ON public.unified_contacts USING btree (owner_id) WHERE excluded;

--
-- Name: idx_uc_favorite; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_uc_favorite ON public.unified_contacts USING btree (owner_id, is_favorite) WHERE (is_favorite = true);

--
-- Name: idx_uc_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_uc_owner ON public.unified_contacts USING btree (owner_id);

--
-- Name: idx_uc_phone; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_uc_phone ON public.unified_contacts USING gin (phones);

--
-- Name: idx_uc_registered_est; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_uc_registered_est ON public.unified_contacts USING btree (owner_id, registered_estimate);

--
-- Name: idx_uc_tags; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_uc_tags ON public.unified_contacts USING gin (tags);

--
-- Name: idx_uc_tg_user; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_uc_tg_user ON public.unified_contacts USING btree (owner_id, telegram_user_id);

--
-- Name: idx_uc_username; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_uc_username ON public.unified_contacts USING btree (owner_id, username);

--
-- Name: idx_unified_contacts_gender; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_unified_contacts_gender ON public.unified_contacts USING btree (owner_id, gender);

--
-- Name: idx_usage_log_account; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_usage_log_account ON public.account_usage_log USING btree (account_id, created_at);

--
-- Name: idx_usage_log_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_usage_log_type ON public.account_usage_log USING btree (operation_type, created_at);

--
-- Name: idx_user_activity_bot; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_user_activity_bot ON public.user_activity USING btree (bot_id);

--
-- Name: idx_user_activity_last_seen; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_user_activity_last_seen ON public.user_activity USING btree (bot_id, last_seen);

--
-- Name: idx_user_pm_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_user_pm_owner ON public.user_payment_methods USING btree (owner_id, method_type);

--
-- Name: idx_user_proxies_owner_backup; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_user_proxies_owner_backup ON public.user_proxies USING btree (owner_id) WHERE ((is_backup = true) AND (is_active = true));

--
-- Name: idx_user_proxies_owner_fp; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_user_proxies_owner_fp ON public.user_proxies USING btree (owner_id, proxy_fp) WHERE (proxy_fp IS NOT NULL);

--
-- Name: idx_user_tags_bot_tag; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_user_tags_bot_tag ON public.user_tags USING btree (bot_id, tag);

--
-- Name: idx_user_tags_bot_user; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_user_tags_bot_user ON public.user_tags USING btree (bot_id, user_id);

--
-- Name: idx_va_admin_drafts_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_va_admin_drafts_created ON public.va_admin_drafts USING btree (created_at);

--
-- Name: idx_va_admin_drafts_owner_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_va_admin_drafts_owner_status ON public.va_admin_drafts USING btree (owner_id, status, created_at DESC);

--
-- Name: idx_va_admin_events_channel; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_va_admin_events_channel ON public.va_admin_events USING btree (owner_id, channel_id, created_at DESC);

--
-- Name: idx_va_admin_events_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_va_admin_events_created ON public.va_admin_events USING btree (created_at);

--
-- Name: idx_va_admin_plan_channel; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_va_admin_plan_channel ON public.va_admin_plan USING btree (owner_id, channel_id, status, slot_at);

--
-- Name: idx_va_admin_plan_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_va_admin_plan_created ON public.va_admin_plan USING btree (created_at);

--
-- Name: idx_va_admin_plan_unwritten; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_va_admin_plan_unwritten ON public.va_admin_plan USING btree (slot_at) WHERE ((status = 'planned'::text) AND (body IS NULL));

--
-- Name: idx_va_channel_admin_due; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_va_channel_admin_due ON public.va_channel_admin USING btree (next_post_at) WHERE enabled;

--
-- Name: idx_va_channel_brain_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_va_channel_brain_owner ON public.va_channel_brain USING btree (owner_id);

--
-- Name: idx_va_channel_posts_owner_recent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_va_channel_posts_owner_recent ON public.va_channel_posts USING btree (owner_id, published_at DESC);

--
-- Name: idx_va_channel_posts_published_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_va_channel_posts_published_at ON public.va_channel_posts USING btree (published_at);

--
-- Name: idx_va_channel_posts_recent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_va_channel_posts_recent ON public.va_channel_posts USING btree (owner_id, channel_key, published_at DESC);

--
-- Name: idx_va_learning_decisions_channel; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_va_learning_decisions_channel ON public.va_learning_decisions USING btree (owner_id, channel_id, id DESC);

--
-- Name: idx_va_media_channel; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_va_media_channel ON public.va_media USING btree (owner_id, channel_id, selected_at) WHERE enabled;

--
-- Name: idx_va_news_inbox_pending; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_va_news_inbox_pending ON public.va_news_inbox USING btree (owner_id, channel_id, published_at, id) WHERE (status = ANY (ARRAY['pending'::text, 'drafting'::text]));

--
-- Name: idx_va_posts_unlearned; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_va_posts_unlearned ON public.va_channel_posts USING btree (owner_id, channel_key, published_at) WHERE ((learning_sampled_at IS NOT NULL) AND (learned_at IS NULL));

--
-- Name: idx_va_reference_channels_due; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_va_reference_channels_due ON public.va_reference_channels USING btree (attempted_at NULLS FIRST);

--
-- Name: idx_va_tracked_links_channel; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_va_tracked_links_channel ON public.va_tracked_links USING btree (owner_id, channel_id, created_at DESC);

--
-- Name: idx_va_tracked_links_target; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_va_tracked_links_target ON public.va_tracked_links USING btree (owner_id, target_channel_id, created_at DESC);

--
-- Name: idx_vault_deleted; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_vault_deleted ON public.vault_messages USING btree (owner_id, deleted_at DESC) WHERE is_deleted;

--
-- Name: idx_vault_edited; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_vault_edited ON public.vault_messages USING btree (owner_id, edited_at DESC) WHERE is_edited;

--
-- Name: idx_vault_intent_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_vault_intent_owner ON public.vault_intent_rules USING btree (owner_id, is_active);

--
-- Name: idx_vault_owner_chat; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_vault_owner_chat ON public.vault_messages USING btree (owner_id, chat_id, msg_date);

--
-- Name: idx_vault_owner_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_vault_owner_date ON public.vault_messages USING btree (owner_id, msg_date DESC);

--
-- Name: idx_vstate_hist_entity; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_vstate_hist_entity ON public.virtual_state_history USING btree (owner_id, entity_type, entity_id, state_key, created_at);

--
-- Name: idx_vstates_expiry; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_vstates_expiry ON public.virtual_states USING btree (expires_at) WHERE (expires_at IS NOT NULL);

--
-- Name: idx_vstates_owner_key; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_vstates_owner_key ON public.virtual_states USING btree (owner_id, entity_type, state_key, value);

--
-- Name: idx_warmup_account; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_warmup_account ON public.account_warmup_plans USING btree (account_id);

--
-- Name: idx_warmup_interests_acc; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_warmup_interests_acc ON public.account_warmup_interests USING btree (account_id, last_seen_at DESC NULLS LAST, found_at DESC);

--
-- Name: idx_warmup_log_acc; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_warmup_log_acc ON public.account_warmup_log USING btree (account_id, performed_at DESC);

--
-- Name: idx_warmup_log_performed; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_warmup_log_performed ON public.account_warmup_log USING btree (performed_at);

--
-- Name: idx_warmup_pause_unnotified; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_warmup_pause_unnotified ON public.account_warmup_plans USING btree (owner_id) WHERE ((status = 'paused'::text) AND (pause_notified_at IS NULL));

--
-- Name: idx_warmup_session_log_performed; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_warmup_session_log_performed ON public.warmup_session_log USING btree (performed_at);

--
-- Name: idx_warmup_session_log_sid; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_warmup_session_log_sid ON public.warmup_session_log USING btree (session_id);

--
-- Name: idx_warmup_sessions_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_warmup_sessions_owner ON public.warmup_sessions USING btree (owner_id);

--
-- Name: idx_warmup_sessions_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_warmup_sessions_status ON public.warmup_sessions USING btree (status);

--
-- Name: idx_wb_accounts_owner_phone; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_wb_accounts_owner_phone ON public.wb_accounts USING btree (owner_id, phone);

--
-- Name: idx_wb_accounts_pick; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_wb_accounts_pick ON public.wb_accounts USING btree (owner_id, status, health_score DESC, last_used_at NULLS FIRST);

--
-- Name: idx_wb_operations_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_wb_operations_owner ON public.wb_operations USING btree (owner_id, created_at DESC);

--
-- Name: idx_wb_operations_queued; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_wb_operations_queued ON public.wb_operations USING btree (status, created_at) WHERE (status = ANY (ARRAY['queued'::text, 'running'::text]));

--
-- Name: idx_wb_targets_op_ref; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_wb_targets_op_ref ON public.wb_operation_targets USING btree (op_id, ref);

--
-- Name: idx_wb_targets_pending; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_wb_targets_pending ON public.wb_operation_targets USING btree (op_id, status);

--
-- Name: idx_workflow_definitions_bot; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_workflow_definitions_bot ON public.workflow_definitions USING btree (bot_id) WHERE (bot_id IS NOT NULL);

--
-- Name: idx_workflow_definitions_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_workflow_definitions_owner ON public.workflow_definitions USING btree (owner_id, name);

--
-- Name: idx_workflow_runs_owner_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_workflow_runs_owner_status ON public.workflow_runs USING btree (owner_id, status);

--
-- Name: idx_workflow_runs_workflow; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_workflow_runs_workflow ON public.workflow_runs USING btree (workflow_id, started_at DESC);

--
-- Name: idx_ws_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_ws_owner ON public.workspaces USING btree (owner_id);

--
-- Name: idx_wsinv_code; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_wsinv_code ON public.workspace_invites USING btree (invite_code);

--
-- Name: idx_wsm_user; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_wsm_user ON public.workspace_members USING btree (user_id);

--
-- Name: idx_wsm_ws; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_wsm_ws ON public.workspace_members USING btree (workspace_id);

--
-- Name: intents_owner_status_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX intents_owner_status_idx ON public.intents USING btree (owner_id, status, created_at DESC);

--
-- Name: ix_strike_history_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_strike_history_owner ON public.strike_history USING btree (owner_id, created_at DESC);

--
-- Name: mesh_queue_mesh_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX mesh_queue_mesh_idx ON public.mesh_queue USING btree (mesh_id);

--
-- Name: mesh_queue_pending_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX mesh_queue_pending_idx ON public.mesh_queue USING btree (scheduled_at) WHERE (status = 'pending'::text);

--
-- Name: mesh_targets_mesh_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX mesh_targets_mesh_idx ON public.mesh_targets USING btree (mesh_id);

--
-- Name: mp_api_keys_provider_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX mp_api_keys_provider_idx ON public.mp_provider_api_keys USING btree (provider_id);

--
-- Name: mp_audit_created_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX mp_audit_created_idx ON public.mp_audit_log USING btree (created_at DESC);

--
-- Name: mp_audit_entity_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX mp_audit_entity_idx ON public.mp_audit_log USING btree (entity_type, entity_id);

--
-- Name: mp_providers_category_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX mp_providers_category_idx ON public.mp_providers USING btree (category);

--
-- Name: mp_providers_owner_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX mp_providers_owner_idx ON public.mp_providers USING btree (owner_id);

--
-- Name: mp_providers_status_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX mp_providers_status_idx ON public.mp_providers USING btree (status);

--
-- Name: mp_services_active_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX mp_services_active_idx ON public.mp_services USING btree (is_active);

--
-- Name: mp_services_category_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX mp_services_category_idx ON public.mp_services USING btree (category);

--
-- Name: mp_services_kind_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX mp_services_kind_idx ON public.mp_services USING btree (resource_kind);

--
-- Name: mp_services_provider_ext_uniq; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX mp_services_provider_ext_uniq ON public.mp_services USING btree (provider_id, external_id) WHERE (external_id <> ''::text);

--
-- Name: mp_services_provider_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX mp_services_provider_idx ON public.mp_services USING btree (provider_id);

--
-- Name: op_telemetry_account_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX op_telemetry_account_idx ON public.op_telemetry USING btree (account_id, created_at DESC);

--
-- Name: op_telemetry_created_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX op_telemetry_created_idx ON public.op_telemetry USING btree (created_at DESC);

--
-- Name: op_telemetry_type_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX op_telemetry_type_idx ON public.op_telemetry USING btree (op_type, created_at DESC);

--
-- Name: payout_requests_user_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX payout_requests_user_idx ON public.payout_requests USING btree (user_id, status);

--
-- Name: promo_logs_created_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX promo_logs_created_idx ON public.promo_logs USING btree (created_at DESC);

--
-- Name: promo_logs_order_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX promo_logs_order_idx ON public.promo_logs USING btree (order_id);

--
-- Name: promo_logs_owner_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX promo_logs_owner_idx ON public.promo_logs USING btree (owner_id);

--
-- Name: promo_orders_owner_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX promo_orders_owner_idx ON public.promo_orders USING btree (owner_id);

--
-- Name: promo_orders_status_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX promo_orders_status_idx ON public.promo_orders USING btree (status);

--
-- Name: self_promo_runs_user_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX self_promo_runs_user_idx ON public.self_promo_runs USING btree (initiated_by, created_at DESC);

--
-- Name: self_promo_templates_active_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX self_promo_templates_active_idx ON public.self_promo_templates USING btree (is_active);

--
-- Name: smm_panels_owner_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX smm_panels_owner_idx ON public.smm_panels USING btree (owner_id);

--
-- Name: uq_bot_mesh_hop_step; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_bot_mesh_hop_step ON public.bot_mesh_hops USING btree (task_id, step, to_bot);

--
-- Name: uq_cf_worker_pool_owner_url; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_cf_worker_pool_owner_url ON public.cf_worker_pool USING btree (owner_id, worker_url);

--
-- Name: uq_managed_bots_extra_username; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_managed_bots_extra_username ON public.managed_bots USING btree (lower(extra_username)) WHERE (extra_username IS NOT NULL);

--
-- Name: uq_va_reference_channels; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_va_reference_channels ON public.va_reference_channels USING btree (owner_id, channel_id, lower(ref_username));

--
-- Name: tg_accounts trg_immunity_capture_status; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_immunity_capture_status AFTER UPDATE OF acc_status ON public.tg_accounts FOR EACH ROW EXECUTE FUNCTION public._immunity_capture_status();

--
-- Name: account_flood_log account_flood_log_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.account_flood_log
    ADD CONSTRAINT account_flood_log_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.tg_accounts(id) ON DELETE CASCADE;

--
-- Name: account_health_history account_health_history_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.account_health_history
    ADD CONSTRAINT account_health_history_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.tg_accounts(id) ON DELETE CASCADE;

--
-- Name: account_trust_history account_trust_history_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.account_trust_history
    ADD CONSTRAINT account_trust_history_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.tg_accounts(id) ON DELETE CASCADE;

--
-- Name: ad_posts_log ad_posts_log_placement_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ad_posts_log
    ADD CONSTRAINT ad_posts_log_placement_id_fkey FOREIGN KEY (placement_id) REFERENCES public.ad_placements(id) ON DELETE CASCADE;

--
-- Name: admin_audit_log admin_audit_log_admin_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.admin_audit_log
    ADD CONSTRAINT admin_audit_log_admin_id_fkey FOREIGN KEY (admin_id) REFERENCES public.platform_users(user_id);

--
-- Name: admin_audit_log admin_audit_log_target_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.admin_audit_log
    ADD CONSTRAINT admin_audit_log_target_user_id_fkey FOREIGN KEY (target_user_id) REFERENCES public.platform_users(user_id);

--
-- Name: ambassador_status ambassador_status_tier_key_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ambassador_status
    ADD CONSTRAINT ambassador_status_tier_key_fkey FOREIGN KEY (tier_key) REFERENCES public.ambassador_tiers(tier_key);

--
-- Name: audience_overlaps audience_overlaps_node_a_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.audience_overlaps
    ADD CONSTRAINT audience_overlaps_node_a_fkey FOREIGN KEY (node_a) REFERENCES public.graph_nodes(id) ON DELETE CASCADE;

--
-- Name: audience_overlaps audience_overlaps_node_b_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.audience_overlaps
    ADD CONSTRAINT audience_overlaps_node_b_fkey FOREIGN KEY (node_b) REFERENCES public.graph_nodes(id) ON DELETE CASCADE;

--
-- Name: auto_funnel_runs auto_funnel_runs_funnel_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auto_funnel_runs
    ADD CONSTRAINT auto_funnel_runs_funnel_id_fkey FOREIGN KEY (funnel_id) REFERENCES public.auto_funnels(id) ON DELETE CASCADE;

--
-- Name: auto_funnel_steps auto_funnel_steps_funnel_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auto_funnel_steps
    ADD CONSTRAINT auto_funnel_steps_funnel_id_fkey FOREIGN KEY (funnel_id) REFERENCES public.auto_funnels(id) ON DELETE CASCADE;

--
-- Name: auto_replies auto_replies_bot_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auto_replies
    ADD CONSTRAINT auto_replies_bot_id_fkey FOREIGN KEY (bot_id) REFERENCES public.managed_bots(bot_id) ON DELETE CASCADE;

--
-- Name: automation_rules automation_rules_bot_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.automation_rules
    ADD CONSTRAINT automation_rules_bot_id_fkey FOREIGN KEY (bot_id) REFERENCES public.managed_bots(bot_id) ON DELETE CASCADE;

--
-- Name: bm_node_threads bm_node_threads_node_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bm_node_threads
    ADD CONSTRAINT bm_node_threads_node_id_fkey FOREIGN KEY (node_id) REFERENCES public.bm_telegram_nodes(id) ON DELETE CASCADE;

--
-- Name: bot_deep_links bot_deep_links_bot_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bot_deep_links
    ADD CONSTRAINT bot_deep_links_bot_id_fkey FOREIGN KEY (bot_id) REFERENCES public.managed_bots(bot_id) ON DELETE CASCADE;

--
-- Name: bot_flood_config bot_flood_config_bot_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bot_flood_config
    ADD CONSTRAINT bot_flood_config_bot_id_fkey FOREIGN KEY (bot_id) REFERENCES public.managed_bots(bot_id) ON DELETE CASCADE;

--
-- Name: bot_mesh_hops bot_mesh_hops_task_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bot_mesh_hops
    ADD CONSTRAINT bot_mesh_hops_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.bot_mesh_tasks(task_id) ON DELETE CASCADE;

--
-- Name: bot_metrics bot_metrics_bot_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bot_metrics
    ADD CONSTRAINT bot_metrics_bot_id_fkey FOREIGN KEY (bot_id) REFERENCES public.managed_bots(bot_id) ON DELETE CASCADE;

--
-- Name: bot_routing_weights bot_routing_weights_bot_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bot_routing_weights
    ADD CONSTRAINT bot_routing_weights_bot_id_fkey FOREIGN KEY (bot_id) REFERENCES public.managed_bots(bot_id) ON DELETE CASCADE;

--
-- Name: bot_sales_dialogs bot_sales_dialogs_persona_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bot_sales_dialogs
    ADD CONSTRAINT bot_sales_dialogs_persona_id_fkey FOREIGN KEY (persona_id) REFERENCES public.bot_sales_personas(id) ON DELETE SET NULL;

--
-- Name: bot_sales_orders bot_sales_orders_persona_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bot_sales_orders
    ADD CONSTRAINT bot_sales_orders_persona_id_fkey FOREIGN KEY (persona_id) REFERENCES public.bot_sales_personas(id) ON DELETE SET NULL;

--
-- Name: bot_sales_products bot_sales_products_persona_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bot_sales_products
    ADD CONSTRAINT bot_sales_products_persona_id_fkey FOREIGN KEY (persona_id) REFERENCES public.bot_sales_personas(id) ON DELETE CASCADE;

--
-- Name: bot_update_offsets bot_update_offsets_bot_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bot_update_offsets
    ADD CONSTRAINT bot_update_offsets_bot_id_fkey FOREIGN KEY (bot_id) REFERENCES public.managed_bots(bot_id) ON DELETE CASCADE;

--
-- Name: bot_users bot_users_bot_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bot_users
    ADD CONSTRAINT bot_users_bot_id_fkey FOREIGN KEY (bot_id) REFERENCES public.managed_bots(bot_id) ON DELETE CASCADE;

--
-- Name: broadcast_campaign_ops broadcast_campaign_ops_campaign_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.broadcast_campaign_ops
    ADD CONSTRAINT broadcast_campaign_ops_campaign_id_fkey FOREIGN KEY (campaign_id) REFERENCES public.broadcast_campaigns(id) ON DELETE CASCADE;

--
-- Name: broadcasts broadcasts_bot_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.broadcasts
    ADD CONSTRAINT broadcasts_bot_id_fkey FOREIGN KEY (bot_id) REFERENCES public.managed_bots(bot_id) ON DELETE CASCADE;

--
-- Name: channel_search_rankings channel_search_rankings_keyword_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.channel_search_rankings
    ADD CONSTRAINT channel_search_rankings_keyword_id_fkey FOREIGN KEY (keyword_id) REFERENCES public.channel_tracked_keywords(id) ON DELETE CASCADE;

--
-- Name: community_channels community_channels_node_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.community_channels
    ADD CONSTRAINT community_channels_node_id_fkey FOREIGN KEY (node_id) REFERENCES public.community_nodes(id) ON DELETE CASCADE;

--
-- Name: community_node_members community_node_members_node_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.community_node_members
    ADD CONSTRAINT community_node_members_node_id_fkey FOREIGN KEY (node_id) REFERENCES public.community_nodes(id) ON DELETE CASCADE;

--
-- Name: contact_conflicts contact_conflicts_contact_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.contact_conflicts
    ADD CONSTRAINT contact_conflicts_contact_id_fkey FOREIGN KEY (contact_id) REFERENCES public.unified_contacts(id) ON DELETE CASCADE;

--
-- Name: contact_crm_activity contact_crm_activity_contact_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.contact_crm_activity
    ADD CONSTRAINT contact_crm_activity_contact_id_fkey FOREIGN KEY (contact_id) REFERENCES public.unified_contacts(id) ON DELETE CASCADE;

--
-- Name: contact_crm contact_crm_contact_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.contact_crm
    ADD CONSTRAINT contact_crm_contact_id_fkey FOREIGN KEY (contact_id) REFERENCES public.unified_contacts(id) ON DELETE CASCADE;

--
-- Name: contact_group_members contact_group_members_contact_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.contact_group_members
    ADD CONSTRAINT contact_group_members_contact_id_fkey FOREIGN KEY (contact_id) REFERENCES public.unified_contacts(id) ON DELETE CASCADE;

--
-- Name: contact_group_members contact_group_members_group_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.contact_group_members
    ADD CONSTRAINT contact_group_members_group_id_fkey FOREIGN KEY (group_id) REFERENCES public.contact_groups(id) ON DELETE CASCADE;

--
-- Name: contact_history contact_history_contact_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.contact_history
    ADD CONSTRAINT contact_history_contact_id_fkey FOREIGN KEY (contact_id) REFERENCES public.unified_contacts(id) ON DELETE CASCADE;

--
-- Name: contact_identity_graph contact_identity_graph_contact_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.contact_identity_graph
    ADD CONSTRAINT contact_identity_graph_contact_id_fkey FOREIGN KEY (contact_id) REFERENCES public.unified_contacts(id) ON DELETE CASCADE;

--
-- Name: contact_relationships contact_relationships_contact_a_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.contact_relationships
    ADD CONSTRAINT contact_relationships_contact_a_id_fkey FOREIGN KEY (contact_a_id) REFERENCES public.unified_contacts(id) ON DELETE CASCADE;

--
-- Name: contact_relationships contact_relationships_contact_b_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.contact_relationships
    ADD CONSTRAINT contact_relationships_contact_b_id_fkey FOREIGN KEY (contact_b_id) REFERENCES public.unified_contacts(id) ON DELETE CASCADE;

--
-- Name: contact_smart_tags contact_smart_tags_contact_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.contact_smart_tags
    ADD CONSTRAINT contact_smart_tags_contact_id_fkey FOREIGN KEY (contact_id) REFERENCES public.unified_contacts(id) ON DELETE CASCADE;

--
-- Name: contact_sources contact_sources_contact_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.contact_sources
    ADD CONSTRAINT contact_sources_contact_id_fkey FOREIGN KEY (contact_id) REFERENCES public.unified_contacts(id) ON DELETE CASCADE;

--
-- Name: contact_versions contact_versions_contact_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.contact_versions
    ADD CONSTRAINT contact_versions_contact_id_fkey FOREIGN KEY (contact_id) REFERENCES public.unified_contacts(id) ON DELETE CASCADE;

--
-- Name: crm_activity crm_activity_deal_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.crm_activity
    ADD CONSTRAINT crm_activity_deal_id_fkey FOREIGN KEY (deal_id) REFERENCES public.crm_deals(id) ON DELETE CASCADE;

--
-- Name: deep_link_visits deep_link_visits_link_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.deep_link_visits
    ADD CONSTRAINT deep_link_visits_link_id_fkey FOREIGN KEY (link_id) REFERENCES public.bot_deep_links(id) ON DELETE CASCADE;

--
-- Name: dm_campaign_log dm_campaign_log_campaign_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dm_campaign_log
    ADD CONSTRAINT dm_campaign_log_campaign_id_fkey FOREIGN KEY (campaign_id) REFERENCES public.dm_campaigns(id) ON DELETE CASCADE;

--
-- Name: ecosystem_members ecosystem_members_ecosystem_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ecosystem_members
    ADD CONSTRAINT ecosystem_members_ecosystem_id_fkey FOREIGN KEY (ecosystem_id) REFERENCES public.ecosystems(id) ON DELETE CASCADE;

--
-- Name: entity_follow_events entity_follow_events_follow_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.entity_follow_events
    ADD CONSTRAINT entity_follow_events_follow_id_fkey FOREIGN KEY (follow_id) REFERENCES public.entity_follows(id) ON DELETE CASCADE;

--
-- Name: experiment_assignments experiment_assignments_experiment_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.experiment_assignments
    ADD CONSTRAINT experiment_assignments_experiment_id_fkey FOREIGN KEY (experiment_id) REFERENCES public.experiments(id) ON DELETE CASCADE;

--
-- Name: experiment_assignments experiment_assignments_variant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.experiment_assignments
    ADD CONSTRAINT experiment_assignments_variant_id_fkey FOREIGN KEY (variant_id) REFERENCES public.experiment_variants(id) ON DELETE CASCADE;

--
-- Name: experiment_variants experiment_variants_experiment_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.experiment_variants
    ADD CONSTRAINT experiment_variants_experiment_id_fkey FOREIGN KEY (experiment_id) REFERENCES public.experiments(id) ON DELETE CASCADE;

--
-- Name: experiments experiments_bot_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.experiments
    ADD CONSTRAINT experiments_bot_id_fkey FOREIGN KEY (bot_id) REFERENCES public.managed_bots(bot_id) ON DELETE CASCADE;

--
-- Name: funnel_steps funnel_steps_funnel_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.funnel_steps
    ADD CONSTRAINT funnel_steps_funnel_id_fkey FOREIGN KEY (funnel_id) REFERENCES public.funnels(id) ON DELETE CASCADE;

--
-- Name: funnel_subscriptions funnel_subscriptions_funnel_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.funnel_subscriptions
    ADD CONSTRAINT funnel_subscriptions_funnel_id_fkey FOREIGN KEY (funnel_id) REFERENCES public.funnels(id) ON DELETE CASCADE;

--
-- Name: funnels funnels_bot_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.funnels
    ADD CONSTRAINT funnels_bot_id_fkey FOREIGN KEY (bot_id) REFERENCES public.managed_bots(bot_id) ON DELETE CASCADE;

--
-- Name: ghost_action_log ghost_action_log_ghost_profile_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ghost_action_log
    ADD CONSTRAINT ghost_action_log_ghost_profile_id_fkey FOREIGN KEY (ghost_profile_id) REFERENCES public.ghost_profiles(id) ON DELETE CASCADE;

--
-- Name: gift_inventory gift_inventory_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.gift_inventory
    ADD CONSTRAINT gift_inventory_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.tg_accounts(id) ON DELETE CASCADE;

--
-- Name: gift_inventory gift_inventory_owner_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.gift_inventory
    ADD CONSTRAINT gift_inventory_owner_id_fkey FOREIGN KEY (owner_id) REFERENCES public.platform_users(user_id) ON DELETE CASCADE;

--
-- Name: gift_recipients gift_recipients_owner_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.gift_recipients
    ADD CONSTRAINT gift_recipients_owner_id_fkey FOREIGN KEY (owner_id) REFERENCES public.platform_users(user_id) ON DELETE CASCADE;

--
-- Name: gift_transfer_items gift_transfer_items_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.gift_transfer_items
    ADD CONSTRAINT gift_transfer_items_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.tg_accounts(id) ON DELETE CASCADE;

--
-- Name: gift_transfer_items gift_transfer_items_inventory_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.gift_transfer_items
    ADD CONSTRAINT gift_transfer_items_inventory_id_fkey FOREIGN KEY (inventory_id) REFERENCES public.gift_inventory(id) ON DELETE CASCADE;

--
-- Name: gift_transfer_items gift_transfer_items_plan_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.gift_transfer_items
    ADD CONSTRAINT gift_transfer_items_plan_id_fkey FOREIGN KEY (plan_id) REFERENCES public.gift_transfer_plans(id) ON DELETE CASCADE;

--
-- Name: gift_transfer_ops gift_transfer_ops_operation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.gift_transfer_ops
    ADD CONSTRAINT gift_transfer_ops_operation_id_fkey FOREIGN KEY (operation_id) REFERENCES public.operation_queue(id) ON DELETE CASCADE;

--
-- Name: gift_transfer_ops gift_transfer_ops_plan_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.gift_transfer_ops
    ADD CONSTRAINT gift_transfer_ops_plan_id_fkey FOREIGN KEY (plan_id) REFERENCES public.gift_transfer_plans(id) ON DELETE CASCADE;

--
-- Name: gift_transfer_plans gift_transfer_plans_owner_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.gift_transfer_plans
    ADD CONSTRAINT gift_transfer_plans_owner_id_fkey FOREIGN KEY (owner_id) REFERENCES public.platform_users(user_id) ON DELETE CASCADE;

--
-- Name: gift_transfer_reports gift_transfer_reports_owner_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.gift_transfer_reports
    ADD CONSTRAINT gift_transfer_reports_owner_id_fkey FOREIGN KEY (owner_id) REFERENCES public.platform_users(user_id) ON DELETE CASCADE;

--
-- Name: gift_transfer_reports gift_transfer_reports_plan_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.gift_transfer_reports
    ADD CONSTRAINT gift_transfer_reports_plan_id_fkey FOREIGN KEY (plan_id) REFERENCES public.gift_transfer_plans(id) ON DELETE CASCADE;

--
-- Name: global_presence_targets global_presence_targets_plan_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.global_presence_targets
    ADD CONSTRAINT global_presence_targets_plan_id_fkey FOREIGN KEY (plan_id) REFERENCES public.global_presence_plans(id) ON DELETE CASCADE;

--
-- Name: graph_edges graph_edges_from_node_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.graph_edges
    ADD CONSTRAINT graph_edges_from_node_fkey FOREIGN KEY (from_node) REFERENCES public.graph_nodes(id) ON DELETE CASCADE;

--
-- Name: graph_edges graph_edges_to_node_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.graph_edges
    ADD CONSTRAINT graph_edges_to_node_fkey FOREIGN KEY (to_node) REFERENCES public.graph_nodes(id) ON DELETE CASCADE;

--
-- Name: growth_actions growth_actions_goal_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.growth_actions
    ADD CONSTRAINT growth_actions_goal_id_fkey FOREIGN KEY (goal_id) REFERENCES public.growth_goals(id) ON DELETE CASCADE;

--
-- Name: growth_reports growth_reports_goal_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.growth_reports
    ADD CONSTRAINT growth_reports_goal_id_fkey FOREIGN KEY (goal_id) REFERENCES public.growth_goals(id) ON DELETE CASCADE;

--
-- Name: growth_schedule growth_schedule_seed_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.growth_schedule
    ADD CONSTRAINT growth_schedule_seed_id_fkey FOREIGN KEY (seed_id) REFERENCES public.growth_content_seeds(id) ON DELETE CASCADE;

--
-- Name: host_rentals host_rentals_offering_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.host_rentals
    ADD CONSTRAINT host_rentals_offering_id_fkey FOREIGN KEY (offering_id) REFERENCES public.host_offerings(id) ON DELETE CASCADE;

--
-- Name: keyword_hits keyword_hits_watcher_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.keyword_hits
    ADD CONSTRAINT keyword_hits_watcher_id_fkey FOREIGN KEY (watcher_id) REFERENCES public.keyword_watchers(id) ON DELETE CASCADE;

--
-- Name: keyword_stats keyword_stats_bot_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.keyword_stats
    ADD CONSTRAINT keyword_stats_bot_id_fkey FOREIGN KEY (bot_id) REFERENCES public.managed_bots(bot_id) ON DELETE CASCADE;

--
-- Name: mesh_queue mesh_queue_mesh_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mesh_queue
    ADD CONSTRAINT mesh_queue_mesh_id_fkey FOREIGN KEY (mesh_id) REFERENCES public.content_meshes(id) ON DELETE CASCADE;

--
-- Name: mesh_queue mesh_queue_target_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mesh_queue
    ADD CONSTRAINT mesh_queue_target_id_fkey FOREIGN KEY (target_id) REFERENCES public.mesh_targets(id) ON DELETE CASCADE;

--
-- Name: mesh_targets mesh_targets_mesh_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mesh_targets
    ADD CONSTRAINT mesh_targets_mesh_id_fkey FOREIGN KEY (mesh_id) REFERENCES public.content_meshes(id) ON DELETE CASCADE;

--
-- Name: mp_provider_api_keys mp_provider_api_keys_provider_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mp_provider_api_keys
    ADD CONSTRAINT mp_provider_api_keys_provider_id_fkey FOREIGN KEY (provider_id) REFERENCES public.mp_providers(id) ON DELETE CASCADE;

--
-- Name: mp_services mp_services_provider_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mp_services
    ADD CONSTRAINT mp_services_provider_id_fkey FOREIGN KEY (provider_id) REFERENCES public.mp_providers(id) ON DELETE CASCADE;

--
-- Name: narrative_posts narrative_posts_campaign_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.narrative_posts
    ADD CONSTRAINT narrative_posts_campaign_id_fkey FOREIGN KEY (campaign_id) REFERENCES public.narrative_campaigns(id) ON DELETE CASCADE;

--
-- Name: network_edges network_edges_instance_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.network_edges
    ADD CONSTRAINT network_edges_instance_id_fkey FOREIGN KEY (instance_id) REFERENCES public.network_instances(id) ON DELETE CASCADE;

--
-- Name: network_edges network_edges_source_node_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.network_edges
    ADD CONSTRAINT network_edges_source_node_id_fkey FOREIGN KEY (source_node_id) REFERENCES public.network_nodes(id);

--
-- Name: network_edges network_edges_target_node_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.network_edges
    ADD CONSTRAINT network_edges_target_node_id_fkey FOREIGN KEY (target_node_id) REFERENCES public.network_nodes(id);

--
-- Name: network_instances network_instances_template_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.network_instances
    ADD CONSTRAINT network_instances_template_id_fkey FOREIGN KEY (template_id) REFERENCES public.network_templates(id);

--
-- Name: network_nodes network_nodes_instance_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.network_nodes
    ADD CONSTRAINT network_nodes_instance_id_fkey FOREIGN KEY (instance_id) REFERENCES public.network_instances(id) ON DELETE CASCADE;

--
-- Name: notary_observations notary_observations_watch_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notary_observations
    ADD CONSTRAINT notary_observations_watch_id_fkey FOREIGN KEY (watch_id) REFERENCES public.notary_watches(id) ON DELETE CASCADE;

--
-- Name: observation_state observation_state_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.observation_state
    ADD CONSTRAINT observation_state_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.tg_accounts(id) ON DELETE CASCADE;

--
-- Name: observation_state observation_state_keyword_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.observation_state
    ADD CONSTRAINT observation_state_keyword_id_fkey FOREIGN KEY (keyword_id) REFERENCES public.tracked_keywords(id) ON DELETE CASCADE;

--
-- Name: operation_log operation_log_op_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.operation_log
    ADD CONSTRAINT operation_log_op_id_fkey FOREIGN KEY (op_id) REFERENCES public.operation_queue(id) ON DELETE CASCADE;

--
-- Name: persona_memory persona_memory_persona_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.persona_memory
    ADD CONSTRAINT persona_memory_persona_id_fkey FOREIGN KEY (persona_id) REFERENCES public.persona_profiles(id) ON DELETE CASCADE;

--
-- Name: phone_owner_links phone_owner_links_owner_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.phone_owner_links
    ADD CONSTRAINT phone_owner_links_owner_id_fkey FOREIGN KEY (owner_id) REFERENCES public.platform_users(user_id) ON DELETE CASCADE;

--
-- Name: platform_referral_codes platform_referral_codes_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.platform_referral_codes
    ADD CONSTRAINT platform_referral_codes_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.platform_users(user_id);

--
-- Name: platform_referrals platform_referrals_referred_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.platform_referrals
    ADD CONSTRAINT platform_referrals_referred_id_fkey FOREIGN KEY (referred_id) REFERENCES public.platform_users(user_id);

--
-- Name: platform_referrals platform_referrals_referrer_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.platform_referrals
    ADD CONSTRAINT platform_referrals_referrer_id_fkey FOREIGN KEY (referrer_id) REFERENCES public.platform_users(user_id);

--
-- Name: platform_users platform_users_referrer_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.platform_users
    ADD CONSTRAINT platform_users_referrer_id_fkey FOREIGN KEY (referrer_id) REFERENCES public.platform_users(user_id) ON DELETE SET NULL;

--
-- Name: ranking_alerts ranking_alerts_keyword_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ranking_alerts
    ADD CONSTRAINT ranking_alerts_keyword_id_fkey FOREIGN KEY (keyword_id) REFERENCES public.tracked_keywords(id) ON DELETE CASCADE;

--
-- Name: referral_rewards referral_rewards_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.referral_rewards
    ADD CONSTRAINT referral_rewards_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.platform_users(user_id);

--
-- Name: referrals referrals_deep_link_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.referrals
    ADD CONSTRAINT referrals_deep_link_id_fkey FOREIGN KEY (deep_link_id) REFERENCES public.bot_deep_links(id) ON DELETE SET NULL;

--
-- Name: relay_messages relay_messages_session_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.relay_messages
    ADD CONSTRAINT relay_messages_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.relay_sessions(id) ON DELETE CASCADE;

--
-- Name: relay_sessions relay_sessions_bot_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.relay_sessions
    ADD CONSTRAINT relay_sessions_bot_id_fkey FOREIGN KEY (bot_id) REFERENCES public.managed_bots(bot_id) ON DELETE CASCADE;

--
-- Name: resource_activity_log resource_activity_log_session_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_activity_log
    ADD CONSTRAINT resource_activity_log_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.resource_activity_sessions(id) ON DELETE CASCADE;

--
-- Name: restriction_events restriction_events_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.restriction_events
    ADD CONSTRAINT restriction_events_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.tg_accounts(id) ON DELETE SET NULL;

--
-- Name: restriction_events restriction_events_bot_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.restriction_events
    ADD CONSTRAINT restriction_events_bot_id_fkey FOREIGN KEY (bot_id) REFERENCES public.managed_bots(bot_id) ON DELETE SET NULL;

--
-- Name: routing_rules routing_rules_source_bot_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.routing_rules
    ADD CONSTRAINT routing_rules_source_bot_id_fkey FOREIGN KEY (source_bot_id) REFERENCES public.managed_bots(bot_id) ON DELETE CASCADE;

--
-- Name: routing_rules routing_rules_target_bot_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.routing_rules
    ADD CONSTRAINT routing_rules_target_bot_id_fkey FOREIGN KEY (target_bot_id) REFERENCES public.managed_bots(bot_id) ON DELETE CASCADE;

--
-- Name: scheduled_broadcasts scheduled_broadcasts_bot_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.scheduled_broadcasts
    ADD CONSTRAINT scheduled_broadcasts_bot_id_fkey FOREIGN KEY (bot_id) REFERENCES public.managed_bots(bot_id) ON DELETE CASCADE;

--
-- Name: search_alert_cooldown search_alert_cooldown_keyword_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.search_alert_cooldown
    ADD CONSTRAINT search_alert_cooldown_keyword_id_fkey FOREIGN KEY (keyword_id) REFERENCES public.tracked_keywords(id) ON DELETE CASCADE;

--
-- Name: search_change_events search_change_events_keyword_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.search_change_events
    ADD CONSTRAINT search_change_events_keyword_id_fkey FOREIGN KEY (keyword_id) REFERENCES public.tracked_keywords(id) ON DELETE CASCADE;

--
-- Name: search_observations search_observations_snapshot_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.search_observations
    ADD CONSTRAINT search_observations_snapshot_id_fkey FOREIGN KEY (snapshot_id) REFERENCES public.search_snapshots(snapshot_id) ON DELETE CASCADE;

--
-- Name: search_rankings search_rankings_keyword_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.search_rankings
    ADD CONSTRAINT search_rankings_keyword_id_fkey FOREIGN KEY (keyword_id) REFERENCES public.tracked_keywords(id) ON DELETE CASCADE;

--
-- Name: search_snapshots search_snapshots_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.search_snapshots
    ADD CONSTRAINT search_snapshots_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.tg_accounts(id) ON DELETE CASCADE;

--
-- Name: search_snapshots search_snapshots_keyword_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.search_snapshots
    ADD CONSTRAINT search_snapshots_keyword_id_fkey FOREIGN KEY (keyword_id) REFERENCES public.tracked_keywords(id) ON DELETE CASCADE;

--
-- Name: self_promo_runs self_promo_runs_template_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.self_promo_runs
    ADD CONSTRAINT self_promo_runs_template_id_fkey FOREIGN KEY (template_id) REFERENCES public.self_promo_templates(id) ON DELETE SET NULL;

--
-- Name: stars_events stars_events_experiment_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.stars_events
    ADD CONSTRAINT stars_events_experiment_id_fkey FOREIGN KEY (experiment_id) REFERENCES public.stars_experiments(id) ON DELETE CASCADE;

--
-- Name: tg_accounts tg_accounts_added_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tg_accounts
    ADD CONSTRAINT tg_accounts_added_by_fkey FOREIGN KEY (added_by) REFERENCES public.platform_users(user_id) ON DELETE SET NULL;

--
-- Name: tg_accounts tg_accounts_proxy_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tg_accounts
    ADD CONSTRAINT tg_accounts_proxy_id_fkey FOREIGN KEY (proxy_id) REFERENCES public.user_proxies(id) ON DELETE SET NULL;

--
-- Name: tg_cloud_chunk_locs tg_cloud_chunk_locs_chunk_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tg_cloud_chunk_locs
    ADD CONSTRAINT tg_cloud_chunk_locs_chunk_id_fkey FOREIGN KEY (chunk_id) REFERENCES public.tg_cloud_chunks(id) ON DELETE CASCADE;

--
-- Name: tg_cloud_chunks tg_cloud_chunks_file_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tg_cloud_chunks
    ADD CONSTRAINT tg_cloud_chunks_file_id_fkey FOREIGN KEY (file_id) REFERENCES public.tg_cloud_files(id) ON DELETE CASCADE;

--
-- Name: user_activity user_activity_bot_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_activity
    ADD CONSTRAINT user_activity_bot_id_fkey FOREIGN KEY (bot_id) REFERENCES public.managed_bots(bot_id) ON DELETE CASCADE;

--
-- Name: user_tags user_tags_bot_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_tags
    ADD CONSTRAINT user_tags_bot_id_fkey FOREIGN KEY (bot_id) REFERENCES public.managed_bots(bot_id) ON DELETE CASCADE;

--
-- Name: va_admin_drafts va_admin_drafts_media_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.va_admin_drafts
    ADD CONSTRAINT va_admin_drafts_media_id_fkey FOREIGN KEY (media_id) REFERENCES public.va_media(id);

--
-- Name: vault_intent_hits vault_intent_hits_rule_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vault_intent_hits
    ADD CONSTRAINT vault_intent_hits_rule_id_fkey FOREIGN KEY (rule_id) REFERENCES public.vault_intent_rules(id) ON DELETE CASCADE;

--
-- Name: warmup_session_log warmup_session_log_session_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.warmup_session_log
    ADD CONSTRAINT warmup_session_log_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.warmup_sessions(id) ON DELETE CASCADE;

--
-- Name: wb_account_budget wb_account_budget_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.wb_account_budget
    ADD CONSTRAINT wb_account_budget_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.wb_accounts(id) ON DELETE CASCADE;

--
-- Name: wb_operation_targets wb_operation_targets_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.wb_operation_targets
    ADD CONSTRAINT wb_operation_targets_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.wb_accounts(id) ON DELETE SET NULL;

--
-- Name: wb_operation_targets wb_operation_targets_op_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.wb_operation_targets
    ADD CONSTRAINT wb_operation_targets_op_id_fkey FOREIGN KEY (op_id) REFERENCES public.wb_operations(id) ON DELETE CASCADE;

--
-- Name: workflow_runs workflow_runs_workflow_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workflow_runs
    ADD CONSTRAINT workflow_runs_workflow_id_fkey FOREIGN KEY (workflow_id) REFERENCES public.workflow_definitions(id) ON DELETE SET NULL;

--
-- Name: workspace_invites workspace_invites_workspace_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workspace_invites
    ADD CONSTRAINT workspace_invites_workspace_id_fkey FOREIGN KEY (workspace_id) REFERENCES public.workspaces(id) ON DELETE CASCADE;

--
-- Name: workspace_members workspace_members_invited_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workspace_members
    ADD CONSTRAINT workspace_members_invited_by_fkey FOREIGN KEY (invited_by) REFERENCES public.platform_users(user_id);

--
-- Name: workspace_members workspace_members_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workspace_members
    ADD CONSTRAINT workspace_members_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.platform_users(user_id) ON DELETE CASCADE;

--
-- Name: workspace_members workspace_members_workspace_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workspace_members
    ADD CONSTRAINT workspace_members_workspace_id_fkey FOREIGN KEY (workspace_id) REFERENCES public.workspaces(id) ON DELETE CASCADE;

--
-- Name: workspaces workspaces_owner_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workspaces
    ADD CONSTRAINT workspaces_owner_id_fkey FOREIGN KEY (owner_id) REFERENCES public.platform_users(user_id) ON DELETE CASCADE;

--
-- PostgreSQL database dump complete
--
