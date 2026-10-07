-- Record an explicit, verified start state when a prelaunch wallet has already
-- been funded or used. This is an operator opening entry, not an inferred pool
-- deposit. New custody observation begins at the next block and nonce.
ALTER TABLE chain_stream ADD COLUMN start_nonce bigint NOT NULL DEFAULT 0 CHECK (start_nonce>=0);

CREATE TABLE custody_opening_baselines (
    name text PRIMARY KEY CHECK (name='custody'),
    capture_id text NOT NULL UNIQUE REFERENCES chain_captures,
    network jsonb NOT NULL,
    height bigint NOT NULL CHECK (height>=0),
    block_hash text NOT NULL CHECK (block_hash ~ '^0x[0-9a-f]{64}$'),
    tig numeric NOT NULL CHECK (tig>=0 AND tig=trunc(tig)),
    native numeric NOT NULL CHECK (native>=0 AND native=trunc(native)),
    start_nonce bigint NOT NULL CHECK (start_nonce>=0),
    actor text NOT NULL CHECK (length(btrim(actor))>0),
    reason text NOT NULL CHECK (length(btrim(reason))>0),
    journal_ids uuid[] NOT NULL DEFAULT ARRAY[]::uuid[],
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TRIGGER immutable BEFORE UPDATE OR DELETE ON custody_opening_baselines
    FOR EACH ROW EXECUTE FUNCTION immutable_record();
CREATE TRIGGER prevent_truncate BEFORE TRUNCATE ON custody_opening_baselines
    FOR EACH STATEMENT EXECUTE FUNCTION immutable_record();

CREATE TABLE mainnet_protocol_opening_credits (
    topup_id text PRIMARY KEY,
    player_id text NOT NULL,
    capture_id text NOT NULL REFERENCES funding_captures,
    tx_hash text NOT NULL,
    log_index bigint NOT NULL CHECK (log_index>=0),
    amount numeric NOT NULL CHECK (amount>0 AND amount=trunc(amount)),
    base_block bigint NOT NULL CHECK (base_block>=0),
    base_hash text NOT NULL CHECK (base_hash ~ '^0x[0-9a-f]{64}$'),
    actor text NOT NULL CHECK (length(btrim(actor))>0),
    reason text NOT NULL CHECK (length(btrim(reason))>0),
    evidence jsonb NOT NULL CHECK (jsonb_typeof(evidence)='object'),
    journal_id uuid NOT NULL UNIQUE REFERENCES journals,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(tx_hash,log_index)
);
CREATE TRIGGER immutable BEFORE UPDATE OR DELETE ON mainnet_protocol_opening_credits
    FOR EACH ROW EXECUTE FUNCTION immutable_record();
CREATE TRIGGER prevent_truncate BEFORE TRUNCATE ON mainnet_protocol_opening_credits
    FOR EACH STATEMENT EXECUTE FUNCTION immutable_record();

CREATE OR REPLACE FUNCTION protect_chain_stream() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='DELETE' THEN RAISE EXCEPTION 'custody observation cannot be disabled by deleting its cursor'; END IF;
    IF (NEW.name,NEW.network,NEW.start_height,NEW.start_nonce,NEW.created_at) IS DISTINCT FROM
       (OLD.name,OLD.network,OLD.start_height,OLD.start_nonce,OLD.created_at) OR NEW.last_height<OLD.last_height
       OR (NEW.last_height=OLD.last_height AND NEW.last_hash<>OLD.last_hash)
    THEN RAISE EXCEPTION 'immutable custody observation identity or backwards cursor'; END IF;
    RETURN NEW;
END $$;
