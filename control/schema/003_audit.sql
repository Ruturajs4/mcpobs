-- Audit log for operator actions.
--
-- WHY IT EXISTS
-- The operator console carries two destructive levers -- hard-quota a tenant and
-- revoke a key -- and until this table nothing recorded who pulled either. "Who
-- hard-quotaed this customer at 3am" is a question that eventually gets asked,
-- and an admin surface where the answer is "we cannot tell" makes every other
-- control on it unverifiable: you cannot investigate a misuse you cannot see.
--
-- IT LIVES IN POSTGRES, BESIDE WHAT IT AUDITS, AND THAT IS THE POINT
-- The action and its record are written in ONE TRANSACTION. That removes the
-- question every audit log otherwise has to answer badly:
--
--   * write the record first and the action can fail -> a phantom entry;
--   * write it after and the process can die -> an unlogged action;
--   * make the action depend on the log and an audit outage blocks revoking a
--     LEAKED KEY, which is the one operation you cannot afford to delay.
--
-- Because both rows live in the same database, none of those trades has to be
-- made: either both land or neither does. If Postgres is down, the action was
-- never going to succeed anyway.
--
-- WHAT IS NOT AUDITED, DELIBERATELY
-- Reads. The console refreshes every 30 seconds, so auditing list views would
-- write thousands of rows a day whose signal is "an operator had a tab open".
-- That volume would bury the mutations, which are the entries anyone will ever
-- search for. "Who looked at customer X" is a real compliance question and it is
-- not answered here -- said plainly rather than left to be assumed.
--
-- NO TTL. Every other table in this system drops old rows on a schedule. An
-- audit log that expires is one an investigation discovers empty, and these
-- rows are tiny.
CREATE TABLE IF NOT EXISTS audit_log (
    id           BIGSERIAL PRIMARY KEY,
    at           TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- The acting credential. `actor_key_id` is a real reference for joining,
    -- and `actor_prefix`/`actor_org` are DENORMALISED COPIES so the record
    -- still names its actor after the key row is gone. An audit entry that
    -- becomes anonymous when someone deletes a key is an audit entry with a
    -- delete button attached to it.
    actor_key_id BIGINT REFERENCES api_keys(id) ON DELETE SET NULL,
    actor_prefix TEXT NOT NULL DEFAULT '',
    actor_org    TEXT NOT NULL DEFAULT '',
    -- 'console' or 'cli'. CLI actions are audited too: `scripts/admin.py` needs
    -- database access, which makes it the MORE privileged path, not one to
    -- exempt.
    actor_source TEXT NOT NULL DEFAULT 'console',

    action       TEXT NOT NULL,
    -- What was acted on: an org slug, a key prefix.
    target       TEXT NOT NULL DEFAULT '',
    -- 'ok' | 'denied' | 'failed'. Refused attempts are recorded, and are the
    -- most interesting rows in the table: a `denied` entry against the admin
    -- surface is somebody trying a credential that does not work.
    outcome      TEXT NOT NULL DEFAULT 'ok',
    -- Before and after, so an entry says what changed rather than only that
    -- something did.
    detail       JSONB NOT NULL DEFAULT '{}'::jsonb,
    source_ip    TEXT NOT NULL DEFAULT ''
);

-- Newest first is how it is always read.
CREATE INDEX IF NOT EXISTS audit_log_at_idx ON audit_log (at DESC);
-- "Everything that ever happened to this tenant" is the second question.
CREATE INDEX IF NOT EXISTS audit_log_target_idx ON audit_log (target, at DESC);
