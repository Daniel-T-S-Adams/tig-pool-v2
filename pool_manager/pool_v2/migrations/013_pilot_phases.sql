-- Later test phases extend cumulative allowances without resetting history.
CREATE TABLE pilot_phases (
    number integer PRIMARY KEY CHECK (number>0),
    phase_key text NOT NULL UNIQUE CHECK (length(phase_key) BETWEEN 1 AND 128),
    config jsonb NOT NULL,
    previous_number integer NOT NULL CHECK (previous_number=number-1),
    attempts_before integer NOT NULL CHECK (attempts_before>=0),
    actor text NOT NULL CHECK (length(actor) BETWEEN 1 AND 200),
    reason text NOT NULL CHECK (length(reason) BETWEEN 1 AND 1000),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TRIGGER immutable BEFORE UPDATE OR DELETE ON pilot_phases
    FOR EACH ROW EXECUTE FUNCTION immutable_record();
CREATE TRIGGER prevent_truncate BEFORE TRUNCATE ON pilot_phases
    FOR EACH STATEMENT EXECUTE FUNCTION immutable_record();
