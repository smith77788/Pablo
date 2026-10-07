-- Migration: add_infrastructure_models
-- Created: 2026-05-27
-- Description: Add infrastructure models - assets, accounts, clusters, operations, visibility

-- ─── ENUMS ────────────────────────────────────────────────────────────────────

CREATE TYPE "AssetType" AS ENUM (
  'TELEGRAM_ACCOUNT',
  'TELEGRAM_BOT',
  'CHANNEL',
  'GROUP',
  'CHAT',
  'PROXY',
  'SESSION',
  'KEYWORD',
  'COMPETITOR',
  'OPERATION_TEMPLATE'
);

CREATE TYPE "AssetStatus" AS ENUM (
  'ACTIVE',
  'INACTIVE',
  'WARNING',
  'LIMITED',
  'UNSTABLE',
  'DISCONNECTED',
  'ARCHIVED'
);

CREATE TYPE "TgAccountStatus" AS ENUM (
  'ACTIVE',
  'DISCONNECTED',
  'REQUIRES_LOGIN',
  'WARNING',
  'LIMITED',
  'UNSTABLE',
  'ARCHIVED'
);

CREATE TYPE "ProxyType" AS ENUM (
  'SOCKS5',
  'HTTP',
  'HTTPS',
  'MTPROTO'
);

CREATE TYPE "ProxyStatus" AS ENUM (
  'ACTIVE',
  'INACTIVE',
  'FAILED',
  'CHECKING'
);

CREATE TYPE "OperationStatus" AS ENUM (
  'DRAFT',
  'PENDING_APPROVAL',
  'APPROVED',
  'SCHEDULED',
  'QUEUED',
  'RUNNING',
  'PAUSED',
  'COMPLETED',
  'FAILED',
  'CANCELLED'
);

CREATE TYPE "OperationStepStatus" AS ENUM (
  'PENDING',
  'RUNNING',
  'COMPLETED',
  'FAILED',
  'SKIPPED',
  'CANCELLED'
);

-- ─── PROJECTS ─────────────────────────────────────────────────────────────────

CREATE TABLE "projects" (
  "id"          TEXT         NOT NULL DEFAULT gen_random_uuid()::text,
  "tenantId"    TEXT         NOT NULL,
  "name"        TEXT         NOT NULL,
  "description" TEXT,
  "niche"       TEXT,
  "language"    TEXT         NOT NULL DEFAULT 'ru',
  "region"      TEXT,
  "tags"        TEXT[]       NOT NULL DEFAULT ARRAY[]::TEXT[],
  "isActive"    BOOLEAN      NOT NULL DEFAULT true,
  "createdAt"   TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
  "updatedAt"   TIMESTAMP(3) NOT NULL,

  CONSTRAINT "projects_pkey" PRIMARY KEY ("id")
);

CREATE INDEX "projects_tenantId_idx" ON "projects"("tenantId");

ALTER TABLE "projects"
  ADD CONSTRAINT "projects_tenantId_fkey"
  FOREIGN KEY ("tenantId") REFERENCES "tenants"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- ─── ASSETS ───────────────────────────────────────────────────────────────────

CREATE TABLE "assets" (
  "id"              TEXT         NOT NULL DEFAULT gen_random_uuid()::text,
  "tenantId"        TEXT         NOT NULL,
  "projectId"       TEXT,
  "clusterId"       TEXT,
  "type"            "AssetType"  NOT NULL,
  "name"            TEXT         NOT NULL,
  "username"        TEXT,
  "externalId"      TEXT,
  "status"          "AssetStatus" NOT NULL DEFAULT 'ACTIVE',
  "healthScore"     DOUBLE PRECISION NOT NULL DEFAULT 1.0,
  "riskScore"       DOUBLE PRECISION NOT NULL DEFAULT 0.0,
  "visibilityScore" DOUBLE PRECISION NOT NULL DEFAULT 0.0,
  "activityScore"   DOUBLE PRECISION NOT NULL DEFAULT 0.0,
  "operationalLoad" DOUBLE PRECISION NOT NULL DEFAULT 0.0,
  "tags"            TEXT[]       NOT NULL DEFAULT ARRAY[]::TEXT[],
  "metadata"        JSONB        NOT NULL DEFAULT '{}',
  "notes"           TEXT,
  "lastActivityAt"  TIMESTAMP(3),
  "archivedAt"      TIMESTAMP(3),
  "createdAt"       TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
  "updatedAt"       TIMESTAMP(3) NOT NULL,

  CONSTRAINT "assets_pkey" PRIMARY KEY ("id")
);

