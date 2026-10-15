-- Whole-network selection references are read on demand, separately from the
-- per-block pool captures, and keep only the best bundle per algorithm and
-- track with its parameters. Payloads expire under the block floor unless a
-- reservation cites the capture; rows and identities stay.
CREATE TABLE reference_captures (
    id uuid PRIMARY KEY,
    block_id text NOT NULL,
    height bigint NOT NULL CHECK (height>=0),
    round integer NOT NULL CHECK (round>0),
    block_timestamp bigint NOT NULL CHECK (block_timestamp>=0),
    source_semantic_digest text NOT NULL CHECK (length(source_semantic_digest)=64),
    payload_sha256 text NOT NULL CHECK (length(payload_sha256)=64),
    compressed_payload bytea NOT NULL,
    metadata jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    expired_at timestamptz,
    UNIQUE (block_id, payload_sha256)
);
CREATE INDEX reference_captures_by_height ON reference_captures(height DESC, created_at DESC);
CREATE FUNCTION reference_capture_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='DELETE' OR current_setting('pool_v2.retention',true) IS DISTINCT FROM 'on'
       OR (to_jsonb(NEW)-'compressed_payload'-'expired_at') <> (to_jsonb(OLD)-'compressed_payload'-'expired_at')
       OR OLD.expired_at IS NOT NULL OR NEW.expired_at IS NULL OR length(NEW.compressed_payload)<>0
       OR NOT EXISTS (SELECT 1 FROM retention_floors f WHERE f.name='evidence' AND OLD.height<=f.block_height)
       OR EXISTS (SELECT 1 FROM reservations WHERE selection->'reference_capture'->>'id'=OLD.id::text)
    THEN RAISE EXCEPTION 'network references expire only under the guarded retention floor'; END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER retention BEFORE UPDATE OR DELETE ON reference_captures
    FOR EACH ROW EXECUTE FUNCTION reference_capture_guard();
CREATE TRIGGER prevent_truncate BEFORE TRUNCATE ON reference_captures
    FOR EACH STATEMENT EXECUTE FUNCTION immutable_record();
