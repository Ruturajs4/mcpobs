-- Alerting (DF-10). An org defines a rule against the ClickHouse rollup
-- (tool_metrics_1m, 012_rollups.sql) and gets notified when it breaches.
--
-- WHY THE STATE LIVES HERE, IN POSTGRES, NOT IN CLICKHOUSE
-- `tool_metrics_1m` is where the numbers live; whether a rule has already
-- fired is identity, not telemetry -- small, mutating, read and written by
-- the evaluator on every tick. Same reasoning 001's header gives for putting
-- the control plane in Postgres at all.
--
-- WHY A DEBOUNCE COLUMN EXISTS
-- The rollup cannot apply the argMax(..., normalization_version) replay-dedup
-- every other read in this codebase uses (D24) -- a materialized view only
-- sees an insert batch, not version history. A replay transiently inflates
-- `tool_metrics_1m`'s counters until scripts/recompute_rollups.py reconciles
-- them (the same gap assertion E1 exists to catch). Firing on one breached
-- evaluation window would alarm on that inflation. `consecutive_breaches`
-- requires the condition to hold across N evaluations in a row before the
-- rule actually fires -- absorbing the hazard instead of re-deriving from
-- raw spans on every tick, which is a bigger departure from this codebase's
-- accepted rollup trade-off than the debounce is.
--
-- RESOLVING IS NOT DEBOUNCED. Fast to resolve, slow to fire: a rule that
-- stays "firing" after the condition clears is worse than one that flickers,
-- and a state machine that can only move toward "ok" on good data can never
-- get stuck.
CREATE TABLE IF NOT EXISTS alert_channels (
    id          BIGSERIAL PRIMARY KEY,
    org_id      BIGINT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,               -- 'slack' | 'webhook'
    -- The webhook URL. Never returned by a GET on the admin API -- same "a
    -- database dump must contain no usable credential" reasoning 001 gives
    -- for api_keys.secret_hash, extended to "a bearer URL is a credential".
    target      TEXT NOT NULL,
    created_by  BIGINT REFERENCES users(id) ON DELETE SET NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS alert_channels_org_idx ON alert_channels (org_id);

CREATE TABLE IF NOT EXISTS alert_rules (
    id                   BIGSERIAL PRIMARY KEY,
    org_id               BIGINT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    project_id           BIGINT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name                 TEXT NOT NULL,
    -- 'error_rate' | 'p95_latency_ms' | 'calls'. A closed set, not a free
    -- column -- the evaluator switches on this, and an unrecognised value
    -- must fail loudly there rather than be silently accepted here.
    metric               TEXT NOT NULL,
    -- NULL = every tool in the project. Set = one tool only, matching
    -- tool_metrics_1m's own mcp_tool_name column.
    tool_name            TEXT,
    threshold            DOUBLE PRECISION NOT NULL,
    window_minutes       INT NOT NULL DEFAULT 5,
    consecutive_breaches INT NOT NULL DEFAULT 2,
    channel_id           BIGINT NOT NULL REFERENCES alert_channels(id) ON DELETE CASCADE,
    enabled              BOOLEAN NOT NULL DEFAULT true,
    created_by           BIGINT REFERENCES users(id) ON DELETE SET NULL,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS alert_rules_org_idx ON alert_rules (org_id);
-- The evaluator's own query: every enabled rule, on every tick.
CREATE INDEX IF NOT EXISTS alert_rules_enabled_idx ON alert_rules (enabled) WHERE enabled;

CREATE TABLE IF NOT EXISTS alert_rule_state (
    rule_id            BIGINT PRIMARY KEY REFERENCES alert_rules(id) ON DELETE CASCADE,
    status             TEXT NOT NULL DEFAULT 'ok',  -- 'ok' | 'firing'
    consecutive_count  INT NOT NULL DEFAULT 0,
    last_evaluated_at  TIMESTAMPTZ,
    last_fired_at      TIMESTAMPTZ,
    last_resolved_at   TIMESTAMPTZ
);