CREATE INDEX "assets_tenantId_type_idx" ON "assets"("tenantId", "type");
CREATE INDEX "assets_tenantId_status_idx" ON "assets"("tenantId", "status");

ALTER TABLE "assets"
  ADD CONSTRAINT "assets_tenantId_fkey"
  FOREIGN KEY ("tenantId") REFERENCES "tenants"("id") ON DELETE CASCADE ON UPDATE CASCADE;

ALTER TABLE "assets"
  ADD CONSTRAINT "assets_projectId_fkey"
  FOREIGN KEY ("projectId") REFERENCES "projects"("id") ON DELETE SET NULL ON UPDATE CASCADE;

-- clusterId FK added after clusters table is created (see below)

-- ─── PROXIES ──────────────────────────────────────────────────────────────────

CREATE TABLE "proxies" (
  "id"                    TEXT         NOT NULL DEFAULT gen_random_uuid()::text,
  "tenantId"              TEXT         NOT NULL,
  "host"                  TEXT         NOT NULL,
  "port"                  INTEGER      NOT NULL,
  "type"                  "ProxyType"  NOT NULL DEFAULT 'SOCKS5',
  "usernameEncrypted"     TEXT,
  "passwordEncrypted"     TEXT,
  "region"                TEXT,
  "status"                "ProxyStatus" NOT NULL DEFAULT 'ACTIVE',
  "latencyMs"             INTEGER,
  "healthScore"           DOUBLE PRECISION NOT NULL DEFAULT 1.0,
  "failureCount"          INTEGER      NOT NULL DEFAULT 0,
  "assignedAccountsCount" INTEGER      NOT NULL DEFAULT 0,
  "lastCheckedAt"         TIMESTAMP(3),
  "createdAt"             TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
  "updatedAt"             TIMESTAMP(3) NOT NULL,

  CONSTRAINT "proxies_pkey" PRIMARY KEY ("id")
);

CREATE INDEX "proxies_tenantId_idx" ON "proxies"("tenantId");

ALTER TABLE "proxies"
  ADD CONSTRAINT "proxies_tenantId_fkey"
  FOREIGN KEY ("tenantId") REFERENCES "tenants"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- ─── CLUSTERS ─────────────────────────────────────────────────────────────────

CREATE TABLE "clusters" (
  "id"              TEXT         NOT NULL DEFAULT gen_random_uuid()::text,
  "tenantId"        TEXT         NOT NULL,
  "projectId"       TEXT,
  "name"            TEXT         NOT NULL,
  "description"     TEXT,
  "type"            TEXT         NOT NULL DEFAULT 'general',
  "language"        TEXT,
  "region"          TEXT,
  "niche"           TEXT,
  "tags"            TEXT[]       NOT NULL DEFAULT ARRAY[]::TEXT[],
  "assetsCount"     INTEGER      NOT NULL DEFAULT 0,
  "accountsCount"   INTEGER      NOT NULL DEFAULT 0,
  "botsCount"       INTEGER      NOT NULL DEFAULT 0,
  "channelsCount"   INTEGER      NOT NULL DEFAULT 0,
  "healthScore"     DOUBLE PRECISION NOT NULL DEFAULT 1.0,
  "visibilityScore" DOUBLE PRECISION NOT NULL DEFAULT 0.0,
  "activityScore"   DOUBLE PRECISION NOT NULL DEFAULT 0.0,
  "operationalLoad" DOUBLE PRECISION NOT NULL DEFAULT 0.0,
  "riskScore"       DOUBLE PRECISION NOT NULL DEFAULT 0.0,
  "createdAt"       TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
  "updatedAt"       TIMESTAMP(3) NOT NULL,

  CONSTRAINT "clusters_pkey" PRIMARY KEY ("id")
);

CREATE INDEX "clusters_tenantId_idx" ON "clusters"("tenantId");

ALTER TABLE "clusters"
  ADD CONSTRAINT "clusters_tenantId_fkey"
  FOREIGN KEY ("tenantId") REFERENCES "tenants"("id") ON DELETE CASCADE ON UPDATE CASCADE;

ALTER TABLE "clusters"
  ADD CONSTRAINT "clusters_projectId_fkey"
  FOREIGN KEY ("projectId") REFERENCES "projects"("id") ON DELETE SET NULL ON UPDATE CASCADE;

-- Add deferred FK from assets to clusters
ALTER TABLE "assets"
  ADD CONSTRAINT "assets_clusterId_fkey"
  FOREIGN KEY ("clusterId") REFERENCES "clusters"("id") ON DELETE SET NULL ON UPDATE CASCADE;

