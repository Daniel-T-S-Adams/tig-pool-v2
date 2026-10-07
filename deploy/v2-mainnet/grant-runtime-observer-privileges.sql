-- Run as the pool_v2 schema owner after applying migrations.
-- Public funding collection must bind its immutable player ID. Runtime block
-- status and round-coverage reads also need to see the operator's gap waiver.
GRANT INSERT ON TABLE pool_v2.protocol_identity TO innopool_runtime;
GRANT INSERT ON TABLE pool_v2.custody_opening_baselines TO innopool_runtime;
GRANT SELECT ON TABLE pool_v2.observation_gap_waivers TO innopool_runtime;
