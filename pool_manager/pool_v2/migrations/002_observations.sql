CREATE TABLE observation_chunks (
    digest text PRIMARY KEY CHECK (length(digest) = 64),
    compressed bytea NOT NULL
);
CREATE TABLE capture_attempts (
    id uuid PRIMARY KEY,
    collector text NOT NULL,
    block_id text,
    height bigint,
    manifest jsonb NOT NULL,
    metadata jsonb NOT NULL,
    error text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX capture_attempts_by_height ON capture_attempts(height);
CREATE TABLE observed_blocks (
    id text PRIMARY KEY,
    height bigint NOT NULL UNIQUE,
    previous_id text NOT NULL,
    round integer NOT NULL CHECK (round > 0),
    timestamp bigint NOT NULL,
    blocks_per_round integer NOT NULL CHECK (blocks_per_round > 0),
    semantic_digest text NOT NULL,
    attempt_id uuid NOT NULL REFERENCES capture_attempts,
    captured_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE observation_stream (
    name text PRIMARY KEY CHECK (name='tig'),
    launch_height bigint NOT NULL CHECK (launch_height >= 0),
    latest_seen_height bigint NOT NULL,
    contiguous_height bigint NOT NULL,
    CHECK (contiguous_height >= launch_height-1)
);
CREATE TABLE observation_alerts (
    id bigserial PRIMARY KEY,
    kind text NOT NULL,
    height bigint,
    details jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE credited_blocks (
    block_id text PRIMARY KEY REFERENCES observed_blocks,
    player_id text NOT NULL,
    rule text NOT NULL CHECK (rule='equal-cutoff-bundles-v1'),
    ownership_digest text NOT NULL,
    total_num numeric NOT NULL CHECK (total_num >= 0 AND total_num=trunc(total_num)),
    total_den numeric NOT NULL CHECK (total_den > 0 AND total_den=trunc(total_den))
);
CREATE TABLE benchmark_credits (
    block_id text NOT NULL REFERENCES credited_blocks,
    benchmark_id text NOT NULL REFERENCES reservations(benchmark_id),
    member_id uuid NOT NULL REFERENCES members,
    numerator numeric NOT NULL CHECK (numerator >= 0 AND numerator=trunc(numerator)),
    denominator numeric NOT NULL CHECK (denominator > 0 AND denominator=trunc(denominator)),
    PRIMARY KEY(block_id, benchmark_id)
);
CREATE INDEX benchmark_credits_member ON benchmark_credits(member_id);
DO $$ DECLARE name text; BEGIN
    FOREACH name IN ARRAY ARRAY['observation_chunks','capture_attempts','observed_blocks',
        'observation_alerts','credited_blocks','benchmark_credits'] LOOP
        EXECUTE format('CREATE TRIGGER immutable BEFORE UPDATE OR DELETE ON %I '
                       'FOR EACH ROW EXECUTE FUNCTION immutable_record()', name);
        EXECUTE format('CREATE TRIGGER prevent_truncate BEFORE TRUNCATE ON %I '
                       'FOR EACH STATEMENT EXECUTE FUNCTION immutable_record()', name);
    END LOOP;
END $$;
