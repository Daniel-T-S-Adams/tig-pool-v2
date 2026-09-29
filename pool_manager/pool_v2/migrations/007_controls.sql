CREATE TABLE runtime_controls (
    name text PRIMARY KEY CHECK (name='new_work_paused'),
    value boolean NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE runtime_control_events (
    event_key text PRIMARY KEY,
    name text NOT NULL CHECK (name='new_work_paused'),
    before_value boolean NOT NULL,
    after_value boolean NOT NULL,
    actor text NOT NULL,
    reason text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TRIGGER immutable BEFORE UPDATE OR DELETE ON runtime_control_events
    FOR EACH ROW EXECUTE FUNCTION immutable_record();
CREATE TRIGGER prevent_truncate BEFORE TRUNCATE ON runtime_control_events
    FOR EACH STATEMENT EXECUTE FUNCTION immutable_record();
