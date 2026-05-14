-- SQLite schema for chainlit's built-in SQLAlchemyDataLayer.
--
-- Translated from scripts/local_stack/chainlit_schema.sql (Postgres):
--   UUID    -> TEXT  (Chainlit stores ids as strings; SQLite has no UUID)
--   JSONB   -> TEXT  (SQLAlchemyDataLayer json.dumps' the value either way)
--   TEXT[]  -> TEXT  (same — arrays come back as a JSON string)
--   BOOLEAN -> INTEGER (SQLite's native bool storage; accepts TRUE/FALSE too)
--
-- Idempotent: every CREATE / ALTER uses IF NOT EXISTS so re-applying on
-- an existing chainlit.db is a no-op. The frontend applies this at
-- startup before configuring the data layer.

CREATE TABLE IF NOT EXISTS users (
    "id" TEXT PRIMARY KEY,
    "identifier" TEXT NOT NULL UNIQUE,
    "metadata" TEXT NOT NULL,
    "createdAt" TEXT
);

CREATE TABLE IF NOT EXISTS threads (
    "id" TEXT PRIMARY KEY,
    "createdAt" TEXT,
    "name" TEXT,
    "userId" TEXT,
    "userIdentifier" TEXT,
    "tags" TEXT,
    "metadata" TEXT,
    FOREIGN KEY ("userId") REFERENCES users("id") ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS steps (
    "id" TEXT PRIMARY KEY,
    "name" TEXT NOT NULL,
    "type" TEXT NOT NULL,
    "threadId" TEXT NOT NULL,
    "parentId" TEXT,
    "streaming" INTEGER NOT NULL,
    "waitForAnswer" INTEGER,
    "isError" INTEGER,
    "metadata" TEXT,
    "tags" TEXT,
    "input" TEXT,
    "output" TEXT,
    "createdAt" TEXT,
    "command" TEXT,
    "start" TEXT,
    "end" TEXT,
    "generation" TEXT,
    "showInput" TEXT,
    "language" TEXT,
    "indent" INTEGER,
    "defaultOpen" INTEGER,
    "autoCollapse" INTEGER,
    "icon" TEXT,
    "modes" TEXT,
    FOREIGN KEY ("threadId") REFERENCES threads("id") ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS elements (
    "id" TEXT PRIMARY KEY,
    "threadId" TEXT,
    "type" TEXT,
    "url" TEXT,
    "chainlitKey" TEXT,
    "name" TEXT NOT NULL,
    "display" TEXT,
    "objectKey" TEXT,
    "size" TEXT,
    "page" INTEGER,
    "language" TEXT,
    "forId" TEXT,
    "mime" TEXT,
    "props" TEXT,
    FOREIGN KEY ("threadId") REFERENCES threads("id") ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS feedbacks (
    "id" TEXT PRIMARY KEY,
    "forId" TEXT NOT NULL,
    "threadId" TEXT NOT NULL,
    "value" INTEGER NOT NULL,
    "comment" TEXT,
    FOREIGN KEY ("threadId") REFERENCES threads("id") ON DELETE CASCADE
);

-- (No ALTER TABLE blocks here — SQLite has no `ADD COLUMN IF NOT EXISTS`
-- and would fail on re-run. The CREATE TABLE above lists every current
-- Chainlit column, so a fresh DB is complete. If Chainlit later adds
-- new columns, delete chainlit.db and let it be re-created.)
