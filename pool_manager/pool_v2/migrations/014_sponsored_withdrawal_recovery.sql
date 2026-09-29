-- Preserve the real outer transaction; separately explain the custody nonce.
CREATE TABLE custody_authorization_payments (
    send_id uuid PRIMARY KEY REFERENCES custody_sends,
    chain_id bigint NOT NULL,
    tx_hash text NOT NULL,
    authority text NOT NULL,
    nonce numeric NOT NULL CHECK (nonce >= 0 AND nonce = trunc(nonce)),
    delegate text NOT NULL,
    actor text NOT NULL,
    reason text NOT NULL,
    evidence jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(chain_id,tx_hash),
    UNIQUE(chain_id,authority,nonce),
    FOREIGN KEY(chain_id,tx_hash) REFERENCES chain_transactions
);
CREATE TRIGGER immutable BEFORE UPDATE OR DELETE ON custody_authorization_payments
    FOR EACH ROW EXECUTE FUNCTION immutable_record();
CREATE TRIGGER prevent_truncate BEFORE TRUNCATE ON custody_authorization_payments
    FOR EACH STATEMENT EXECUTE FUNCTION immutable_record();
ALTER TABLE custody_checks ADD COLUMN custody_code text;
