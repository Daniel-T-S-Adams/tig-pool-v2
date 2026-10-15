-- A public archive that was downloaded and checked once is remembered by its
-- URL, library name and compute type. Later reservations of the same algorithm
-- reuse that hash and size instead of downloading the archive again. Rows are
-- append-only; the newest row within the coordinator's reuse age is used.
CREATE TABLE verified_binaries (
    id uuid PRIMARY KEY,
    source_url text NOT NULL,
    algorithm_name text NOT NULL,
    compute_type text NOT NULL,
    sha256 text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    size_bytes integer NOT NULL CHECK (size_bytes>0 AND size_bytes<=67108864),
    verified_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX verified_binaries_by_source ON verified_binaries(source_url,algorithm_name,compute_type,verified_at DESC);
CREATE TRIGGER immutable BEFORE UPDATE OR DELETE ON verified_binaries
    FOR EACH ROW EXECUTE FUNCTION immutable_record();
CREATE TRIGGER prevent_truncate BEFORE TRUNCATE ON verified_binaries
    FOR EACH STATEMENT EXECUTE FUNCTION immutable_record();
