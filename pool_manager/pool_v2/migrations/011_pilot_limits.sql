-- Optional, immutable limits for a fresh two-member CPU testnet pilot.
CREATE TABLE pilot_limits (
    name text PRIMARY KEY CHECK (name='testnet-cpu'),
    config jsonb NOT NULL,
    actor text NOT NULL CHECK (length(actor)>0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TRIGGER immutable BEFORE UPDATE OR DELETE ON pilot_limits
    FOR EACH ROW EXECUTE FUNCTION immutable_record();
CREATE TRIGGER prevent_truncate BEFORE TRUNCATE ON pilot_limits
    FOR EACH STATEMENT EXECUTE FUNCTION immutable_record();
