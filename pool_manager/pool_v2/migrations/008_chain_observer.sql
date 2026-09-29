CREATE TABLE chain_stream (
    name text PRIMARY KEY CHECK (name='custody'),
    network jsonb NOT NULL,
    start_height bigint NOT NULL CHECK (start_height>0),
    last_height bigint NOT NULL,
    last_hash text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (last_height>=start_height-1)
);
CREATE FUNCTION protect_chain_stream() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='DELETE' THEN RAISE EXCEPTION 'custody observation cannot be disabled by deleting its cursor'; END IF;
    IF (NEW.name,NEW.network,NEW.start_height,NEW.created_at) IS DISTINCT FROM
       (OLD.name,OLD.network,OLD.start_height,OLD.created_at) OR NEW.last_height<OLD.last_height
       OR (NEW.last_height=OLD.last_height AND NEW.last_hash<>OLD.last_hash)
    THEN RAISE EXCEPTION 'immutable custody observation identity or backwards cursor'; END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER stream_guard BEFORE UPDATE OR DELETE ON chain_stream FOR EACH ROW EXECUTE FUNCTION protect_chain_stream();
CREATE TRIGGER prevent_truncate BEFORE TRUNCATE ON chain_stream FOR EACH STATEMENT EXECUTE FUNCTION immutable_record();
CREATE TABLE chain_captures (
    id text PRIMARY KEY,
    payload_gzip bytea NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE chain_batches (
    capture_id text PRIMARY KEY REFERENCES chain_captures,
    first_height bigint NOT NULL,
    last_height bigint NOT NULL,
    block_hash text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (last_height>=first_height-1)
);
CREATE TABLE custody_checks (
    id bigserial PRIMARY KEY,
    capture_id text NOT NULL REFERENCES chain_captures,
    height bigint,
    target_height bigint,
    block_hash text,
    actual_tig numeric,
    actual_native numeric,
    recorded_tig numeric,
    recorded_native numeric,
    outgoing_nonce bigint,
    accounted_nonces bigint,
    healthy boolean NOT NULL,
    reason text NOT NULL,
    checked_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE chain_alerts (
    id bigserial PRIMARY KEY,
    capture_id text NOT NULL REFERENCES chain_captures,
    kind text NOT NULL,
    details jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
DO $$ DECLARE item text; BEGIN
    FOREACH item IN ARRAY ARRAY['chain_captures','chain_batches','custody_checks','chain_alerts'] LOOP
        EXECUTE format('CREATE TRIGGER immutable BEFORE UPDATE OR DELETE ON %I FOR EACH ROW EXECUTE FUNCTION immutable_record()',item);
        EXECUTE format('CREATE TRIGGER prevent_truncate BEFORE TRUNCATE ON %I FOR EACH STATEMENT EXECUTE FUNCTION immutable_record()',item);
    END LOOP;
END $$;
