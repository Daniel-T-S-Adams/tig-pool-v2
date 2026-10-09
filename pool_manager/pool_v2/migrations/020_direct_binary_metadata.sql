-- New assignments download the checked public artifact directly. Existing
-- archives and assignment URLs remain readable for already issued work.
CREATE TABLE reservation_binaries (
    reservation_id uuid PRIMARY KEY REFERENCES reservations,
    sha256 text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    source_url text NOT NULL,
    size_bytes integer NOT NULL CHECK (size_bytes>0 AND size_bytes<=67108864),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TRIGGER immutable BEFORE UPDATE OR DELETE ON reservation_binaries
    FOR EACH ROW EXECUTE FUNCTION immutable_record();
CREATE TRIGGER prevent_truncate BEFORE TRUNCATE ON reservation_binaries
    FOR EACH STATEMENT EXECUTE FUNCTION immutable_record();
