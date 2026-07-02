-- TelegramUserSession
CREATE TABLE "telegram_user_sessions" (
    "id" TEXT NOT NULL PRIMARY KEY,
    "telegramId" BIGINT NOT NULL UNIQUE,
    "tenantId" TEXT,
    "currentMenu" TEXT NOT NULL DEFAULT 'main',
    "currentSection" TEXT NOT NULL DEFAULT '',
    "wizardState" JSONB,
    "wizardStep" INTEGER NOT NULL DEFAULT 0,
    "tempData" JSONB,
    "paginationPage" INTEGER NOT NULL DEFAULT 0,
    "lastActivity" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    "createdAt" TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- PendingConfirmation
CREATE TABLE "pending_confirmations" (
    "id" TEXT NOT NULL PRIMARY KEY,
    "sessionId" TEXT NOT NULL REFERENCES "telegram_user_sessions"("id") ON DELETE CASCADE,
    "actionType" TEXT NOT NULL,
    "actionData" JSONB NOT NULL,
    "expiresAt" TIMESTAMPTZ NOT NULL,
    "confirmedAt" TIMESTAMPTZ,
    "cancelledAt" TIMESTAMPTZ,
    "createdAt" TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- OperationQueueStatus enum
CREATE TYPE "OperationQueueStatus" AS ENUM ('PENDING', 'RUNNING', 'DONE', 'FAILED', 'CANCELLED');

-- OperationQueue
CREATE TABLE "operation_queues" (
    "id" TEXT NOT NULL PRIMARY KEY,
    "tenantId" TEXT NOT NULL,
    "opType" TEXT NOT NULL,
    "status" "OperationQueueStatus" NOT NULL DEFAULT 'PENDING',
    "params" JSONB NOT NULL DEFAULT '{}',
    "result" JSONB,
    "totalItems" INTEGER NOT NULL DEFAULT 0,
    "doneItems" INTEGER NOT NULL DEFAULT 0,
    "errorMsg" TEXT,
    "createdAt" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    "startedAt" TIMESTAMPTZ,
    "finishedAt" TIMESTAMPTZ
);

CREATE INDEX "operation_queues_tenantId_status_idx" ON "operation_queues"("tenantId", "status");

-- OperationQueueStep
CREATE TABLE "operation_queue_steps" (
    "id" TEXT NOT NULL PRIMARY KEY,
    "operationId" TEXT NOT NULL REFERENCES "operation_queues"("id") ON DELETE CASCADE,
    "stepNum" INTEGER NOT NULL,
    "target" TEXT,
    "status" TEXT NOT NULL,
    "message" TEXT,
    "createdAt" TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX "operation_queue_steps_operationId_idx" ON "operation_queue_steps"("operationId");

-- AssetTemplate
CREATE TABLE "asset_templates" (
    "id" TEXT NOT NULL PRIMARY KEY,
    "tenantId" TEXT NOT NULL,
    "name" TEXT NOT NULL,
    "assetType" TEXT NOT NULL,
    "template" JSONB NOT NULL DEFAULT '{}',
    "createdAt" TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX "asset_templates_tenantId_assetType_idx" ON "asset_templates"("tenantId", "assetType");
