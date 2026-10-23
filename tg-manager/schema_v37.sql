-- SEO AI suggestions cache: last AI-generated SEO proposal per channel
CREATE TABLE IF NOT EXISTS seo_ai_suggestions (
    owner_id   BIGINT  NOT NULL,
    chan_id    BIGINT  NOT NULL,
    title      TEXT,
    about      TEXT,
    username   TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (owner_id, chan_id)
);
