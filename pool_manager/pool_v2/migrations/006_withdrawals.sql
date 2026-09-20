-- Preserve even zero-valued ERC-20 events without creating monetary entries.
ALTER TABLE transfers DROP CONSTRAINT transfers_amount_check;
ALTER TABLE transfers ADD CHECK (amount>=0 AND amount=trunc(amount));

CREATE TABLE custody_identity (
    name text PRIMARY KEY CHECK (name='custody'),
    chain_id bigint NOT NULL CHECK (chain_id>0),
    token text NOT NULL,
    wallet text NOT NULL,
    decimals integer NOT NULL CHECK (decimals=18),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE chain_transactions (
    chain_id bigint NOT NULL,
    tx_hash text NOT NULL,
    sender text NOT NULL,
    recipient text,
    nonce numeric NOT NULL CHECK (nonce>=0 AND nonce=trunc(nonce)),
    successful boolean NOT NULL,
    value numeric NOT NULL CHECK (value>=0 AND value=trunc(value)),
    fee numeric NOT NULL CHECK (fee>=0 AND fee=trunc(fee)),
    fee_model text NOT NULL,
    block_number bigint NOT NULL,
    block_hash text NOT NULL,
    block_timestamp timestamptz NOT NULL,
    evidence jsonb NOT NULL,
    PRIMARY KEY(chain_id,tx_hash),
    UNIQUE(chain_id,sender,nonce)
);
CREATE TABLE withdrawal_reviews (
    withdrawal_id uuid PRIMARY KEY REFERENCES withdrawals,
    chain_id bigint NOT NULL,
    token text NOT NULL,
    sender text NOT NULL,
    fee_model text NOT NULL,
    actor text NOT NULL,
    confirmations integer NOT NULL CHECK (confirmations>0),
    evidence jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE withdrawal_attempts (
    id uuid PRIMARY KEY,
    withdrawal_id uuid NOT NULL REFERENCES withdrawals,
    request_key text NOT NULL,
    chain_id bigint NOT NULL,
    sender text NOT NULL,
    nonce numeric NOT NULL CHECK (nonce>=0 AND nonce=trunc(nonce)),
    fee_limit numeric NOT NULL CHECK (fee_limit>0 AND fee_limit=trunc(fee_limit)),
    actor text NOT NULL,
    preflight jsonb NOT NULL,
    sent_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(withdrawal_id,request_key),
    UNIQUE(chain_id,sender,nonce)
);
CREATE TABLE withdrawal_transaction_claims (
    chain_id bigint NOT NULL,
    tx_hash text NOT NULL,
    attempt_id uuid NOT NULL REFERENCES withdrawal_attempts,
    actor text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(attempt_id,chain_id,tx_hash)
);
CREATE TABLE withdrawal_attempt_outcomes (
    attempt_id uuid PRIMARY KEY REFERENCES withdrawal_attempts,
    chain_id bigint NOT NULL,
    tx_hash text NOT NULL,
    outcome text NOT NULL CHECK (outcome IN ('paid','failed','cancelled')),
    fee numeric NOT NULL CHECK (fee>=0 AND fee=trunc(fee)),
    journal_id uuid NOT NULL REFERENCES journals,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY(chain_id,tx_hash) REFERENCES chain_transactions,
    UNIQUE(chain_id,tx_hash)
);
CREATE TABLE withdrawal_events (
    event_key text PRIMARY KEY,
    withdrawal_id uuid NOT NULL REFERENCES withdrawals,
    kind text NOT NULL,
    actor text NOT NULL,
    details jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE withdrawal_wallet_challenges (
    id uuid PRIMARY KEY,
    member_id uuid NOT NULL REFERENCES members,
    old_wallet text NOT NULL,
    new_wallet text NOT NULL,
    message text NOT NULL,
    expires_at timestamptz NOT NULL,
    used_at timestamptz
);
CREATE TABLE withdrawal_wallet_changes (
    challenge_id uuid PRIMARY KEY REFERENCES withdrawal_wallet_challenges,
    member_id uuid NOT NULL REFERENCES members,
    old_wallet text NOT NULL,
    new_wallet text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE FUNCTION protect_withdrawal_wallet_challenge() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP <> 'UPDATE' THEN RAISE EXCEPTION 'wallet challenge cannot be removed'; END IF;
    IF (NEW.id,NEW.member_id,NEW.old_wallet,NEW.new_wallet,NEW.message,NEW.expires_at) IS DISTINCT FROM
       (OLD.id,OLD.member_id,OLD.old_wallet,OLD.new_wallet,OLD.message,OLD.expires_at)
       OR OLD.used_at IS NOT NULL OR NEW.used_at IS NULL
    THEN RAISE EXCEPTION 'wallet challenge can only be consumed once'; END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER wallet_challenge_guard BEFORE UPDATE OR DELETE ON withdrawal_wallet_challenges
    FOR EACH ROW EXECUTE FUNCTION protect_withdrawal_wallet_challenge();
CREATE TRIGGER wallet_challenge_truncate BEFORE TRUNCATE ON withdrawal_wallet_challenges
    FOR EACH STATEMENT EXECUTE FUNCTION immutable_record();
DO $$ DECLARE name text; BEGIN
    FOREACH name IN ARRAY ARRAY['custody_identity','chain_transactions','withdrawal_reviews','withdrawal_attempts',
        'withdrawal_transaction_claims','withdrawal_attempt_outcomes','withdrawal_events','withdrawal_wallet_changes'] LOOP
        EXECUTE format('CREATE TRIGGER immutable BEFORE UPDATE OR DELETE ON %I '
                       'FOR EACH ROW EXECUTE FUNCTION immutable_record()',name);
        EXECUTE format('CREATE TRIGGER prevent_truncate BEFORE TRUNCATE ON %I '
                       'FOR EACH STATEMENT EXECUTE FUNCTION immutable_record()',name);
    END LOOP;
END $$;
