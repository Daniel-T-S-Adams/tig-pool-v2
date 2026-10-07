-- Run as the pool_v2 schema owner after applying migrations.
-- Public funding collection must be able to bind its one immutable player ID.
GRANT INSERT ON TABLE pool_v2.protocol_identity TO innopool_runtime;
GRANT INSERT ON TABLE pool_v2.custody_opening_baselines TO innopool_runtime;
