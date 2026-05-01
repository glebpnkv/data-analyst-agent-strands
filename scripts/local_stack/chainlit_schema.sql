-- Schema for chainlit's built-in SQLAlchemyDataLayer (chainlit.data.sql_alchemy).
--
-- Source: official Chainlit docs (docs.chainlit.io/data-layers/sqlalchemy).
-- Note: this differs from chainlit-datalayer (a separate Chainlit-team
-- service that uses a Prisma-managed PascalCase schema). We use the
-- in-process SQLAlchemyDataLayer here, so this is the matching schema.
--
-- Idempotent: every CREATE uses IF NOT EXISTS, so re-running on an
-- existing DB is a no-op. run_local_stack.sh applies this on every
-- container start; no separate migration tool needed in dev.

CREATE TABLE IF NOT EXISTS users (
    "id" UUID PRIMARY KEY,
    "identifier" TEXT NOT NULL UNIQUE,
    "metadata" JSONB NOT NULL,
    "createdAt" TEXT
);

CREATE TABLE IF NOT EXISTS threads (
    "id" UUID PRIMARY KEY,
    "createdAt" TEXT,
    "name" TEXT,
    "userId" UUID,
    "userIdentifier" TEXT,
    "tags" TEXT[],
    "metadata" JSONB,
    FOREIGN KEY ("userId") REFERENCES users("id") ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS steps (
    "id" UUID PRIMARY KEY,
    "name" TEXT NOT NULL,
    "type" TEXT NOT NULL,
    "threadId" UUID NOT NULL,
    "parentId" UUID,
    "streaming" BOOLEAN NOT NULL,
    "waitForAnswer" BOOLEAN,
    "isError" BOOLEAN,
    "metadata" JSONB,
    "tags" TEXT[],
    "input" TEXT,
    "output" TEXT,
    "createdAt" TEXT,
    "command" TEXT,
    "start" TEXT,
    "end" TEXT,
    "generation" JSONB,
    "showInput" TEXT,
    "language" TEXT,
    "indent" INT,
    "defaultOpen" BOOLEAN,
    "autoCollapse" BOOLEAN,
    "icon" TEXT,
    "modes" JSONB,
    FOREIGN KEY ("threadId") REFERENCES threads("id") ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS elements (
    "id" UUID PRIMARY KEY,
    "threadId" UUID,
    "type" TEXT,
    "url" TEXT,
    "chainlitKey" TEXT,
    "name" TEXT NOT NULL,
    "display" TEXT,
    "objectKey" TEXT,
    "size" TEXT,
    "page" INT,
    "language" TEXT,
    "forId" UUID,
    "mime" TEXT,
    "props" JSONB,
    FOREIGN KEY ("threadId") REFERENCES threads("id") ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS feedbacks (
    "id" UUID PRIMARY KEY,
    "forId" UUID NOT NULL,
    "threadId" UUID NOT NULL,
    "value" INT NOT NULL,
    "comment" TEXT,
    FOREIGN KEY ("threadId") REFERENCES threads("id") ON DELETE CASCADE
);

-- Forward-compat upgrades: any column the chainlit StepDict / ElementDict
-- adds in newer releases that wasn't in the original CREATE TABLE goes
-- here. ADD COLUMN IF NOT EXISTS is a no-op on tables that already have
-- the column, so this can be re-run alongside the CREATEs above.
-- Existing volumes from prior boots upgrade in place; fresh ones already
-- have these columns from CREATE TABLE.
ALTER TABLE steps ADD COLUMN IF NOT EXISTS "autoCollapse" BOOLEAN;
ALTER TABLE steps ADD COLUMN IF NOT EXISTS "icon" TEXT;
