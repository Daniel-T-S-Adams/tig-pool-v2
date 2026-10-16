-- Operator-recorded, append-only spending caps. The newest row is the cap in
-- force: a rolling window with the most TIG the pool may commit to precommit
-- fees and the most native token it may reserve for custody sends, plus a
-- ceiling per attempt. Without a row the pool commits no fees at all.
CREATE TABLE spending_caps (
    id uuid PRIMARY KEY,
    event_key text NOT NULL UNIQUE,
    window_seconds integer NOT NULL CHECK (window_seconds BETWEEN 3600 AND 31622400),
    tig_fee_window_units numeric NOT NULL CHECK (tig_fee_window_units>=0 AND tig_fee_window_units=trunc(tig_fee_window_units)),
    tig_fee_attempt_units numeric NOT NULL CHECK (tig_fee_attempt_units>=0 AND tig_fee_attempt_units=trunc(tig_fee_attempt_units)
        AND tig_fee_attempt_units<=tig_fee_window_units),
    native_fee_window_units numeric NOT NULL CHECK (native_fee_window_units>=0 AND native_fee_window_units=trunc(native_fee_window_units)),
    native_fee_attempt_units numeric NOT NULL CHECK (native_fee_attempt_units>=0 AND native_fee_attempt_units=trunc(native_fee_attempt_units)
        AND native_fee_attempt_units<=native_fee_window_units),
    actor text NOT NULL CHECK (length(actor)>0),
    reason text NOT NULL CHECK (length(reason)>0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX spending_caps_latest ON spending_caps (created_at DESC, id DESC);
CREATE TRIGGER immutable BEFORE UPDATE OR DELETE ON spending_caps
    FOR EACH ROW EXECUTE FUNCTION immutable_record();
CREATE TRIGGER prevent_truncate BEFORE TRUNCATE ON spending_caps
    FOR EACH STATEMENT EXECUTE FUNCTION immutable_record();
