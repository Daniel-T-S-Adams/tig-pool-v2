-- One explicit opening entry for TIG's free testnet fee credit. Never cash.
CREATE TABLE protocol_opening_credits (
    source text PRIMARY KEY CHECK (source='tig-testnet-starter'),
    player_id text NOT NULL,
    capture_id text NOT NULL UNIQUE REFERENCES funding_captures,
    amount numeric NOT NULL CHECK (amount>0 AND amount=trunc(amount)),
    journal_id uuid NOT NULL UNIQUE REFERENCES journals,
    actor text NOT NULL CHECK (length(actor)>0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TRIGGER immutable BEFORE UPDATE OR DELETE ON protocol_opening_credits
    FOR EACH ROW EXECUTE FUNCTION immutable_record();
CREATE TRIGGER prevent_truncate BEFORE TRUNCATE ON protocol_opening_credits
    FOR EACH STATEMENT EXECUTE FUNCTION immutable_record();
