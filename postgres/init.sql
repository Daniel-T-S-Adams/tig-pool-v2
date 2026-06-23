-- ============================================================
-- TIG Benchmarker Master Schema (unchanged from tig-monorepo)
-- ============================================================

CREATE TABLE IF NOT EXISTS config (
    config JSONB
);

CREATE TABLE IF NOT EXISTS job (
    benchmark_id TEXT PRIMARY KEY,
    settings JSONB NOT NULL,
    hyperparameters JSONB,
    num_nonces INTEGER NOT NULL,
    rand_hash TEXT NOT NULL,
    fuel_budget BIGINT NOT NULL,
    batch_size INTEGER NOT NULL,
    num_batches INTEGER NOT NULL,
    challenge TEXT NOT NULL,
    algorithm TEXT NOT NULL,
    download_url TEXT NOT NULL,
    block_started INTEGER NOT NULL,
    sampled_nonces JSONB,
    benchmark_submit_time BIGINT,
    proof_submit_time BIGINT,
    start_time BIGINT,
    end_time BIGINT,
    merkle_root_ready BOOLEAN,
    merkle_proofs_ready BOOLEAN,
    benchmark_submitted BOOLEAN,
    proof_submitted BOOLEAN,
    stopped BOOLEAN
);

CREATE INDEX idx_job_batch_size ON job(batch_size);
CREATE INDEX idx_job_block_started ON job(block_started);
CREATE INDEX idx_job_challenge ON job(challenge);
CREATE INDEX idx_job_benchmark_submit_time ON job(benchmark_submit_time);
CREATE INDEX idx_job_proof_submit_time ON job(proof_submit_time);
CREATE INDEX idx_job_merkle_root_ready ON job(merkle_root_ready);
CREATE INDEX idx_job_merkle_proofs_ready ON job(merkle_proofs_ready);
CREATE INDEX idx_job_benchmark_submitted ON job(benchmark_submitted);
CREATE INDEX idx_job_proof_submitted ON job(proof_submitted);
CREATE INDEX idx_job_stopped ON job(stopped);

CREATE TABLE IF NOT EXISTS job_data (
    benchmark_id TEXT PRIMARY KEY,
    merkle_root TEXT,
    solution_quality JSONB,
    average_quality INTEGER,
    merkle_proofs JSONB,
    FOREIGN KEY (benchmark_id) REFERENCES job(benchmark_id)
);

CREATE TABLE IF NOT EXISTS root_batch (
    benchmark_id TEXT,
    batch_idx INTEGER,
    slave TEXT,
    start_time BIGINT,
    end_time BIGINT,
    ready BOOLEAN,
    num_attempts INTEGER DEFAULT 0,
    PRIMARY KEY (benchmark_id, batch_idx),
    FOREIGN KEY (benchmark_id) REFERENCES job(benchmark_id)
);

CREATE INDEX idx_root_batch_benchmark_id ON root_batch(benchmark_id);
CREATE INDEX idx_root_batch_batch_idx ON root_batch(batch_idx);
CREATE INDEX idx_root_batch_slave ON root_batch(slave);
CREATE INDEX idx_root_batch_start_time ON root_batch(start_time);
CREATE INDEX idx_root_batch_end_time ON root_batch(end_time);
CREATE INDEX idx_root_batch_ready ON root_batch(ready);

CREATE TABLE IF NOT EXISTS benchmark_slot (
    slot_id TEXT PRIMARY KEY,
    slot_type TEXT NOT NULL,
    benchmark_id TEXT REFERENCES job(benchmark_id),
    challenge TEXT,
    algorithm_id TEXT,
    track_id TEXT,
    assigned_at BIGINT,
    last_activity_at BIGINT,
    state TEXT NOT NULL DEFAULT 'idle'
);

CREATE INDEX IF NOT EXISTS idx_benchmark_slot_type ON benchmark_slot(slot_type);
CREATE INDEX IF NOT EXISTS idx_benchmark_slot_benchmark_id ON benchmark_slot(benchmark_id);
CREATE INDEX IF NOT EXISTS idx_benchmark_slot_state ON benchmark_slot(state);

CREATE TABLE IF NOT EXISTS proofs_batch (
    benchmark_id TEXT REFERENCES job(benchmark_id),
    batch_idx INTEGER,
    slave TEXT,
    start_time BIGINT,
    end_time BIGINT,
    sampled_nonces JSONB,
    ready BOOLEAN,
    num_attempts INTEGER DEFAULT 0,
    PRIMARY KEY (benchmark_id, batch_idx),
    FOREIGN KEY (benchmark_id, batch_idx) REFERENCES root_batch(benchmark_id, batch_idx)
);

CREATE INDEX idx_proofs_batch_benchmark_id ON proofs_batch(benchmark_id);
CREATE INDEX idx_proofs_batch_batch_idx ON proofs_batch(batch_idx);
CREATE INDEX idx_proofs_batch_slave ON proofs_batch(slave);
CREATE INDEX idx_proofs_batch_start_time ON proofs_batch(start_time);
CREATE INDEX idx_proofs_batch_end_time ON proofs_batch(end_time);
CREATE INDEX idx_proofs_batch_ready ON proofs_batch(ready);

