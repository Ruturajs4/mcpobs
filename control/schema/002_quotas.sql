-- Per-org quota overrides (Architecture.md §5.1, ADR-008).
--
-- The PLAN limits live in code (`control/quota.py`), because they are product
-- decisions that change on a release cadence. What belongs in the database is
-- the exception: this customer, on this plan, with a limit raised because
-- support agreed to it. A plans TABLE would have invited someone to edit
-- product limits with prod SQL, which is how two environments end up disagreeing
-- about what "pro" means.
--
-- NULL means "use the plan". Not 0 -- 0 is a meaningful value here (unlimited),
-- so it cannot double as "unset" without making an override to unlimited
-- indistinguishable from no override at all.
ALTER TABLE orgs ADD COLUMN IF NOT EXISTS quota_spans_per_minute BIGINT;
ALTER TABLE orgs ADD COLUMN IF NOT EXISTS quota_spans_per_day BIGINT;

COMMENT ON COLUMN orgs.quota_spans_per_minute IS
    'NULL = use the plan limit. 0 = unlimited. Overrides control/quota.py.';