-- ─── TELEGRAM ACCOUNTS ────────────────────────────────────────────────────────

CREATE TABLE "telegram_accounts" (
  "id"               TEXT             NOT NULL DEFAULT gen_random_uuid()::text,
  "tenantId"         TEXT             NOT NULL,
  "projectId"        TEXT,
  "clusterId"        TEXT,
  "proxyId"          TEXT,
  "phone"            TEXT,
  "username"         TEXT,
  "firstName"        TEXT,
  "lastName"         TEXT,
  "tgUserId"         BIGINT,
  "sessionEncrypted" TEXT,
  "status"           "TgAccountStatus" NOT NULL DEFAULT 'ACTIVE',
  "healthScore"      DOUBLE PRECISION NOT NULL DEFAULT 1.0,
  "trustScore"       DOUBLE PRECISION NOT NULL DEFAULT 1.0,
  "riskScore"        DOUBLE PRECISION NOT NULL DEFAULT 0.0,
  "floodCount7d"     INTEGER          NOT NULL DEFAULT 0,
  "lastFloodAt"      TIMESTAMP(3),
  "cooldownUntil"    TIMESTAMP(3),
  "deviceModel"      TEXT,
  "systemVersion"    TEXT,
  "appVersion"       TEXT,
  "tags"             TEXT[]           NOT NULL DEFAULT ARRAY[]::TEXT[],
  "notes"            TEXT,
  "isActive"         BOOLEAN          NOT NULL DEFAULT true,
  "lastUsedAt"       TIMESTAMP(3),
  "createdAt"        TIMESTAMP(3)     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  "updatedAt"        TIMESTAMP(3)     NOT NULL,

  CONSTRAINT "telegram_accounts_pkey" PRIMARY KEY ("id")
);

CREATE INDEX "telegram_accounts_tenantId_idx" ON "telegram_accounts"("tenantId");
CREATE INDEX "telegram_accounts_tenantId_status_idx" ON "telegram_accounts"("tenantId", "status");

ALTER TABLE "telegram_accounts"
  ADD CONSTRAINT "telegram_accounts_tenantId_fkey"
  FOREIGN KEY ("tenantId") REFERENCES "tenants"("id") ON DELETE CASCADE ON UPDATE CASCADE;

ALTER TABLE "telegram_accounts"
  ADD CONSTRAINT "telegram_accounts_proxyId_fkey"
  FOREIGN KEY ("proxyId") REFERENCES "proxies"("id") ON DELETE SET NULL ON UPDATE CASCADE;

-- ─── ACCOUNT FLOOD LOGS ───────────────────────────────────────────────────────

CREATE TABLE "account_flood_logs" (
  "id"          TEXT         NOT NULL DEFAULT gen_random_uuid()::text,
  "accountId"   TEXT         NOT NULL,
  "operation"   TEXT,
  "waitSeconds" INTEGER      NOT NULL DEFAULT 0,
  "createdAt"   TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

  CONSTRAINT "account_flood_logs_pkey" PRIMARY KEY ("id")
);

ALTER TABLE "account_flood_logs"
  ADD CONSTRAINT "account_flood_logs_accountId_fkey"
  FOREIGN KEY ("accountId") REFERENCES "telegram_accounts"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- ─── OPERATIONS ───────────────────────────────────────────────────────────────

CREATE TABLE "operations" (
  "id"                TEXT             NOT NULL DEFAULT gen_random_uuid()::text,
  "tenantId"          TEXT             NOT NULL,
  "projectId"         TEXT,
  "createdById"       TEXT             NOT NULL,
  "approvedById"      TEXT,
  "name"              TEXT             NOT NULL,
  "description"       TEXT,
  "type"              TEXT             NOT NULL,
  "targetScope"       JSONB            NOT NULL DEFAULT '{}',
  "selectedAssets"    TEXT[]           NOT NULL DEFAULT ARRAY[]::TEXT[],
  "selectedClusters"  TEXT[]           NOT NULL DEFAULT ARRAY[]::TEXT[],
  "status"            "OperationStatus" NOT NULL DEFAULT 'DRAFT',
  "riskScore"         DOUBLE PRECISION NOT NULL DEFAULT 0.0,
  "safetyScore"       DOUBLE PRECISION NOT NULL DEFAULT 1.0,
  "estimatedDuration" INTEGER,
  "scheduledAt"       TIMESTAMP(3),
  "startedAt"         TIMESTAMP(3),
  "completedAt"       TIMESTAMP(3),
  "result"            JSONB,
  "errorMessage"      TEXT,
  "createdAt"         TIMESTAMP(3)     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  "updatedAt"         TIMESTAMP(3)     NOT NULL,

  CONSTRAINT "operations_pkey" PRIMARY KEY ("id")
);