CREATE TABLE IF NOT EXISTS batch_data (
    benchmark_id TEXT,
    batch_idx INTEGER,
    merkle_root TEXT,
    solution_quality JSONB,
    average_quality INTEGER,
    merkle_proofs JSONB,
    PRIMARY KEY (benchmark_id, batch_idx),
    FOREIGN KEY (benchmark_id, batch_idx) REFERENCES root_batch(benchmark_id, batch_idx)
);

CREATE INDEX idx_proofs_batch_data_benchmark_id ON batch_data(benchmark_id);
CREATE INDEX idx_proofs_batch_data_batch_idx ON batch_data(batch_idx);

-- Default config (pool operator sets their real api_key/player_id via the benchmarker UI)
INSERT INTO config
SELECT '{
  "player_id": "0x0000000000000000000000000000000000000000",
  "api_key": "00000000000000000000000000000000",
  "api_url": "https://mainnet-api.tig.foundation",
  "time_between_resubmissions": 60000,
  "max_concurrent_benchmarks": 8,
  "algo_selection": [],
  "time_before_batch_retry": 60000,
  "max_batch_attempts": 3,
  "adaptive_slave_caps": {
    "enabled": true,
    "window_ms": 1800000,
    "target_buffer_ms": 600000,
    "warmup_completed_batches": 3,
    "cpu_min_cap": 4,
    "cpu_max_cap": 8,
    "gpu_min_cap": 1,
    "gpu_max_cap": 6
  },
  "slaves": [
    {
      "name_regex": "pool-.*",
      "algorithm_id_regex": ".*",
      "max_concurrent_batches": 2
    }
  ]
}'::jsonb
WHERE NOT EXISTS (SELECT 1 FROM config);


-- ============================================================
-- InnoPool Extension Schema
-- ============================================================

-- Registered pool members: wallet address <-> slave name
-- slave_name is the primary key — one wallet can have multiple slaves (cpu + gpu)
CREATE TABLE IF NOT EXISTS pool_members (
    slave_name TEXT PRIMARY KEY,
    wallet_address TEXT NOT NULL,
    invite_code TEXT,
    registered_at BIGINT NOT NULL DEFAULT (EXTRACT(EPOCH FROM NOW()) * 1000)::BIGINT,
    active BOOLEAN NOT NULL DEFAULT true,
    notes TEXT
);

CREATE INDEX idx_pool_members_slave_name ON pool_members(slave_name);
CREATE INDEX idx_pool_members_active ON pool_members(active);

-- Contribution snapshots: how many nonces each member computed per snapshot window
CREATE TABLE IF NOT EXISTS pool_contributions (
    id SERIAL PRIMARY KEY,
    wallet_address TEXT NOT NULL,
    -- Number of root batches completed in this window
    batches_completed BIGINT NOT NULL DEFAULT 0,
    -- Total nonces computed across those batches
    nonces_computed BIGINT NOT NULL DEFAULT 0,
    -- Fractional share (0.0 to 1.0) of pool compute in this window
    share_fraction FLOAT NOT NULL DEFAULT 0.0,
    -- Block height at time of snapshot
    block_height BIGINT,
    snapshot_start_ms BIGINT NOT NULL,
    snapshot_end_ms BIGINT NOT NULL,
    created_at BIGINT NOT NULL DEFAULT (EXTRACT(EPOCH FROM NOW()) * 1000)::BIGINT
);

CREATE INDEX idx_pool_contributions_wallet ON pool_contributions(wallet_address);
CREATE INDEX idx_pool_contributions_snapshot_end ON pool_contributions(snapshot_end_ms);

-- Coinbase distribution history: records every /set-coinbase call made to TIG API
CREATE TABLE IF NOT EXISTS pool_coinbase_history (
    id SERIAL PRIMARY KEY,
    -- The distribution map sent: { wallet_address: weight, ... }
    distribution JSONB NOT NULL,
    -- Block height when submitted
    block_height BIGINT,
    -- TIG API response
    api_response TEXT,
    success BOOLEAN NOT NULL DEFAULT false,
    submitted_at BIGINT NOT NULL DEFAULT (EXTRACT(EPOCH FROM NOW()) * 1000)::BIGINT
);

-- Invite codes for invite-only registration
CREATE TABLE IF NOT EXISTS pool_invites (
    code TEXT PRIMARY KEY,
    created_by TEXT NOT NULL DEFAULT 'admin',
    used_by TEXT,  -- wallet_address that used this invite
    used_at BIGINT,
    created_at BIGINT NOT NULL DEFAULT (EXTRACT(EPOCH FROM NOW()) * 1000)::BIGINT,
    expires_at BIGINT  -- NULL = never expires
);

-- Pool-level metadata / settings store
CREATE TABLE IF NOT EXISTS pool_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at BIGINT NOT NULL DEFAULT (EXTRACT(EPOCH FROM NOW()) * 1000)::BIGINT
);

INSERT INTO pool_settings (key, value) VALUES
    ('last_coinbase_block', '0'),
    ('last_snapshot_ms', '0'),
    ('coinbase_update_period', '50')  -- update coinbase every 50 blocks (adjust after checking chain config)
ON CONFLICT (key) DO NOTHING;
