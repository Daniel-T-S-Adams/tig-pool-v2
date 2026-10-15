-- Backfill compact evidence without rewriting immutable block identity.
-- Raw copies can expire individually once the replacement, credit and
-- reconciliation are backed up. Missing heights remain missing heights.
CREATE TABLE compact_block_copies (
    block_id text PRIMARY KEY REFERENCES observed_blocks,
    source_semantic_digest text NOT NULL CHECK (length(source_semantic_digest)=64),
    projection_digest text NOT NULL CHECK (length(projection_digest)=64),
    payload_sha256 text NOT NULL CHECK (length(payload_sha256)=64),
    compressed_payload bytea NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    expired_at timestamptz
);
CREATE FUNCTION compact_copy_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='DELETE' OR current_setting('pool_v2.retention',true) IS DISTINCT FROM 'on'
       OR (to_jsonb(NEW)-'compressed_payload'-'expired_at') <> (to_jsonb(OLD)-'compressed_payload'-'expired_at')
       OR OLD.expired_at IS NOT NULL OR NEW.expired_at IS NULL OR length(NEW.compressed_payload)<>0
       OR NOT EXISTS (SELECT 1 FROM observed_blocks b,retention_floors f
                      WHERE b.id=OLD.block_id AND f.name='evidence' AND b.height<=f.block_height)
       OR EXISTS (SELECT 1 FROM reservations WHERE selection->>'block_id'=OLD.block_id AND collateral_outcome IS NULL)
    THEN RAISE EXCEPTION 'compact evidence expires only under the guarded retention floor'; END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER retention BEFORE UPDATE OR DELETE ON compact_block_copies
    FOR EACH ROW EXECUTE FUNCTION compact_copy_guard();
CREATE TRIGGER prevent_truncate BEFORE TRUNCATE ON compact_block_copies
    FOR EACH STATEMENT EXECUTE FUNCTION immutable_record();

CREATE FUNCTION compact_capture_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE floors retention_floors%ROWTYPE; covered timestamptz; allowed boolean;
BEGIN
    IF TG_OP='DELETE' OR current_setting('pool_v2.retention',true) IS DISTINCT FROM 'on'
       OR (to_jsonb(NEW)-'manifest'-'metadata') <> (to_jsonb(OLD)-'manifest'-'metadata')
       OR NOT (NEW.manifest ? 'expired') OR OLD.manifest ? 'expired'
       OR NOT (NEW.metadata=OLD.metadata OR (NEW.metadata ? 'expired'
           AND jsonb_typeof(NEW.metadata)='object' AND (SELECT count(*) FROM jsonb_object_keys(NEW.metadata))=1))
    THEN RAISE EXCEPTION 'capture expiry cannot change immutable evidence identity'; END IF;
    SELECT * INTO floors FROM retention_floors WHERE name='evidence';
    allowed := (OLD.height IS NOT NULL AND OLD.height<=floors.block_height)
               OR (OLD.height IS NULL AND OLD.created_at<=floors.capture_cutoff);
    IF NOT allowed AND current_setting('pool_v2.compaction',true)='on' THEN
        covered := current_setting('pool_v2.compaction_covered_until')::timestamptz;
        SELECT EXISTS (
            SELECT 1 FROM observed_blocks b JOIN compact_block_copies c ON c.block_id=b.id
            JOIN credited_blocks credit ON credit.block_id=b.id
            JOIN reconciled_blocks r ON r.block_id=b.id
            WHERE b.attempt_id=OLD.id AND c.source_semantic_digest=b.semantic_digest
              AND c.expired_at IS NULL AND length(c.compressed_payload)>0
              AND OLD.created_at<=covered AND c.created_at<=covered
              AND NOT EXISTS (SELECT 1 FROM observation_alerts a
                  WHERE a.height=b.height AND a.kind='conflicting-block')) INTO allowed;
    END IF;
    IF NOT allowed THEN RAISE EXCEPTION 'raw capture still needs its evidence or a backed compact replacement'; END IF;
    RETURN NEW;
END $$;
DROP TRIGGER retention ON capture_attempts;
CREATE TRIGGER retention BEFORE UPDATE OR DELETE ON capture_attempts
    FOR EACH ROW EXECUTE FUNCTION compact_capture_guard();
