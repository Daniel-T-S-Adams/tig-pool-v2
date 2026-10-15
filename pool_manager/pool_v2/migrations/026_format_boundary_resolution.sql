-- A pool-scoped capture of a block that was already recorded from a compact
-- full-network capture is an alternative projection of the same block, not
-- new evidence. It may be resolved only with a proof that every
-- credit-bearing input agrees. Alerts and block identities stay immutable.
DO $$ DECLARE constraint_name text; BEGIN
    FOR constraint_name IN SELECT conname FROM pg_constraint
        WHERE conrelid='observation_conflict_resolutions'::regclass AND contype='c'
          AND (pg_get_constraintdef(oid) LIKE '%policy%' OR pg_get_constraintdef(oid) LIKE '%proof%')
    LOOP EXECUTE format('ALTER TABLE observation_conflict_resolutions DROP CONSTRAINT %I', constraint_name); END LOOP;
END $$;
ALTER TABLE observation_conflict_resolutions
    ADD CONSTRAINT observation_conflict_resolutions_policy_check
        CHECK (policy IN ('legacy-compact-record-order-v1','format-boundary-projection-v1')),
    ADD CONSTRAINT observation_conflict_resolutions_proof_check CHECK (
        proof->>'policy'=policy AND proof->>'block_id'=block_id AND (
            (policy='legacy-compact-record-order-v1'
             AND proof ?& ARRAY['policy','block_id','height','complete_network_replay_matches','changed_paths',
                 'legacy_semantic_digest','replayed_source_semantic_digest','legacy_payload_sha256',
                 'compact_payload_sha256','replayed_network_sha256']
             AND proof->>'complete_network_replay_matches'='true'
             AND jsonb_typeof(proof->'changed_paths')='array' AND jsonb_array_length(proof->'changed_paths')>0)
         OR (policy='format-boundary-projection-v1'
             AND proof ?& ARRAY['policy','block_id','height','original_format','conflicting_format',
                 'original_semantic_digest','conflicting_semantic_digest','original_payload_sha256',
                 'conflicting_payload_sha256','compared','pool_credit_matches']
             AND proof->>'original_format'='pool-network-v1' AND proof->>'conflicting_format'='pool-scoped-v2'
             AND proof->>'pool_credit_matches'='true'
             AND jsonb_typeof(proof->'compared')='array' AND jsonb_array_length(proof->'compared')>0)));
CREATE OR REPLACE FUNCTION ordering_resolution_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM observation_alerts a
        JOIN observed_blocks b ON b.height=a.height
        JOIN capture_attempts c ON c.id=NEW.conflicting_attempt_id
        WHERE a.id=NEW.alert_id AND a.kind='conflicting-block'
          AND a.details=jsonb_build_object('block_id',b.id,'collector',c.collector)
          AND b.id=NEW.block_id AND b.attempt_id=NEW.original_attempt_id
          AND c.block_id=b.id AND c.height=b.height
          AND CASE NEW.policy WHEN 'legacy-compact-record-order-v1'
                  THEN NEW.proof->>'legacy_semantic_digest'=b.semantic_digest
                  ELSE NEW.proof->>'original_semantic_digest'=b.semantic_digest END
          AND (NEW.proof->>'height')::bigint=b.height
          AND c.error='conflicting block identity or accounting evidence at a captured height')
    THEN RAISE EXCEPTION 'conflict resolution must bind the original block and conflicting capture'; END IF;
    RETURN NEW;
END $$;
