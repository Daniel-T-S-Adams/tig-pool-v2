-- Owner decision, 14 October 2026: a reward round may settle with a bounded
-- number of unrecoverable missing heights. Each tolerated height counts as zero
-- pool credit and is listed in the settlement inputs; the raw gap stays visible
-- and no block is fabricated. A round missing more than the limit, or holding a
-- conflicting block, remains held for review. Policy rows are append-only so
-- every change keeps its actor and reason.
CREATE TABLE missing_block_policies (
    revision bigserial PRIMARY KEY,
    policy text NOT NULL CHECK (policy = 'bounded-zero-credit-v1'),
    max_missing_per_round integer NOT NULL CHECK (max_missing_per_round >= 0),
    warn_missing_per_round integer NOT NULL
        CHECK (warn_missing_per_round >= 0 AND warn_missing_per_round <= max_missing_per_round),
    actor text NOT NULL CHECK (length(btrim(actor)) > 0),
    reason text NOT NULL CHECK (length(btrim(reason)) > 0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TRIGGER immutable BEFORE UPDATE OR DELETE ON missing_block_policies
    FOR EACH ROW EXECUTE FUNCTION immutable_record();
CREATE TRIGGER prevent_truncate BEFORE TRUNCATE ON missing_block_policies
    FOR EACH STATEMENT EXECUTE FUNCTION immutable_record();
INSERT INTO missing_block_policies (policy, max_missing_per_round, warn_missing_per_round, actor, reason)
VALUES ('bounded-zero-credit-v1', 50, 10, 'pool owner',
        'Decision of 14 October 2026: a few unrecoverable missing blocks per round count as zero credit; rounds missing more are held for review');
