-- Run as the pool_v2 schema owner after applying migrations.
-- Public funding collection must bind its immutable player ID. Runtime block
-- status and round-coverage reads also need to see the operator's gap waiver.
GRANT INSERT ON TABLE pool_v2.protocol_identity TO innopool_runtime;
GRANT INSERT ON TABLE pool_v2.custody_opening_baselines TO innopool_runtime;
GRANT SELECT ON TABLE pool_v2.observation_gap_waivers TO innopool_runtime;
-- Migration 018: the block observer maintains the chunk index; status and
-- health reads may inspect the retention floors and runs. Without these the
-- recorder fails every block after the migration (seen 8 October 2026).
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE pool_v2.chunk_last_ref TO innopool_observer, innopool_runtime;
GRANT SELECT ON TABLE pool_v2.retention_floors, pool_v2.retention_runs TO innopool_observer, innopool_runtime;