CREATE INDEX "operations_tenantId_status_idx" ON "operations"("tenantId", "status");

ALTER TABLE "operations"
  ADD CONSTRAINT "operations_tenantId_fkey"
  FOREIGN KEY ("tenantId") REFERENCES "tenants"("id") ON DELETE CASCADE ON UPDATE CASCADE;

ALTER TABLE "operations"
  ADD CONSTRAINT "operations_projectId_fkey"
  FOREIGN KEY ("projectId") REFERENCES "projects"("id") ON DELETE SET NULL ON UPDATE CASCADE;

-- ─── OPERATION STEPS ──────────────────────────────────────────────────────────

CREATE TABLE "operation_steps" (
  "id"           TEXT                  NOT NULL DEFAULT gen_random_uuid()::text,
  "operationId"  TEXT                  NOT NULL,
  "accountId"    TEXT,
  "assetId"      TEXT,
  "actionType"   TEXT                  NOT NULL,
  "payload"      JSONB                 NOT NULL DEFAULT '{}',
  "scheduledFor" TIMESTAMP(3),
  "status"       "OperationStepStatus" NOT NULL DEFAULT 'PENDING',
  "attempts"     INTEGER               NOT NULL DEFAULT 0,
  "result"       JSONB,
  "errorMessage" TEXT,
  "createdAt"    TIMESTAMP(3)          NOT NULL DEFAULT CURRENT_TIMESTAMP,
  "updatedAt"    TIMESTAMP(3)          NOT NULL,

  CONSTRAINT "operation_steps_pkey" PRIMARY KEY ("id")
);

CREATE INDEX "operation_steps_operationId_status_idx" ON "operation_steps"("operationId", "status");

ALTER TABLE "operation_steps"
  ADD CONSTRAINT "operation_steps_operationId_fkey"
  FOREIGN KEY ("operationId") REFERENCES "operations"("id") ON DELETE CASCADE ON UPDATE CASCADE;

ALTER TABLE "operation_steps"
  ADD CONSTRAINT "operation_steps_accountId_fkey"
  FOREIGN KEY ("accountId") REFERENCES "telegram_accounts"("id") ON DELETE SET NULL ON UPDATE CASCADE;

-- ─── TIMING PROFILES ──────────────────────────────────────────────────────────

CREATE TABLE "timing_profiles" (
  "id"                        TEXT             NOT NULL DEFAULT gen_random_uuid()::text,
  "accountId"                 TEXT             NOT NULL,
  "allowedOperationTypes"     TEXT[]           NOT NULL DEFAULT ARRAY[]::TEXT[],
  "dailyActionBudget"         INTEGER          NOT NULL DEFAULT 50,
  "hourlyActionBudget"        INTEGER          NOT NULL DEFAULT 10,
  "minIntervalSeconds"        INTEGER          NOT NULL DEFAULT 30,
  "reliabilityScore"          DOUBLE PRECISION NOT NULL DEFAULT 1.0,
  "historicalSuccessRate"     DOUBLE PRECISION NOT NULL DEFAULT 1.0,
  "operationalTemperature"    DOUBLE PRECISION NOT NULL DEFAULT 0.0,
  "projectedNextSafeActionAt" TIMESTAMP(3),
  "lastActionAt"              TIMESTAMP(3),
  "createdAt"                 TIMESTAMP(3)     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  "updatedAt"                 TIMESTAMP(3)     NOT NULL,

  CONSTRAINT "timing_profiles_pkey" PRIMARY KEY ("id")
);

CREATE UNIQUE INDEX "timing_profiles_accountId_key" ON "timing_profiles"("accountId");

ALTER TABLE "timing_profiles"
  ADD CONSTRAINT "timing_profiles_accountId_fkey"
  FOREIGN KEY ("accountId") REFERENCES "telegram_accounts"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- ─── VISIBILITY KEYWORDS ──────────────────────────────────────────────────────

