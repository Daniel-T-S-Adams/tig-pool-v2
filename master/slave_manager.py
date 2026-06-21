import os
import json
import logging
import re
import time
import random
from threading import Thread, Lock
from dataclasses import dataclass
from fastapi import FastAPI, Request, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
import uvicorn
from common.structs import *
from common.utils import *
from typing import Dict, List, Optional, Set
from master.sql import get_db_conn
from master.client_manager import CONFIG


logger = logging.getLogger(os.path.splitext(os.path.basename(__file__))[0])


def _batch_retry_time(algorithm_id: str) -> int:
    """Return the retry timeout (ms) for a given algorithm_id.

    Falls back to the global time_before_batch_retry if no per-challenge
    override is set.  per_challenge_time_before_batch_retry is keyed by
    challenge prefix, e.g. {"c005": 2400000, "c004": 600000}.
    """
    challenge_id = algorithm_id.split("_")[0] if "_" in algorithm_id else algorithm_id
    overrides = CONFIG.get("per_challenge_time_before_batch_retry", {})
    return overrides.get(challenge_id, CONFIG["time_before_batch_retry"])


class SlaveManager:
    def __init__(self):
        self.batches = []
        self.lock = Lock()
        self._slot_table_ready = False

    def _ensure_slot_table(self):
        if self._slot_table_ready:
            return
        get_db_conn().execute(
            """
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
            )
            """
        )
        get_db_conn().execute(
            "CREATE INDEX IF NOT EXISTS idx_benchmark_slot_type ON benchmark_slot(slot_type)"
        )
        get_db_conn().execute(
            "CREATE INDEX IF NOT EXISTS idx_benchmark_slot_benchmark_id ON benchmark_slot(benchmark_id)"
        )
        get_db_conn().execute(
            "CREATE INDEX IF NOT EXISTS idx_benchmark_slot_state ON benchmark_slot(state)"
        )
        self._slot_table_ready = True

    def _resource_slot_counts(self) -> Dict[str, int]:
        cfg = CONFIG.get("resource_slots", {})
        if not cfg:
            return {}
        if cfg.get("enabled") is False:
            return {}
        counts = cfg.get("slots", cfg)
        return {
            str(k): int(v)
            for k, v in counts.items()
            if k != "enabled" and isinstance(v, int) and v > 0
        }

    def _slot_types_for_slave(self, slave_name: str) -> List[str]:
        counts = self._resource_slot_counts()
        if not counts:
            return []
        if slave_name.startswith("pool-cpu-") or slave_name.startswith("aws-cpu-slave-"):
            return ["cpu"] if "cpu" in counts else []
        if slave_name.startswith("pool-gpu-") or slave_name.startswith("c3-slave-"):
            return [t for t in ("vector_search", "hypergraph", "neuralnet_optimizer") if t in counts]
        return []

    def _sync_slots(self):
        counts = self._resource_slot_counts()
        if not counts:
            return
        self._ensure_slot_table()
        queries = []
        for slot_type, count in counts.items():
            for i in range(1, count + 1):
                queries.append((
                    """
                    INSERT INTO benchmark_slot (slot_id, slot_type)
                    VALUES (%s, %s)
                    ON CONFLICT (slot_id) DO NOTHING
                    """,
                    (f"{slot_type}_{i:03d}", slot_type)
                ))
        if queries:
            get_db_conn().execute_many(*queries)

    def _release_slots(self):
        """Free slots whose benchmark has finished, stopped, expired, or disappeared."""
        if not self._resource_slot_counts():
            return
        self._ensure_slot_table()
        get_db_conn().execute(
            """
            UPDATE benchmark_slot S
            SET benchmark_id = NULL,
                challenge = NULL,
                algorithm_id = NULL,
                track_id = NULL,
                assigned_at = NULL,
                last_activity_at = NULL,
                state = 'idle'
            FROM job J
            WHERE S.benchmark_id = J.benchmark_id
              AND (
                J.stopped IS NOT NULL
                OR J.end_time IS NOT NULL
                OR J.merkle_proofs_ready IS NOT NULL
              )
            """
        )
        get_db_conn().execute(
            """
            UPDATE benchmark_slot S
            SET benchmark_id = NULL,
                challenge = NULL,
                algorithm_id = NULL,
                track_id = NULL,
                assigned_at = NULL,
                last_activity_at = NULL,
                state = 'idle'
            WHERE S.benchmark_id IS NOT NULL
              AND NOT EXISTS (
                SELECT 1 FROM job J WHERE J.benchmark_id = S.benchmark_id
              )
            """
        )

    def _challenge_matches_slot(self, slot_type: str) -> str:
        if slot_type == "cpu":
            return "J.challenge NOT IN ('vector_search', 'hypergraph', 'neuralnet_optimizer')"
        return "J.challenge = %s"

    def _assign_idle_slots(self, slot_types: List[str]):
        if not slot_types:
            return
        now_ms = int(time.time() * 1000)
        for slot_type in slot_types:
            idle_slots = get_db_conn().fetch_all(
                """
                SELECT slot_id
                FROM benchmark_slot
                WHERE slot_type = %s
                  AND benchmark_id IS NULL
                ORDER BY slot_id
                """,
                (slot_type,)
            )
            for slot in idle_slots:
                if slot_type == "cpu":
                    job = get_db_conn().fetch_one(
                        """
                        SELECT J.benchmark_id, J.challenge, J.settings
                        FROM job J
                        WHERE J.stopped IS NULL
                          AND J.end_time IS NULL
                          AND J.challenge NOT IN ('vector_search', 'hypergraph', 'neuralnet_optimizer')
                          AND NOT EXISTS (
                            SELECT 1 FROM benchmark_slot S WHERE S.benchmark_id = J.benchmark_id
                          )
                          AND (
                            EXISTS (
                              SELECT 1 FROM root_batch R
                              WHERE R.benchmark_id = J.benchmark_id AND R.ready IS NULL
                            )
                            OR EXISTS (
                              SELECT 1 FROM proofs_batch P
                              WHERE P.benchmark_id = J.benchmark_id AND P.ready IS NULL
                            )
                          )
                        ORDER BY J.block_started, J.start_time, J.benchmark_id
                        LIMIT 1
                        """
                    )
                else:
                    job = get_db_conn().fetch_one(
                        """
                        SELECT J.benchmark_id, J.challenge, J.settings
                        FROM job J
                        WHERE J.stopped IS NULL
                          AND J.end_time IS NULL
                          AND J.challenge = %s
                          AND NOT EXISTS (
                            SELECT 1 FROM benchmark_slot S WHERE S.benchmark_id = J.benchmark_id
                          )
                          AND (
                            EXISTS (
                              SELECT 1 FROM root_batch R
                              WHERE R.benchmark_id = J.benchmark_id AND R.ready IS NULL
                            )
                            OR EXISTS (
                              SELECT 1 FROM proofs_batch P
                              WHERE P.benchmark_id = J.benchmark_id AND P.ready IS NULL
                            )
                          )
                        ORDER BY J.block_started, J.start_time, J.benchmark_id
                        LIMIT 1
                        """,
                        (slot_type,)
                    )
                if job is None:
                    break
                settings = job["settings"] or {}
                get_db_conn().execute(
                    """
                    UPDATE benchmark_slot
                    SET benchmark_id = %s,
                        challenge = %s,
                        algorithm_id = %s,
                        track_id = %s,
                        assigned_at = %s,
                        last_activity_at = %s,
                        state = 'root'
                    WHERE slot_id = %s
                    """,
                    (
                        job["benchmark_id"],
                        job["challenge"],
                        settings.get("algorithm_id"),
                        settings.get("track_id"),
                        now_ms,
                        now_ms,
                        slot["slot_id"],
                    )
                )
                logger.info(
                    f"slot {slot['slot_id']} ({slot_type}) assigned benchmark "
                    f"{job['benchmark_id']} ({job['challenge']}, {settings.get('track_id')})"
                )

    def _slot_benchmark_ids(self, slot_types: List[str]) -> Set[str]:
        if not slot_types:
            return set()
        rows = get_db_conn().fetch_all(
            """
            SELECT benchmark_id
            FROM benchmark_slot
            WHERE slot_type IN %s
              AND benchmark_id IS NOT NULL
            """,
            (tuple(slot_types),)
        )
        return {r["benchmark_id"] for r in rows}

    def _mark_slot_activity(self, benchmark_id: str, state: str):
        if not self._resource_slot_counts():
            return
        get_db_conn().execute(
            """
            UPDATE benchmark_slot
            SET last_activity_at = (EXTRACT(EPOCH FROM NOW()) * 1000)::BIGINT,
                state = %s
            WHERE benchmark_id = %s
            """,
            (state, benchmark_id)
        )

    def run(self):
        with self.lock:
            self.batches = get_db_conn().fetch_all(
                """
                SELECT * FROM (
                    SELECT
                        A.slave,
                        A.start_time,
                        A.end_time,
                        A.num_attempts,
                        JSONB_BUILD_OBJECT(
                            'id', A.benchmark_id || '_' || A.batch_idx,
                            'benchmark_id', A.benchmark_id,
                            'start_nonce', A.batch_idx * B.batch_size,
                            'num_nonces', LEAST(B.batch_size, B.num_nonces - A.batch_idx * B.batch_size),
                            'settings', B.settings,
                            'hyperparameters', B.hyperparameters,
                            'sampled_nonces', A.sampled_nonces,
                            'fuel_budget', B.fuel_budget,
                            'download_url', B.download_url,
                            'rand_hash', B.rand_hash,
                            'batch_size', B.batch_size,
                            'batch_idx', A.batch_idx,
                            'challenge', B.challenge,
                            'algorithm', B.algorithm
                        ) AS batch
                    FROM proofs_batch A
                    INNER JOIN job B
                        ON A.ready IS NULL
                        AND B.merkle_root_ready
                        AND B.stopped IS NULL
                        AND A.benchmark_id = B.benchmark_id
                    ORDER BY B.block_started, A.benchmark_id, A.batch_idx
                )
                
                UNION ALL
                
                SELECT * FROM (
                    SELECT
                        A.slave,
                        A.start_time,
                        A.end_time,
                        A.num_attempts,
                        JSONB_BUILD_OBJECT(
                            'id', A.benchmark_id || '_' || A.batch_idx,
                            'benchmark_id', A.benchmark_id,
                            'start_nonce', A.batch_idx * B.batch_size,
                            'num_nonces', LEAST(B.batch_size, B.num_nonces - A.batch_idx * B.batch_size),
                            'settings', B.settings,
                            'hyperparameters', B.hyperparameters,
                            'sampled_nonces', NULL,
                            'fuel_budget', B.fuel_budget,
                            'download_url', B.download_url,
                            'rand_hash', B.rand_hash,
                            'batch_size', B.batch_size,
                            'batch_idx', A.batch_idx,
                            'challenge', B.challenge,
                            'algorithm', B.algorithm
                        ) AS batch
                    FROM root_batch A
                    INNER JOIN job B
                        ON A.ready IS NULL
                        AND B.stopped IS NULL
                        AND A.benchmark_id = B.benchmark_id
                    ORDER BY B.block_started, A.benchmark_id, A.batch_idx
                )
                """
            )
            logger.debug(f"Refreshed pending batches. Got {len(self.batches)}")

    def start(self):
        app = FastAPI()

        @app.route('/get-batches', methods=['GET'])
        def get_batch(request: Request):
            if (slave_name := request.headers.get('User-Agent', None)) is None:
                return "User-Agent header is required", 403
            if not any(re.match(slave["name_regex"], slave_name) for slave in CONFIG["slaves"]):
                logger.warning(f"slave {slave_name} does not match any regex. rejecting get-batch request")
                raise HTTPException(status_code=403, detail="Unregistered slave")

            slave = next((slave for slave in CONFIG["slaves"] if re.match(slave["name_regex"], slave_name)), None)

            concurrent = []
            updates = []
            now = time.time() * 1000
            slot_types = self._slot_types_for_slave(slave_name)
            slot_benchmark_ids = set()
            if slot_types:
                self._sync_slots()
                self._release_slots()
                self._assign_idle_slots(slot_types)
                slot_benchmark_ids = self._slot_benchmark_ids(slot_types)

            with self.lock:
                max_concurrent = slave["max_concurrent_batches"]
                # Fair-share: cap how many concurrent batches any single benchmark may hold
                # on this slave, so one benchmark can't drain every slot and starve the other
                # challenges (the batches are ordered oldest-precommit-first). Default to a
                # quarter of the slave's capacity when not explicitly configured.
                per_bench_cap = CONFIG.get("max_batches_per_benchmark", 0)
                if not per_bench_cap or per_bench_cap < 1:
                    per_bench_cap = max(1, max_concurrent // 4)

                concurrent = [
                    b["batch"] for b in self.batches
                    if b["slave"] == slave_name
                ]
                concurrent_by_bench = {}
                for b in self.batches:
                    if b["slave"] == slave_name and b["end_time"] is None:
                        bid = b["batch"]["benchmark_id"]
                        concurrent_by_bench[bid] = concurrent_by_bench.get(bid, 0) + 1

                def assign_pass(respect_cap):
                    for b in self.batches:
                        batch = b["batch"]
                        bid = batch["benchmark_id"]
                        if len(concurrent) >= max_concurrent:
                            break
                        if (
                            b["slave"] == slave_name or
                            not re.match(slave["algorithm_id_regex"], batch["settings"]["algorithm_id"]) or
                            b["end_time"] is not None
                        ):
                            continue
                        if slot_types and bid not in slot_benchmark_ids:
                            continue
                        if not (
                            b["slave"] is None or
                            b["start_time"] is None or
                            (now - b["start_time"]) > _batch_retry_time(batch["settings"]["algorithm_id"])
                        ):
                            continue
                        if respect_cap and concurrent_by_bench.get(bid, 0) >= per_bench_cap:
                            continue
                        b["slave"] = slave_name
                        b["start_time"] = now
                        b["num_attempts"] += 1
                        concurrent_by_bench[bid] = concurrent_by_bench.get(bid, 0) + 1
                        table = "root_batch" if batch["sampled_nonces"] is None else "proofs_batch"
                        slot_state = "root" if batch["sampled_nonces"] is None else "proof"
                        updates.append((
                            f"""
                            UPDATE {table}
                            SET slave = %s,
                                start_time = %s,
                                num_attempts = %s
                            WHERE benchmark_id = %s
                                AND batch_idx = %s
                            """,
                            (slave_name, now, b["num_attempts"], batch["benchmark_id"], batch["batch_idx"])
                        ))
                        if slot_types:
                            updates.append((
                                """
                                UPDATE benchmark_slot
                                SET last_activity_at = %s,
                                    state = %s
                                WHERE benchmark_id = %s
                                """,
                                (now, slot_state, batch["benchmark_id"])
                            ))
                        concurrent.append(batch)

                # Pass 1: spread across benchmarks (respect per-benchmark cap) so all
                # challenges advance together. Pass 2: if slots remain because few
                # benchmarks are active, fill them ignoring the cap (use full capacity).
                assign_pass(respect_cap=True)
                assign_pass(respect_cap=False)
            if len(concurrent) == 0:
                logger.debug(f"no batches available for {slave_name}")
            if len(updates) > 0:
                get_db_conn().execute_many(*updates)
            return JSONResponse(content=jsonable_encoder(concurrent))

        def find_batch(batch_id: str, request: Request):
            if (slave_name := request.headers.get('User-Agent', None)) is None:
                raise HTTPException(status_code=403, detail="User-Agent header is required")
            
            with self.lock:
                b = next((
                    b for b in self.batches
                    if (
                        b["batch"]["id"] == batch_id and 
                        b["slave"] == slave_name and
                        b["end_time"] is None and
                        (
                            'error' in request.url.path or
                            (b["batch"]["sampled_nonces"] is None) == ('root' in request.url.path)
                        )
                    )
                ), None)
                if b is None:
                    raise HTTPException(
                        status_code=408, 
                        detail=f"Slave {slave_name} posted to {request.url.path}, but either took too long, or was not assigned this batch."
                    )
            
            return slave_name, b

        @app.post('/submit-batch-error/{batch_id}')
        async def submit_batch_error(batch_id: str, request: Request):
            slave_name, b = find_batch(batch_id, request)
            result = await request.json()
            logger.warning(f"slave {slave_name} reported failure for {batch_id}: {result['error']}")

            benchmark_id, batch_idx = batch_id.split("_")
            batch_idx = int(batch_idx)
            if b["num_attempts"] < CONFIG["max_batch_attempts"]:
                table_name = "root_batch" if b["batch"]["sampled_nonces"] is None else "proofs_batch"
                queries = [
                    (
                        f"""
                        UPDATE {table_name}
                        SET slave = NULL
                        WHERE benchmark_id = %s 
                            AND batch_idx = %s
                        """, 
                        (
                            benchmark_id,
                            batch_idx
                        )
                    )
                ]
            else:
                queries = [
                    (
                        """
                        UPDATE job
                        SET stopped = True
                        WHERE benchmark_id = %s 
                        """, 
                        (
                            benchmark_id,
                        )
                    )
                ]
            get_db_conn().execute_many(*queries)

            return {"status": "OK"}

        @app.post('/submit-batch-root/{batch_id}')
        async def submit_batch_root(batch_id: str, request: Request):
            slave_name, b = find_batch(batch_id, request)
            try:
                result = await request.json()
                merkle_root = MerkleHash.from_str(result["merkle_root"])
                solution_quality = result["solution_quality"]
                assert isinstance(solution_quality, list) and all(isinstance(x, int) for x in solution_quality)
                logger.debug(f"slave {slave_name} submitted root for {batch_id}")
            except Exception as e:
                logger.error(f"slave {slave_name} submitted INVALID root for {batch_id}: {e}")
                raise HTTPException(status_code=400, detail="INVALID root")
            # Update roots table with merkle root and solution quality
            benchmark_id, batch_idx = batch_id.split("_")
            batch_idx = int(batch_idx)
            queries = [
                (
                    """
                    UPDATE root_batch
                    SET ready = true,
                        end_time = (EXTRACT(EPOCH FROM NOW()) * 1000)::BIGINT
                    WHERE benchmark_id = %s 
                        AND batch_idx = %s
                    """, 
                    (
                        benchmark_id,
                        batch_idx
                    )
                ),
                (
                    """
                    UPDATE batch_data
                    SET merkle_root = %s,
                        solution_quality = %s,
                        average_quality = %s
                    WHERE benchmark_id = %s 
                        AND batch_idx = %s                    
                    """,
                    (
                        merkle_root.to_str(),
                        json.dumps(solution_quality),
                        sum(solution_quality) // len(solution_quality),
                        benchmark_id,
                        batch_idx
                    )
                )
            ]
            get_db_conn().execute_many(*queries)

            return {"status": "OK"}

        @app.post('/submit-batch-proofs/{batch_id}')
        async def submit_batch_proofs(batch_id: str, request: Request):
            slave_name, b = find_batch(batch_id, request)
            try:
                result = await request.json()
                merkle_proofs = [MerkleProof.from_dict(x) for x in result["merkle_proofs"]]
                logger.debug(f"slave {slave_name} submitted proofs for {batch_id}")
            except Exception as e:
                logger.error(f"slave {slave_name} submitted INVALID proofs for {batch_id}: {e}")
                raise HTTPException(status_code=400, detail="INVALID proofs")
            # Update proofs table with merkle proofs
            benchmark_id, batch_idx = batch_id.split("_")
            batch_idx = int(batch_idx)
            get_db_conn().execute_many(*[
                (
                    """
                    UPDATE proofs_batch
                    SET ready = true,
                        end_time = (EXTRACT(EPOCH FROM NOW()) * 1000)::BIGINT
                    WHERE benchmark_id = %s 
                        AND batch_idx = %s
                    """, 
                    (benchmark_id, batch_idx)
                ),
                (
                    """
                    UPDATE batch_data
                    SET merkle_proofs = %s
                    WHERE benchmark_id = %s 
                        AND batch_idx = %s
                    """, 
                    (
                        json.dumps([x.to_dict() for x in merkle_proofs]), 
                        benchmark_id, 
                        batch_idx
                    )
                )
            ])

            return {"status": "OK"}
            
        thread = Thread(target=lambda: uvicorn.run(app, host="0.0.0.0", port=5115))
        thread.daemon = True
        thread.start()

        logger.info(f"webserver started on 0.0.0.0:5115")
