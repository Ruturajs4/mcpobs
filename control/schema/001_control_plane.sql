-- The control plane (DF-9). Postgres, global, NOT per-cell (Architecture.md §3).
--
-- WHY POSTGRES AND NOT CLICKHOUSE
-- ClickHouse holds telemetry: append-mostly, enormous, eventually consistent
-- enough. This holds identity: small, mutating, and read on the authentication
-- path of every single ingest request. Revoking a key must take effect now, not
-- after a merge. Those are different databases because they are different
-- problems, and ADR-009 already ruled that control-plane writes are synchronous
-- Postgres writes rather than events.

CREATE TABLE IF NOT EXISTS orgs (
    id          BIGSERIAL PRIMARY KEY,
    slug        TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    plan        TEXT NOT NULL DEFAULT 'trial',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- `tenant_id` in the telemetry tables IS `orgs.slug`, deliberately.
-- A surrogate integer would be smaller, but every ClickHouse row, every Kafka
-- partition key and every debugging session would then need a join against a
-- different database to answer "whose data is this?". The slug is stable,
-- human-readable and immutable, which is the whole requirement.
COMMENT ON COLUMN orgs.slug IS 'Equals tenant_id in ClickHouse and the Kafka partition key.';

CREATE TABLE IF NOT EXISTS users (
    id          BIGSERIAL PRIMARY KEY,
    org_id      BIGINT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    email       TEXT NOT NULL,
    name        TEXT NOT NULL DEFAULT '',
    role        TEXT NOT NULL DEFAULT 'member',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (email)
);

-- INVITE-ONLY. There is no signup table and no self-service path to an org,
-- because the product does not have one: an account exists only because
-- somebody already inside issued an invite.
--
-- Enforcing that here rather than in the API is the point. An API-layer check is
-- one forgotten branch away from an open front door; a schema with no way to
-- create a user except through a consumed invite cannot be bypassed by a route
-- someone adds later.
CREATE TABLE IF NOT EXISTS invites (
    id          BIGSERIAL PRIMARY KEY,
    org_id      BIGINT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    email       TEXT NOT NULL,
    -- The code is shown ONCE, at creation, and only its hash is stored. An
    -- invite code is a bearer credential: whoever holds it becomes a member of
    -- someone's organisation, so a database dump must not contain usable ones.
    code_hash   TEXT NOT NULL UNIQUE,
    role        TEXT NOT NULL DEFAULT 'member',
    invited_by  BIGINT REFERENCES users(id) ON DELETE SET NULL,
    expires_at  TIMESTAMPTZ NOT NULL,
    -- Single use. Set when redeemed; a partial unique index would be
    -- over-engineering when the accepting UPDATE is already conditional.
    accepted_at TIMESTAMPTZ,
    accepted_by BIGINT REFERENCES users(id) ON DELETE SET NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS invites_org_idx ON invites (org_id);

CREATE TABLE IF NOT EXISTS projects (
    id          BIGSERIAL PRIMARY KEY,
    org_id      BIGINT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    slug        TEXT NOT NULL,
    name        TEXT NOT NULL DEFAULT '',
    environment TEXT NOT NULL DEFAULT 'production',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (org_id, slug)
);

-- API keys.
--
-- FORMAT: mcpo_<environment>_<prefix>_<secret>
--   prefix -- 8 chars, stored in clear and INDEXED. It is what makes
--             authentication a single indexed lookup instead of a scan that
--             hashes every row.
--   secret -- 32 bytes of urandom, stored only as a SHA-256 hash.
--
-- WHY PLAIN SHA-256 AND NOT BCRYPT/ARGON2
-- Those exist to make brute force expensive against LOW-ENTROPY inputs -- human
-- passwords. This secret is 256 bits from `secrets.token_urlsafe`, so brute
-- force is not the threat model and a deliberately slow hash would only add
-- tens of milliseconds to the hot path of every ingest request. Hashing at all
-- is what matters: a leaked dump must not contain usable keys.
--
-- This reasoning does NOT transfer to a password column. If one is ever added,
-- it needs a real password hash, and this comment is here so nobody copies the
-- wrong precedent.
CREATE TABLE IF NOT EXISTS api_keys (
    id           BIGSERIAL PRIMARY KEY,
    org_id       BIGINT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    project_id   BIGINT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name         TEXT NOT NULL DEFAULT '',
    prefix       TEXT NOT NULL UNIQUE,
    secret_hash  TEXT NOT NULL,
    -- 'ingest' writes telemetry; 'read' queries it. Separate because they leak
    -- differently: an ingest key lives in a customer's server process and in
    -- their deployment config, a read key in a browser. One compromised should
    -- not imply the other.
    scopes       TEXT NOT NULL DEFAULT 'ingest',
    created_by   BIGINT REFERENCES users(id) ON DELETE SET NULL,
    last_used_at TIMESTAMPTZ,
    expires_at   TIMESTAMPTZ,
    revoked_at   TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS api_keys_prefix_idx ON api_keys (prefix);
CREATE INDEX IF NOT EXISTS api_keys_org_idx ON api_keys (org_id);