CREATE TABLE "visibility_keywords" (
  "id"        TEXT         NOT NULL DEFAULT gen_random_uuid()::text,
  "tenantId"  TEXT         NOT NULL,
  "projectId" TEXT,
  "groupName" TEXT,
  "keyword"   TEXT         NOT NULL,
  "language"  TEXT         NOT NULL DEFAULT 'ru',
  "region"    TEXT,
  "isActive"  BOOLEAN      NOT NULL DEFAULT true,
  "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
  "updatedAt" TIMESTAMP(3) NOT NULL,

  CONSTRAINT "visibility_keywords_pkey" PRIMARY KEY ("id")
);

CREATE UNIQUE INDEX "visibility_keywords_tenantId_keyword_key" ON "visibility_keywords"("tenantId", "keyword");
CREATE INDEX "visibility_keywords_tenantId_idx" ON "visibility_keywords"("tenantId");

ALTER TABLE "visibility_keywords"
  ADD CONSTRAINT "visibility_keywords_tenantId_fkey"
  FOREIGN KEY ("tenantId") REFERENCES "tenants"("id") ON DELETE CASCADE ON UPDATE CASCADE;

ALTER TABLE "visibility_keywords"
  ADD CONSTRAINT "visibility_keywords_projectId_fkey"
  FOREIGN KEY ("projectId") REFERENCES "projects"("id") ON DELETE SET NULL ON UPDATE CASCADE;

-- ─── KEYWORD POSITIONS ────────────────────────────────────────────────────────

CREATE TABLE "keyword_positions" (
  "id"          TEXT         NOT NULL DEFAULT gen_random_uuid()::text,
  "keywordId"   TEXT         NOT NULL,
  "assetId"     TEXT,
  "assetType"   TEXT,
  "position"    INTEGER,
  "previousPos" INTEGER,
  "delta"       INTEGER,
  "checkedAt"   TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

  CONSTRAINT "keyword_positions_pkey" PRIMARY KEY ("id")
);

CREATE INDEX "keyword_positions_keywordId_checkedAt_idx" ON "keyword_positions"("keywordId", "checkedAt");

ALTER TABLE "keyword_positions"
  ADD CONSTRAINT "keyword_positions_keywordId_fkey"
  FOREIGN KEY ("keywordId") REFERENCES "visibility_keywords"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- ─── COMPETITORS ──────────────────────────────────────────────────────────────

CREATE TABLE "competitors" (
  "id"        TEXT         NOT NULL DEFAULT gen_random_uuid()::text,
  "tenantId"  TEXT         NOT NULL,
  "projectId" TEXT,
  "name"      TEXT         NOT NULL,
  "username"  TEXT,
  "type"      TEXT         NOT NULL DEFAULT 'channel',
  "isTracked" BOOLEAN      NOT NULL DEFAULT true,
  "metadata"  JSONB        NOT NULL DEFAULT '{}',
  "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
  "updatedAt" TIMESTAMP(3) NOT NULL,

  CONSTRAINT "competitors_pkey" PRIMARY KEY ("id")
);

CREATE INDEX "competitors_tenantId_idx" ON "competitors"("tenantId");

ALTER TABLE "competitors"
  ADD CONSTRAINT "competitors_tenantId_fkey"
  FOREIGN KEY ("tenantId") REFERENCES "tenants"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- ─── AUDIT LOGS ───────────────────────────────────────────────────────────────

CREATE TABLE "audit_logs" (
  "id"          TEXT         NOT NULL DEFAULT gen_random_uuid()::text,
  "tenantId"    TEXT         NOT NULL,
  "operatorId"  TEXT,
  "operationId" TEXT,
  "action"      TEXT         NOT NULL,
  "entityType"  TEXT,
  "entityId"    TEXT,
  "before"      JSONB,
  "after"       JSONB,
  "ipAddress"   TEXT,
  "userAgent"   TEXT,
  "createdAt"   TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

  CONSTRAINT "audit_logs_pkey" PRIMARY KEY ("id")
);

CREATE INDEX "audit_logs_tenantId_createdAt_idx" ON "audit_logs"("tenantId", "createdAt");
CREATE INDEX "audit_logs_tenantId_entityType_entityId_idx" ON "audit_logs"("tenantId", "entityType", "entityId");

ALTER TABLE "audit_logs"
  ADD CONSTRAINT "audit_logs_tenantId_fkey"
  FOREIGN KEY ("tenantId") REFERENCES "tenants"("id") ON DELETE CASCADE ON UPDATE CASCADE;

ALTER TABLE "audit_logs"
  ADD CONSTRAINT "audit_logs_operationId_fkey"
  FOREIGN KEY ("operationId") REFERENCES "operations"("id") ON DELETE SET NULL ON UPDATE CASCADE;
