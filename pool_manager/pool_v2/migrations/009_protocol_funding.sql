-- One custody nonce namespace is shared by withdrawals and operator funding.
CREATE TABLE custody_sends (
    id uuid PRIMARY KEY,
    chain_id bigint NOT NULL,
    sender text NOT NULL,
    nonce numeric NOT NULL CHECK (nonce>=0 AND nonce=trunc(nonce)),
    kind text NOT NULL CHECK (kind IN ('withdrawal','protocol_topup')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(chain_id,sender,nonce)
);
INSERT INTO custody_sends(id,chain_id,sender,nonce,kind,created_at)
    SELECT id,chain_id,sender,nonce,'withdrawal',sent_at FROM withdrawal_attempts;
ALTER TABLE withdrawal_attempts ADD CONSTRAINT shared_custody_nonce FOREIGN KEY(id) REFERENCES custody_sends;
CREATE TABLE custody_payments (
    send_id uuid PRIMARY KEY REFERENCES custody_sends,
    chain_id bigint NOT NULL,
    tx_hash text NOT NULL,
    transfer_event text UNIQUE REFERENCES transfers,
    fee numeric NOT NULL CHECK (fee>=0 AND fee=trunc(fee)),
    journal_id uuid NOT NULL REFERENCES journals,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(chain_id,tx_hash),
    FOREIGN KEY(chain_id,tx_hash) REFERENCES chain_transactions(chain_id,tx_hash)
);
INSERT INTO custody_payments(send_id,chain_id,tx_hash,transfer_event,fee,journal_id)
    SELECT o.attempt_id,o.chain_id,o.tx_hash,CASE WHEN o.outcome='paid' THEN w.paid_event ELSE NULL END,o.fee,o.journal_id
    FROM withdrawal_attempt_outcomes o JOIN withdrawal_attempts a ON a.id=o.attempt_id
    JOIN withdrawals w ON w.id=a.withdrawal_id;
CREATE TABLE protocol_identity (
    name text PRIMARY KEY CHECK (name='fees'),
    player_id text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE funding_captures (
    id text PRIMARY KEY,
    payload_gzip bytea NOT NULL,
    player_id text NOT NULL,
    block_id text,
    height bigint,
    available numeric,
    checked_at timestamptz NOT NULL,
    complete boolean NOT NULL,
    error text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE protocol_topups (
    id uuid PRIMARY KEY REFERENCES custody_sends,
    request_key text NOT NULL UNIQUE,
    amount numeric NOT NULL CHECK (amount>0 AND amount=trunc(amount)),
    fee_limit numeric NOT NULL CHECK (fee_limit>0 AND fee_limit=trunc(fee_limit)),
    network jsonb NOT NULL,
    recipient text NOT NULL,
    fee_model text NOT NULL,
    policy_capture text NOT NULL REFERENCES funding_captures,
    preflight jsonb NOT NULL,
    actor text NOT NULL,
    state text NOT NULL CHECK (state IN ('uncertain','awaiting_protocol','credited','failed','cancelled')),
    sent_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE protocol_topup_facts (
    topup_id text PRIMARY KEY,
    player_id text NOT NULL,
    tx_hash text NOT NULL,
    log_index bigint NOT NULL,
    facts jsonb NOT NULL,
    capture_id text NOT NULL REFERENCES funding_captures,
    UNIQUE(player_id,tx_hash,log_index)
);
CREATE TABLE funding_alerts (
    capture_id text PRIMARY KEY REFERENCES funding_captures,
    details jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE protocol_topup_credits (
    send_id uuid PRIMARY KEY REFERENCES protocol_topups,
    topup_id text NOT NULL UNIQUE,
    capture_id text NOT NULL REFERENCES funding_captures,
    transfer_event text NOT NULL UNIQUE REFERENCES transfers,
    journal_id uuid NOT NULL REFERENCES journals,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE topup_transaction_claims (
    send_id uuid NOT NULL REFERENCES protocol_topups,
    tx_hash text NOT NULL,
    actor text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(send_id,tx_hash)
);
CREATE FUNCTION protect_topup() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='DELETE' THEN RAISE EXCEPTION 'top-up cannot be deleted'; END IF;
    IF (NEW.id,NEW.request_key,NEW.amount,NEW.fee_limit,NEW.network,NEW.recipient,NEW.fee_model,NEW.policy_capture,NEW.preflight,NEW.actor,NEW.sent_at)
        IS DISTINCT FROM
       (OLD.id,OLD.request_key,OLD.amount,OLD.fee_limit,OLD.network,OLD.recipient,OLD.fee_model,OLD.policy_capture,OLD.preflight,OLD.actor,OLD.sent_at)
       OR (OLD.state IN ('credited','failed','cancelled') AND NEW.state<>OLD.state)
    THEN RAISE EXCEPTION 'immutable top-up route or final outcome'; END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER topup_guard BEFORE UPDATE OR DELETE ON protocol_topups FOR EACH ROW EXECUTE FUNCTION protect_topup();
CREATE TRIGGER prevent_truncate BEFORE TRUNCATE ON protocol_topups FOR EACH STATEMENT EXECUTE FUNCTION immutable_record();
DO $$ DECLARE item text; BEGIN
    FOREACH item IN ARRAY ARRAY['custody_sends','custody_payments','protocol_identity','funding_captures','protocol_topup_facts','funding_alerts','protocol_topup_credits','topup_transaction_claims'] LOOP
        EXECUTE format('CREATE TRIGGER immutable BEFORE UPDATE OR DELETE ON %I FOR EACH ROW EXECUTE FUNCTION immutable_record()',item);
        EXECUTE format('CREATE TRIGGER prevent_truncate BEFORE TRUNCATE ON %I FOR EACH STATEMENT EXECUTE FUNCTION immutable_record()',item);
    END LOOP;
END $$;
