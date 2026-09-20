-- New schema only. This migration does not read or mutate legacy tables.
CREATE TABLE assets (
    id text PRIMARY KEY,
    decimals integer NOT NULL CHECK (decimals BETWEEN 0 AND 36)
);
INSERT INTO assets VALUES ('TIG', 18), ('NATIVE', 18);

CREATE TABLE members (
    id uuid PRIMARY KEY,
    wallet text NOT NULL UNIQUE CHECK (wallet ~ '^0x[0-9a-f]{40}$'),
    withdrawal_wallet text NOT NULL CHECK (withdrawal_wallet ~ '^0x[0-9a-f]{40}$'),
    multiplier numeric NOT NULL DEFAULT 1 CHECK (multiplier BETWEEN 0 AND 1),
    multiplier_revision bigint NOT NULL DEFAULT 0 CHECK (multiplier_revision >= 0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    last_paid_at timestamptz
);
CREATE TABLE multiplier_changes (
    member_id uuid NOT NULL REFERENCES members,
    revision bigint NOT NULL,
    old_value numeric NOT NULL CHECK (old_value BETWEEN 0 AND 1),
    new_value numeric NOT NULL CHECK (new_value BETWEEN 0 AND 1),
    actor text NOT NULL,
    reason text NOT NULL,
    event_key text NOT NULL UNIQUE,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(member_id, revision)
);

CREATE TABLE accounts (
    id text PRIMARY KEY,
    asset text NOT NULL REFERENCES assets,
    kind text NOT NULL CHECK (kind IN ('member', 'collateral', 'withdrawal',
        'operator', 'operator_commitment', 'round', 'unattributed', 'external')),
    location text NOT NULL DEFAULT 'custody' CHECK (location IN ('custody', 'protocol')),
    balance numeric NOT NULL DEFAULT 0 CHECK (balance = trunc(balance)),
    CHECK (kind = 'external' OR balance >= 0),
    UNIQUE(id, asset)
);
CREATE TABLE journals (
    id uuid PRIMARY KEY,
    event_key text NOT NULL UNIQUE,
    kind text NOT NULL,
    fingerprint text NOT NULL,
    details jsonb NOT NULL,
    reverses uuid UNIQUE REFERENCES journals,
    created_xid bigint NOT NULL DEFAULT txid_current(),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE entries (
    journal_id uuid NOT NULL REFERENCES journals,
    account_id text NOT NULL,
    asset text NOT NULL,
    amount numeric NOT NULL CHECK (amount <> 0 AND amount = trunc(amount)),
    FOREIGN KEY(account_id, asset) REFERENCES accounts(id, asset),
    PRIMARY KEY(journal_id, account_id)
);
CREATE INDEX entries_by_account ON entries(account_id);

CREATE FUNCTION immutable_record() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'append-only record: %', TG_TABLE_NAME;
END $$;
CREATE TRIGGER journals_immutable BEFORE UPDATE OR DELETE ON journals
    FOR EACH ROW EXECUTE FUNCTION immutable_record();
CREATE TRIGGER entries_immutable BEFORE UPDATE OR DELETE ON entries
    FOR EACH ROW EXECUTE FUNCTION immutable_record();
CREATE TRIGGER multiplier_changes_immutable BEFORE UPDATE OR DELETE ON multiplier_changes
    FOR EACH ROW EXECUTE FUNCTION immutable_record();

CREATE FUNCTION protect_account() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.balance <> 0 THEN RAISE EXCEPTION 'account must start at zero'; END IF;
        RETURN NEW;
    END IF;
    IF TG_OP = 'DELETE' OR NEW.id <> OLD.id OR NEW.asset <> OLD.asset OR NEW.kind <> OLD.kind
       OR NEW.location <> OLD.location
       OR pg_trigger_depth() < 2 THEN
        RAISE EXCEPTION 'accounts may change only through journal entries';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER accounts_guard BEFORE INSERT OR UPDATE OR DELETE ON accounts
    FOR EACH ROW EXECUTE FUNCTION protect_account();

CREATE FUNCTION apply_entry() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM journals WHERE id = NEW.journal_id
                   AND created_xid = txid_current()) THEN
        RAISE EXCEPTION 'cannot extend a committed journal';
    END IF;
    UPDATE accounts SET balance = balance + NEW.amount
        WHERE id = NEW.account_id AND asset = NEW.asset;
    IF NOT FOUND THEN RAISE EXCEPTION 'unknown account or asset'; END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER apply_entry AFTER INSERT ON entries
    FOR EACH ROW EXECUTE FUNCTION apply_entry();

CREATE FUNCTION balanced_journal() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE identity uuid;
BEGIN
    IF TG_TABLE_NAME = 'journals' THEN identity := NEW.id;
    ELSE identity := NEW.journal_id; END IF;
    IF (SELECT count(*) FROM entries WHERE journal_id = identity) < 2
       OR EXISTS (SELECT asset FROM entries WHERE journal_id = identity
                  GROUP BY asset HAVING sum(amount) <> 0) THEN
        RAISE EXCEPTION 'journal must balance independently for each asset';
    END IF;
    RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER balanced_journal AFTER INSERT ON journals
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION balanced_journal();
CREATE CONSTRAINT TRIGGER balanced_entries AFTER INSERT ON entries
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION balanced_journal();

CREATE TABLE reservations (
    id uuid PRIMARY KEY,
    member_id uuid NOT NULL REFERENCES members,
    request_key text NOT NULL,
    request_hash text NOT NULL,
    creation_round integer NOT NULL CHECK (creation_round > 0),
    resource text NOT NULL CHECK (resource IN ('CPU','GPU')),
    base_amount numeric NOT NULL CHECK (base_amount > 0 AND base_amount = trunc(base_amount)),
    multiplier numeric NOT NULL CHECK (multiplier BETWEEN 0 AND 1),
    multiplier_revision bigint NOT NULL CHECK (multiplier_revision >= 0),
    amount numeric NOT NULL CHECK (amount >= 0 AND amount = trunc(amount)),
    CHECK (amount = ceil(base_amount * multiplier)),
    rounding text NOT NULL DEFAULT 'ceil-token-unit' CHECK (rounding = 'ceil-token-unit'),
    selection jsonb NOT NULL,
    payload jsonb NOT NULL,
    offer_expires_at timestamptz NOT NULL,
    fee_limit numeric NOT NULL CHECK (fee_limit >= 0 AND fee_limit = trunc(fee_limit)),
    fee_actual numeric CHECK (fee_actual >= 0 AND fee_actual = trunc(fee_actual)),
    state text NOT NULL DEFAULT 'reserved' CHECK (state IN
      ('reserved','uncertain','accepted','rejected','cancelled','active','verification_failed','expired')),
    slot_held boolean NOT NULL DEFAULT true,
    collateral_outcome text CHECK (collateral_outcome IN ('returned','forfeited')),
    benchmark_id text UNIQUE,
    assignment jsonb,
    assignment_digest text,
    handed_over_at timestamptz,
    active_at_height bigint,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(member_id, request_key),
    CHECK ((assignment IS NULL) = (assignment_digest IS NULL)),
    CHECK (handed_over_at IS NULL OR assignment IS NOT NULL)
);
CREATE INDEX reservations_member_slots ON reservations(member_id) WHERE slot_held;
CREATE INDEX reservations_round ON reservations(creation_round) WHERE collateral_outcome IS NULL;
CREATE TABLE reservation_events (
    event_key text PRIMARY KEY,
    reservation_id uuid NOT NULL REFERENCES reservations,
    kind text NOT NULL,
    details jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TRIGGER reservation_events_immutable BEFORE UPDATE OR DELETE ON reservation_events
    FOR EACH ROW EXECUTE FUNCTION immutable_record();
CREATE FUNCTION protect_reservation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'reservation cannot be deleted'; END IF;
    IF (NEW.id, NEW.member_id, NEW.request_key, NEW.request_hash, NEW.creation_round,
        NEW.resource, NEW.base_amount, NEW.multiplier, NEW.multiplier_revision, NEW.amount,
        NEW.rounding, NEW.selection, NEW.payload, NEW.offer_expires_at, NEW.fee_limit)
       IS DISTINCT FROM
       (OLD.id, OLD.member_id, OLD.request_key, OLD.request_hash, OLD.creation_round,
        OLD.resource, OLD.base_amount, OLD.multiplier, OLD.multiplier_revision, OLD.amount,
        OLD.rounding, OLD.selection, OLD.payload, OLD.offer_expires_at, OLD.fee_limit)
       OR (OLD.benchmark_id IS NOT NULL AND NEW.benchmark_id IS DISTINCT FROM OLD.benchmark_id)
       OR (OLD.assignment IS NOT NULL AND
           (NEW.assignment, NEW.assignment_digest) IS DISTINCT FROM (OLD.assignment, OLD.assignment_digest))
       OR (OLD.handed_over_at IS NOT NULL AND NEW.handed_over_at IS DISTINCT FROM OLD.handed_over_at)
       OR (OLD.fee_actual IS NOT NULL AND NEW.fee_actual IS DISTINCT FROM OLD.fee_actual)
       OR (OLD.active_at_height IS NOT NULL AND NEW.active_at_height IS DISTINCT FROM OLD.active_at_height)
       OR (NOT OLD.slot_held AND NEW.slot_held)
       OR (OLD.collateral_outcome IS NOT NULL AND NEW.collateral_outcome IS DISTINCT FROM OLD.collateral_outcome)
    THEN RAISE EXCEPTION 'immutable reservation ownership, pricing, or evidence'; END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER reservations_guard BEFORE UPDATE OR DELETE ON reservations
    FOR EACH ROW EXECUTE FUNCTION protect_reservation();

CREATE TABLE transfers (
    event_id text PRIMARY KEY,
    chain_id bigint NOT NULL CHECK (chain_id > 0),
    token text NOT NULL,
    tx_hash text NOT NULL,
    log_index integer NOT NULL CHECK (log_index >= 0),
    block_number bigint NOT NULL CHECK (block_number >= 0),
    block_hash text NOT NULL,
    block_timestamp timestamptz NOT NULL,
    sender text NOT NULL,
    recipient text NOT NULL,
    amount numeric NOT NULL CHECK (amount > 0 AND amount = trunc(amount)),
    evidence jsonb NOT NULL,
    UNIQUE(chain_id, token, tx_hash, log_index)
);
CREATE TRIGGER transfers_immutable BEFORE UPDATE OR DELETE ON transfers
    FOR EACH ROW EXECUTE FUNCTION immutable_record();
CREATE TABLE transfer_attributions (
    event_id text PRIMARY KEY REFERENCES transfers,
    destination text NOT NULL REFERENCES accounts,
    actor text NOT NULL,
    evidence jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TRIGGER transfer_attributions_immutable BEFORE UPDATE OR DELETE ON transfer_attributions
    FOR EACH ROW EXECUTE FUNCTION immutable_record();

CREATE TABLE withdrawals (
    id uuid PRIMARY KEY,
    member_id uuid NOT NULL REFERENCES members,
    request_key text NOT NULL,
    amount numeric NOT NULL CHECK (amount > 0 AND amount = trunc(amount)),
    recipient text NOT NULL,
    state text NOT NULL DEFAULT 'requested' CHECK (state IN
        ('requested','approved','uncertain','paid','rejected','cancelled')),
    paid_event text UNIQUE REFERENCES transfers,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(member_id, request_key)
);
CREATE UNIQUE INDEX one_pending_withdrawal ON withdrawals(member_id)
    WHERE state IN ('requested','approved','uncertain');
CREATE FUNCTION protect_withdrawal() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'withdrawal cannot be deleted'; END IF;
    IF (NEW.id, NEW.member_id, NEW.request_key, NEW.amount, NEW.recipient, NEW.created_at)
       IS DISTINCT FROM
       (OLD.id, OLD.member_id, OLD.request_key, OLD.amount, OLD.recipient, OLD.created_at)
       OR (OLD.paid_event IS NOT NULL AND NEW.paid_event IS DISTINCT FROM OLD.paid_event)
       OR (OLD.state IN ('paid','rejected','cancelled') AND NEW.state <> OLD.state)
    THEN RAISE EXCEPTION 'immutable withdrawal request or final outcome'; END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER withdrawals_guard BEFORE UPDATE OR DELETE ON withdrawals
    FOR EACH ROW EXECUTE FUNCTION protect_withdrawal();

CREATE TABLE auth_challenges (
    id uuid PRIMARY KEY,
    wallet text NOT NULL,
    message text NOT NULL,
    expires_at timestamptz NOT NULL,
    used_at timestamptz
);
CREATE TABLE tokens (
    digest text PRIMARY KEY,
    member_id uuid NOT NULL REFERENCES members,
    kind text NOT NULL CHECK (kind IN ('wallet','execution')),
    expires_at timestamptz NOT NULL,
    revoked_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

-- Application grants must exclude DDL/TRUNCATE too; these guards also make an
-- accidental maintenance TRUNCATE fail before destroying financial history.
DO $$ DECLARE name text; BEGIN
    FOREACH name IN ARRAY ARRAY['accounts','journals','entries','members',
        'multiplier_changes','reservations','reservation_events','transfers',
        'transfer_attributions','withdrawals'] LOOP
        EXECUTE format('CREATE TRIGGER prevent_truncate BEFORE TRUNCATE ON %I '
                       'FOR EACH STATEMENT EXECUTE FUNCTION immutable_record()', name);
    END LOOP;
END $$;
