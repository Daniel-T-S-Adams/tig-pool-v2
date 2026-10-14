-- Alerts and original block identities remain immutable. Each proven list
-- permutation is resolved individually; a later conflict still blocks use.
CREATE TABLE observation_conflict_resolutions (
    alert_id bigint PRIMARY KEY REFERENCES observation_alerts,
    block_id text NOT NULL REFERENCES observed_blocks,
    original_attempt_id uuid NOT NULL REFERENCES capture_attempts,
    conflicting_attempt_id uuid NOT NULL REFERENCES capture_attempts,
    policy text NOT NULL CHECK (policy='legacy-compact-record-order-v1'),
    actor text NOT NULL CHECK (length(trim(actor))>0),
    reason text NOT NULL CHECK (length(trim(reason))>0),
    proof jsonb NOT NULL CHECK (proof ?& ARRAY['policy','block_id','height','complete_network_replay_matches',
        'changed_paths','legacy_semantic_digest','replayed_source_semantic_digest','legacy_payload_sha256',
        'compact_payload_sha256','replayed_network_sha256'] AND proof->>'policy'=policy
        AND proof->>'block_id'=block_id AND proof->>'complete_network_replay_matches'='true'
        AND jsonb_typeof(proof->'changed_paths')='array' AND jsonb_array_length(proof->'changed_paths')>0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE FUNCTION ordering_resolution_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM observation_alerts a
        JOIN observed_blocks b ON b.height=a.height
        JOIN capture_attempts c ON c.id=NEW.conflicting_attempt_id
        WHERE a.id=NEW.alert_id AND a.kind='conflicting-block'
          AND a.details=jsonb_build_object('block_id',b.id,'collector',c.collector)
          AND b.id=NEW.block_id AND b.attempt_id=NEW.original_attempt_id
          AND c.block_id=b.id AND c.height=b.height
          AND NEW.proof->>'legacy_semantic_digest'=b.semantic_digest
          AND (NEW.proof->>'height')::bigint=b.height
          AND c.error='conflicting block identity or accounting evidence at a captured height')
    THEN RAISE EXCEPTION 'ordering resolution must bind the original block and conflicting capture'; END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER validate_resolution BEFORE INSERT ON observation_conflict_resolutions
    FOR EACH ROW EXECUTE FUNCTION ordering_resolution_guard();
CREATE TRIGGER immutable BEFORE UPDATE OR DELETE ON observation_conflict_resolutions
    FOR EACH ROW EXECUTE FUNCTION immutable_record();
CREATE TRIGGER prevent_truncate BEFORE TRUNCATE ON observation_conflict_resolutions
    FOR EACH STATEMENT EXECUTE FUNCTION immutable_record();
CREATE VIEW unresolved_observation_conflicts WITH (security_invoker=true) AS
    SELECT a.* FROM observation_alerts a
    WHERE a.kind='conflicting-block' AND NOT EXISTS
        (SELECT 1 FROM observation_conflict_resolutions r WHERE r.alert_id=a.id);

CREATE OR REPLACE FUNCTION compact_capture_guard() RETURNS trigger LANGUAGE plpgsql AS $$
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
              AND NOT EXISTS (SELECT 1 FROM unresolved_observation_conflicts a
                  WHERE a.height=b.height AND a.kind='conflicting-block')
              AND NOT EXISTS (SELECT 1 FROM observation_conflict_resolutions r JOIN observation_alerts a ON a.id=r.alert_id
                  WHERE a.height=b.height AND r.created_at>covered)) INTO allowed;
    END IF;
    IF NOT allowed THEN RAISE EXCEPTION 'raw capture still needs its evidence or a backed compact replacement'; END IF;
    RETURN NEW;
END $$;
