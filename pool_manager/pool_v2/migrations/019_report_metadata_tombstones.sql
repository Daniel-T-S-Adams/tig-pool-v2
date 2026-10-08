-- Expired report captures also shed their provenance metadata. The guard keeps
-- refusing every other change; inside a retention run an expiring report row
-- may now set metadata to a tombstone object holding only "expired", either
-- at first expiry or in a later pass over rows expired before this migration.
CREATE OR REPLACE FUNCTION retention_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE floors retention_floors%ROWTYPE; unchanged boolean; tomb boolean;
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
        unchanged := (to_jsonb(NEW) - 'compressed_payload' - 'expired_at' - 'metadata')
                     = (to_jsonb(OLD) - 'compressed_payload' - 'expired_at' - 'metadata');
        tomb := jsonb_typeof(NEW.metadata)='object' AND NEW.metadata ? 'expired'
                AND (SELECT count(*) FROM jsonb_object_keys(NEW.metadata)) = 1;
        IF NOT unchanged OR NEW.expired_at IS NULL OR length(NEW.compressed_payload) <> 0
           OR OLD.reporting_round > floors.reporting_round
           OR NOT (NEW.metadata = OLD.metadata OR tomb)
           OR (OLD.expired_at IS NOT NULL AND (NEW.expired_at <> OLD.expired_at OR NOT tomb OR OLD.metadata ? 'expired')) THEN
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
