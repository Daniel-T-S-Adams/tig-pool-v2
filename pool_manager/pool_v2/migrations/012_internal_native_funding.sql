-- Exact internal CALL identities are separate from their outer transaction.
CREATE TABLE native_internal_receipts (
    chain_id bigint NOT NULL,
    tx_hash text NOT NULL,
    trace_address integer[] NOT NULL CHECK (cardinality(trace_address) BETWEEN 1 AND 64),
    CHECK (array_ndims(trace_address)=1 AND array_lower(trace_address,1)=1
           AND array_position(trace_address,NULL) IS NULL AND 0<=ALL(trace_address)),
    sender text NOT NULL,
    recipient text NOT NULL,
    amount numeric NOT NULL CHECK (amount>0 AND amount=trunc(amount)),
    actor text NOT NULL CHECK (length(actor)>0),
    evidence jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(chain_id,tx_hash,trace_address),
    FOREIGN KEY(chain_id,tx_hash) REFERENCES chain_transactions
);
CREATE TRIGGER immutable BEFORE UPDATE OR DELETE ON native_internal_receipts
    FOR EACH ROW EXECUTE FUNCTION immutable_record();
CREATE TRIGGER prevent_truncate BEFORE TRUNCATE ON native_internal_receipts
    FOR EACH STATEMENT EXECUTE FUNCTION immutable_record();
