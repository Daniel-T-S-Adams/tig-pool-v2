ALTER TABLE protocol_outbox ADD COLUMN match_key text;
ALTER TABLE protocol_outbox ADD COLUMN preflight jsonb;
ALTER TABLE work_requests ADD COLUMN last_attempted_at timestamptz;
CREATE UNIQUE INDEX one_ambiguous_precommit_per_settings ON protocol_outbox(match_key)
    WHERE kind='precommit' AND state='uncertain';
CREATE FUNCTION protect_submission_fence() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (OLD.match_key IS NOT NULL AND NEW.match_key IS DISTINCT FROM OLD.match_key)
       OR (OLD.preflight IS NOT NULL AND NEW.preflight IS DISTINCT FROM OLD.preflight)
    THEN RAISE EXCEPTION 'immutable submission fence and preflight'; END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER submission_fence_guard BEFORE UPDATE ON protocol_outbox
    FOR EACH ROW EXECUTE FUNCTION protect_submission_fence();
CREATE TABLE submission_responses (
    id uuid PRIMARY KEY,
    outbox_id uuid NOT NULL REFERENCES protocol_outbox,
    response jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
-- Save a positive identity before fetching metadata. The process can stop at
-- any point after the POST without losing the accepted work's immutable owner.
CREATE TABLE precommit_receipts (
    reservation_id uuid PRIMARY KEY REFERENCES reservations,
    benchmark_id text NOT NULL UNIQUE,
    evidence jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE algorithm_archives (
    sha256 text PRIMARY KEY,
    archive bytea NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE reservation_archives (
    reservation_id uuid PRIMARY KEY REFERENCES reservations,
    sha256 text NOT NULL REFERENCES algorithm_archives,
    source_url text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE reconciled_blocks (
    block_id text PRIMARY KEY REFERENCES observed_blocks(id),
    player_id text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE reconciliation_queue (
    block_id text PRIMARY KEY REFERENCES observed_blocks(id),
    last_attempted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    last_error text NOT NULL
);
DO $$ DECLARE name text; BEGIN
    FOREACH name IN ARRAY ARRAY['submission_responses','precommit_receipts','algorithm_archives','reservation_archives','reconciled_blocks'] LOOP
        EXECUTE format('CREATE TRIGGER immutable BEFORE UPDATE OR DELETE ON %I '
                       'FOR EACH ROW EXECUTE FUNCTION immutable_record()', name);
        EXECUTE format('CREATE TRIGGER prevent_truncate BEFORE TRUNCATE ON %I '
                       'FOR EACH STATEMENT EXECUTE FUNCTION immutable_record()', name);
    END LOOP;
END $$;
