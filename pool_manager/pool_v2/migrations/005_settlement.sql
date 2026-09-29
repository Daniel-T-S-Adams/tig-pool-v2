CREATE TABLE round_report_scopes (
    creation_round integer PRIMARY KEY CHECK (creation_round>0),
    reporting_rounds integer[] NOT NULL CHECK (cardinality(reporting_rounds)>0),
    adapter_version text NOT NULL,
    evidence jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE report_captures (
    id uuid PRIMARY KEY,
    reporting_round integer NOT NULL CHECK (reporting_round>0),
    block_id text NOT NULL REFERENCES observed_blocks(id),
    payload_sha256 text NOT NULL,
    input_digest text NOT NULL,
    compressed_payload bytea NOT NULL,
    metadata jsonb NOT NULL,
    complete boolean NOT NULL,
    error text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(reporting_round,block_id,input_digest)
);
CREATE TABLE confirmed_reports (
    report_id text PRIMARY KEY,
    reporting_round integer NOT NULL,
    benchmark_id text NOT NULL,
    benchmarker text NOT NULL,
    nonce numeric NOT NULL CHECK (nonce>=0 AND nonce=trunc(nonce)),
    confirmed_height bigint NOT NULL,
    capture_id uuid NOT NULL REFERENCES report_captures
);
CREATE TABLE report_index_captures (
    id uuid PRIMARY KEY,
    reporting_round integer NOT NULL CHECK (reporting_round>0),
    block_id text NOT NULL REFERENCES observed_blocks(id),
    player_id text NOT NULL,
    challenge_id text NOT NULL,
    payload_sha256 text NOT NULL,
    input_digest text NOT NULL,
    compressed_payload bytea NOT NULL,
    metadata jsonb NOT NULL,
    complete boolean NOT NULL,
    error text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(reporting_round,block_id,player_id,challenge_id,input_digest)
);
CREATE TABLE benchmark_reporting_rounds (
    benchmark_id text PRIMARY KEY REFERENCES reservations(benchmark_id),
    creation_round integer NOT NULL,
    reporting_round integer NOT NULL CHECK (reporting_round>0),
    capture_id uuid NOT NULL REFERENCES report_index_captures,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE confirmed_arbitrations (
    report_id text PRIMARY KEY REFERENCES confirmed_reports,
    result text NOT NULL CHECK (result IN ('nonreproducible','reproducible','inconclusive')),
    confirmed_height bigint NOT NULL,
    capture_id uuid NOT NULL REFERENCES report_captures
);
CREATE TABLE round_report_seals (
    id uuid PRIMARY KEY,
    creation_round integer NOT NULL REFERENCES round_report_scopes,
    block_id text NOT NULL REFERENCES observed_blocks(id),
    capture_ids uuid[] NOT NULL,
    reports jsonb NOT NULL,
    input_digest text NOT NULL UNIQUE,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE round_earnings (
    round integer PRIMARY KEY CHECK (round>0),
    expected_received_net numeric NOT NULL CHECK (expected_received_net>=0 AND expected_received_net=trunc(expected_received_net)),
    withheld_operating_cost numeric NOT NULL CHECK (withheld_operating_cost>=0 AND withheld_operating_cost=trunc(withheld_operating_cost)),
    block_id text NOT NULL REFERENCES observed_blocks(id),
    evidence jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE round_receipts (
    event_id text PRIMARY KEY REFERENCES transfers,
    round integer NOT NULL REFERENCES round_earnings,
    amount numeric NOT NULL CHECK (amount>0 AND amount=trunc(amount)),
    evidence jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE round_reimbursements (
    round integer PRIMARY KEY REFERENCES round_earnings,
    amount numeric NOT NULL CHECK (amount>=0 AND amount=trunc(amount)),
    journal_id uuid REFERENCES journals,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE collateral_finalizations (
    reservation_id uuid PRIMARY KEY REFERENCES reservations,
    outcome text NOT NULL CHECK (outcome IN ('returned','forfeited')),
    amount numeric NOT NULL CHECK (amount>=0 AND amount=trunc(amount)),
    report_seal_id uuid NOT NULL REFERENCES round_report_seals,
    journal_id uuid REFERENCES journals,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE round_settlements (
    round integer PRIMARY KEY REFERENCES round_earnings,
    rule text NOT NULL,
    input_digest text NOT NULL,
    inputs jsonb NOT NULL,
    pot numeric NOT NULL CHECK (pot>=0 AND pot=trunc(pot)),
    operator_allocation numeric NOT NULL CHECK (operator_allocation>=0 AND operator_allocation=trunc(operator_allocation)),
    member_allocations jsonb NOT NULL,
    journal_id uuid REFERENCES journals,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
DO $$ DECLARE name text; BEGIN
    FOREACH name IN ARRAY ARRAY['round_report_scopes','report_captures','confirmed_reports','confirmed_arbitrations',
        'report_index_captures','benchmark_reporting_rounds','round_report_seals','round_earnings',
        'round_receipts','round_reimbursements','collateral_finalizations','round_settlements'] LOOP
        EXECUTE format('CREATE TRIGGER immutable BEFORE UPDATE OR DELETE ON %I '
                       'FOR EACH ROW EXECUTE FUNCTION immutable_record()', name);
        EXECUTE format('CREATE TRIGGER prevent_truncate BEFORE TRUNCATE ON %I '
                       'FOR EACH STATEMENT EXECUTE FUNCTION immutable_record()', name);
    END LOOP;
END $$;
