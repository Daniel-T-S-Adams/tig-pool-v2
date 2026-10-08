-- Bounded-life evidence expires under a recorded retention policy. Ledger,
-- credit, benchmark and settlement records are untouched by this migration.
-- Expiry keeps every row (foreign keys stay valid) and blanks only payloads;
-- the guard below refuses any other change, and only inside a retention run.

CREATE TABLE retention_floors (
    name text PRIMARY KEY CHECK (name='evidence'),
    block_height bigint NOT NULL CHECK (block_height >= -1),
    reporting_round integer NOT NULL CHECK (reporting_round >= 0),
    capture_cutoff timestamptz NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
INSERT INTO retention_floors(name,block_height,reporting_round,capture_cutoff)
    VALUES ('evidence',-1,0,'1970-01-01T00:00:00Z');
CREATE FUNCTION protect_retention_floors() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='DELETE' THEN RAISE EXCEPTION 'retention floors cannot be removed'; END IF;
    IF current_setting('pool_v2.retention', true) IS DISTINCT FROM 'on' THEN
        RAISE EXCEPTION 'retention floors change only inside a retention run';
    END IF;
    IF NEW.name<>OLD.name OR NEW.block_height<OLD.block_height OR NEW.reporting_round<OLD.reporting_round
       OR NEW.capture_cutoff<OLD.capture_cutoff THEN
        RAISE EXCEPTION 'retention floors only advance';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER floors_guard BEFORE UPDATE OR DELETE ON retention_floors
    FOR EACH ROW EXECUTE FUNCTION protect_retention_floors();
CREATE TRIGGER prevent_truncate BEFORE TRUNCATE ON retention_floors
    FOR EACH STATEMENT EXECUTE FUNCTION immutable_record();

CREATE TABLE retention_runs (
    id uuid PRIMARY KEY,
    policy jsonb NOT NULL,
    backup_proof jsonb NOT NULL,
    floors jsonb NOT NULL,
    counts jsonb NOT NULL,
    notes jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TRIGGER immutable BEFORE UPDATE OR DELETE ON retention_runs
    FOR EACH ROW EXECUTE FUNCTION immutable_record();
CREATE TRIGGER prevent_truncate BEFORE TRUNCATE ON retention_runs
    FOR EACH STATEMENT EXECUTE FUNCTION immutable_record();

-- The last block height that referenced a chunk no longer referenced by the
-- newest block. The block observer maintains it; a chunk with a row at or
-- below the block floor is referenced only by expired captures.
CREATE TABLE chunk_last_ref (
    digest text PRIMARY KEY CHECK (length(digest) = 64),
    height bigint NOT NULL CHECK (height >= 0)
);
CREATE INDEX chunk_last_ref_by_height ON chunk_last_ref(height);

ALTER TABLE report_captures ADD COLUMN expired_at timestamptz;
ALTER TABLE report_index_captures ADD COLUMN expired_at timestamptz;
ALTER TABLE funding_captures ADD COLUMN expired_at timestamptz;
ALTER TABLE chain_captures ADD COLUMN expired_at timestamptz;

CREATE FUNCTION retention_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE floors retention_floors%ROWTYPE; unchanged boolean;
BEGIN
    IF current_setting('pool_v2.retention', true) IS DISTINCT FROM 'on' THEN
        RAISE EXCEPTION 'append-only record: %', TG_TABLE_NAME;
    END IF;
    SELECT * INTO floors FROM retention_floors WHERE name='evidence';
    IF TG_TABLE_NAME='observation_chunks' THEN
        IF TG_OP='DELETE' THEN RETURN OLD; END IF;
        RAISE EXCEPTION 'observation chunks are removed by retention, never edited';
    END IF;
    IF TG_OP='DELETE' THEN
        RAISE EXCEPTION 'evidence rows are kept; only their payload expires: %', TG_TABLE_NAME;
    END IF;
    IF TG_TABLE_NAME='capture_attempts' THEN
        unchanged := (to_jsonb(NEW) - 'manifest') = (to_jsonb(OLD) - 'manifest');
        IF NOT unchanged OR NOT (NEW.manifest ? 'expired') OR (OLD.manifest ? 'expired')
           OR NOT ((OLD.height IS NOT NULL AND OLD.height <= floors.block_height)
                   OR (OLD.height IS NULL AND OLD.created_at <= floors.capture_cutoff)) THEN
            RAISE EXCEPTION 'capture manifests expire only under the recorded retention floor';
        END IF;
        RETURN NEW;
    END IF;
    IF TG_TABLE_NAME IN ('report_captures','report_index_captures') THEN
        unchanged := (to_jsonb(NEW) - 'compressed_payload' - 'expired_at') = (to_jsonb(OLD) - 'compressed_payload' - 'expired_at');
        IF NOT unchanged OR OLD.expired_at IS NOT NULL OR NEW.expired_at IS NULL
           OR length(NEW.compressed_payload) <> 0 OR OLD.reporting_round > floors.reporting_round THEN
            RAISE EXCEPTION 'report captures expire only under the recorded retention floor';
        END IF;
        RETURN NEW;
    END IF;
    IF TG_TABLE_NAME IN ('funding_captures','chain_captures') THEN
        unchanged := (to_jsonb(NEW) - 'payload_gzip' - 'expired_at') = (to_jsonb(OLD) - 'payload_gzip' - 'expired_at');
        IF NOT unchanged OR OLD.expired_at IS NOT NULL OR NEW.expired_at IS NULL
           OR length(NEW.payload_gzip) <> 0 OR OLD.created_at > floors.capture_cutoff THEN
            RAISE EXCEPTION 'captures expire only under the recorded retention cutoff';
        END IF;
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'append-only record: %', TG_TABLE_NAME;
END $$;
DO $$ DECLARE name text; BEGIN
    FOREACH name IN ARRAY ARRAY['observation_chunks','capture_attempts','report_captures',
        'report_index_captures','funding_captures','chain_captures'] LOOP
        EXECUTE format('DROP TRIGGER immutable ON %I', name);
        EXECUTE format('CREATE TRIGGER retention BEFORE UPDATE OR DELETE ON %I '
                       'FOR EACH ROW EXECUTE FUNCTION retention_guard()', name);
    END LOOP;
END $$;
