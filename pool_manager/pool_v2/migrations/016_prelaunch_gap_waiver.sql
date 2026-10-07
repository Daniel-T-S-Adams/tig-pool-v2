-- One audited accounting exception before the pool accepts any mainnet work.
-- This records an assumption; it does not fabricate an observed block.
CREATE TABLE observation_gap_waivers (
    height bigint PRIMARY KEY CHECK (height >= 0),
    round_number integer NOT NULL CHECK (round_number > 0),
    assumed_block_id text NOT NULL,
    previous_observed_block_id text NOT NULL REFERENCES observed_blocks(id),
    next_observed_block_id text NOT NULL REFERENCES observed_blocks(id),
    policy text NOT NULL UNIQUE CHECK (policy = 'one-time-prelaunch-zero-pool-credit-v1'),
    actor text NOT NULL CHECK (length(btrim(actor)) > 0),
    reason text NOT NULL CHECK (length(btrim(reason)) > 0),
    evidence jsonb NOT NULL CHECK (jsonb_typeof(evidence) = 'object'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (previous_observed_block_id <> next_observed_block_id)
);
CREATE INDEX observation_gap_waivers_round ON observation_gap_waivers(round_number, height);
CREATE TRIGGER observation_gap_waivers_immutable BEFORE UPDATE OR DELETE ON observation_gap_waivers
    FOR EACH ROW EXECUTE FUNCTION immutable_record();
CREATE TRIGGER observation_gap_waivers_prevent_truncate BEFORE TRUNCATE ON observation_gap_waivers
    FOR EACH STATEMENT EXECUTE FUNCTION immutable_record();
