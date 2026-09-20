ALTER TABLE reservations ADD COLUMN payload_text text;
ALTER TABLE reservations ADD COLUMN assignment_payload text;
CREATE FUNCTION protect_payload_bytes() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (OLD.payload_text IS NOT NULL AND NEW.payload_text IS DISTINCT FROM OLD.payload_text)
       OR (OLD.assignment_payload IS NOT NULL AND NEW.assignment_payload IS DISTINCT FROM OLD.assignment_payload)
    THEN RAISE EXCEPTION 'immutable serialized benchmark payload'; END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER reservation_bytes_guard BEFORE UPDATE ON reservations
    FOR EACH ROW EXECUTE FUNCTION protect_payload_bytes();
CREATE TABLE work_requests (
    id uuid PRIMARY KEY,
    member_id uuid NOT NULL REFERENCES members,
    request_key text NOT NULL,
    offer_hash text NOT NULL,
    offer jsonb NOT NULL,
    state text NOT NULL DEFAULT 'queued' CHECK (state IN ('queued','reserved','expired','cancelled')),
    expires_at timestamptz NOT NULL,
    reservation_id uuid UNIQUE REFERENCES reservations,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(member_id,request_key)
);
CREATE FUNCTION protect_work_request() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='DELETE' THEN RAISE EXCEPTION 'member work request cannot be deleted'; END IF;
    IF (NEW.id,NEW.member_id,NEW.request_key,NEW.offer_hash,NEW.offer,NEW.created_at)
       IS DISTINCT FROM (OLD.id,OLD.member_id,OLD.request_key,OLD.offer_hash,OLD.offer,OLD.created_at)
       OR (OLD.state<>'queued' AND NEW.state IS DISTINCT FROM OLD.state)
       OR (OLD.reservation_id IS NOT NULL AND NEW.reservation_id IS DISTINCT FROM OLD.reservation_id)
       OR (NEW.expires_at IS DISTINCT FROM OLD.expires_at AND
           (OLD.state<>'queued' OR NEW.state<>'queued' OR NEW.expires_at<OLD.expires_at))
    THEN RAISE EXCEPTION 'immutable member work offer or reservation'; END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER work_request_guard BEFORE UPDATE OR DELETE ON work_requests
    FOR EACH ROW EXECUTE FUNCTION protect_work_request();
CREATE INDEX work_request_queue ON work_requests(created_at,id) WHERE state='queued';
CREATE TABLE benchmark_payloads (
    benchmark_id text NOT NULL REFERENCES reservations(benchmark_id),
    kind text NOT NULL CHECK (kind IN ('results','proofs')),
    digest text NOT NULL,
    payload jsonb NOT NULL,
    payload_text text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(benchmark_id,kind)
);
CREATE TABLE benchmark_progress (
    benchmark_id text PRIMARY KEY REFERENCES reservations(benchmark_id),
    sampled_nonces jsonb NOT NULL,
    evidence jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE protocol_outbox (
    id uuid PRIMARY KEY,
    reservation_id uuid NOT NULL REFERENCES reservations,
    kind text NOT NULL CHECK (kind IN ('precommit','results','proofs')),
    payload jsonb NOT NULL,
    payload_text text NOT NULL,
    digest text NOT NULL,
    state text NOT NULL DEFAULT 'ready' CHECK (state IN ('ready','uncertain','accepted','rejected','cancelled')),
    sent_at timestamptz,
    evidence jsonb,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(reservation_id,kind)
);
-- Indistinguishable precommits cannot be simultaneously in flight. Lease
-- expiry never moves uncertain back to ready; reconcile external effects.
CREATE UNIQUE INDEX one_uncertain_precommit_per_payload ON protocol_outbox(digest)
    WHERE kind='precommit' AND state='uncertain';
CREATE TRIGGER benchmark_payloads_immutable BEFORE UPDATE OR DELETE ON benchmark_payloads
    FOR EACH ROW EXECUTE FUNCTION immutable_record();
CREATE TRIGGER benchmark_progress_immutable BEFORE UPDATE OR DELETE ON benchmark_progress
    FOR EACH ROW EXECUTE FUNCTION immutable_record();
CREATE FUNCTION protect_outbox() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='DELETE' THEN RAISE EXCEPTION 'protocol intent cannot be deleted'; END IF;
    IF (NEW.id,NEW.reservation_id,NEW.kind,NEW.payload,NEW.payload_text,NEW.digest,NEW.created_at)
       IS DISTINCT FROM (OLD.id,OLD.reservation_id,OLD.kind,OLD.payload,OLD.payload_text,OLD.digest,OLD.created_at)
       OR (OLD.state='uncertain' AND NEW.state='ready')
       OR (OLD.state IN ('accepted','rejected','cancelled') AND
           (NEW.state,NEW.evidence) IS DISTINCT FROM (OLD.state,OLD.evidence))
       OR (OLD.sent_at IS NOT NULL AND NEW.sent_at IS DISTINCT FROM OLD.sent_at)
    THEN RAISE EXCEPTION 'immutable submission intent or potentially sent marker'; END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER outbox_guard BEFORE UPDATE OR DELETE ON protocol_outbox
    FOR EACH ROW EXECUTE FUNCTION protect_outbox();
DO $$ DECLARE name text; BEGIN
    FOREACH name IN ARRAY ARRAY['work_requests','benchmark_payloads','benchmark_progress','protocol_outbox'] LOOP
        EXECUTE format('CREATE TRIGGER prevent_truncate BEFORE TRUNCATE ON %I '
                       'FOR EACH STATEMENT EXECUTE FUNCTION immutable_record()', name);
    END LOOP;
END $$;
