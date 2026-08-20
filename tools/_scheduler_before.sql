-- Live scheduler baseline (run via docker exec -i innopool_db psql)
\pset format aligned
\echo === NOW ===
SELECT now() AT TIME ZONE 'UTC' AS utc_now;

\echo === CREATES / STOPS / FINISHES ===
SELECT
  COUNT(*) FILTER (WHERE start_time >= (EXTRACT(EPOCH FROM now())*1000 - 15*60*1000)) AS creates_15m,
  COUNT(*) FILTER (WHERE start_time >= (EXTRACT(EPOCH FROM now())*1000 - 60*60*1000)) AS creates_60m,
  COUNT(*) FILTER (WHERE start_time >= (EXTRACT(EPOCH FROM now())*1000 - 15*60*1000) AND COALESCE(stopped,false)=true) AS stopped_15m,
  COUNT(*) FILTER (WHERE end_time >= (EXTRACT(EPOCH FROM now())*1000 - 15*60*1000)) AS jobs_ended_15m,
  COUNT(*) FILTER (WHERE end_time >= (EXTRACT(EPOCH FROM now())*1000 - 60*60*1000)) AS jobs_ended_60m,
  COUNT(*) FILTER (WHERE merkle_proofs_ready IS NOT NULL AND COALESCE(proof_submit_time, end_time) >= (EXTRACT(EPOCH FROM now())*1000 - 15*60*1000)) AS proofs_ready_15m,
  COUNT(*) FILTER (WHERE merkle_proofs_ready IS NOT NULL AND COALESCE(proof_submit_time, end_time) >= (EXTRACT(EPOCH FROM now())*1000 - 60*60*1000)) AS proofs_ready_60m,
  COUNT(*) FILTER (WHERE stopped IS NULL AND end_time IS NULL) AS open_jobs
FROM job;

\echo === OPEN JOBS BY PROFILE ===
SELECT
  CASE WHEN settings->>'challenge_id' IN ('c004','c005','c006') THEN 'gpu' ELSE 'cpu' END AS profile,
  COUNT(*) FILTER (WHERE merkle_root_ready IS NULL) AS root_phase,
  COUNT(*) FILTER (WHERE merkle_root_ready IS NOT NULL AND merkle_proofs_ready IS NULL) AS proof_phase,
  COUNT(*) AS open_jobs
FROM job
WHERE stopped IS NULL AND end_time IS NULL
GROUP BY 1
ORDER BY 1;

\echo === ROOTS DONE ===
SELECT
  COUNT(*) FILTER (WHERE ready = true AND end_time >= (EXTRACT(EPOCH FROM now())*1000 - 15*60*1000)) AS roots_done_15m,
  COUNT(*) FILTER (WHERE ready = true AND end_time >= (EXTRACT(EPOCH FROM now())*1000 - 60*60*1000)) AS roots_done_60m
FROM root_batch;

\echo === UNASSIGNED ROOTS ===
SELECT
  CASE WHEN j.settings->>'challenge_id' IN ('c004','c005','c006') THEN 'gpu' ELSE 'cpu' END AS profile,
  COUNT(*) FILTER (WHERE rb.slave IS NULL) AS unassigned,
  COUNT(*) FILTER (WHERE rb.slave IS NOT NULL) AS assigned,
  COUNT(*) AS pending_roots
FROM root_batch rb
JOIN job j ON j.benchmark_id = rb.benchmark_id
WHERE rb.ready IS NULL
  AND j.stopped IS NULL
  AND j.end_time IS NULL
  AND j.merkle_root_ready IS NULL
GROUP BY 1
ORDER BY 1;

\echo === ONLINE SLAVES (2m) ===
SELECT
  CASE
    WHEN slave_name LIKE 'pool-gpu-%' THEN 'gpu'
    ELSE 'cpu'
  END AS profile,
  COUNT(*) AS online
FROM slave_seen
WHERE last_seen >= (EXTRACT(EPOCH FROM now())*1000 - 120000)
GROUP BY 1
ORDER BY 1;
