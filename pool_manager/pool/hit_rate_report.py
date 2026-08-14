"""
Per-track hit-rate report: local max nonce quality vs TIG qualifier floor.

Observe-only. Used by /admin/ops/hit-rate and the ops dashboard. Does not
change autopilot targets or apply config.
"""
from __future__ import annotations

import json
import logging
import os
import statistics
import time
import urllib.error
import urllib.request
from collections import defaultdict
from decimal import Decimal

from pool import database as db

logger = logging.getLogger("pool.hit_rate_report")

MASTER_URL = os.environ.get("MASTER_INTERNAL_URL", "http://master:3336")
HIT_RATE_WINDOW_MS = int(os.environ.get("HIT_RATE_WINDOW_MS", str(6 * 60 * 60 * 1000)))
HIT_RATE_CACHE_MS = int(os.environ.get("HIT_RATE_CACHE_MS", "60000"))

_TIG_CACHE: dict | None = None
_TIG_CACHE_AT_MS = 0


def _json_safe(value):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    return value


def _as_list(obj):
    if obj is None:
        return []
    if isinstance(obj, list):
        return obj
    if isinstance(obj, dict):
        return list(obj.values())
    return []


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    return float(statistics.median(values))


def _p90(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return float(ordered[int(0.9 * (len(ordered) - 1))])


def _max_quality(raw) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return None
    if not isinstance(raw, list) or not raw:
        return None
    nums = [int(x) for x in raw if isinstance(x, (int, float)) or (isinstance(x, str) and str(x).lstrip("-").isdigit())]
    return max(nums) if nums else None


def _configured_bundles(cfg: dict) -> dict[tuple[str, str], int]:
    """Current config only. Do not use this to label historical jobs."""
    out: dict[tuple[str, str], int] = {}
    for algo in cfg.get("algo_selection") or []:
        algorithm_id = algo.get("algorithm_id")
        for track, settings in (algo.get("track_settings") or {}).items():
            try:
                bundles = int((settings or {}).get("num_bundles") or 0)
            except (TypeError, ValueError):
                continue
            if algorithm_id and track and bundles > 0:
                out[(str(algorithm_id), str(track))] = bundles
    return out


def _nonces_per_bundle_from_precommits(precommits) -> dict[tuple[str, str], int]:
    samples: dict[tuple[str, str], list[int]] = defaultdict(list)
    for pc in _as_list(precommits):
        if not isinstance(pc, dict):
            continue
        settings = pc.get("settings") or {}
        details = pc.get("details") or {}
        chal = settings.get("challenge_id")
        track = settings.get("track_id")
        try:
            bundles = int(details.get("num_bundles") or 0)
            nonces = int(details.get("num_nonces") or 0)
        except (TypeError, ValueError):
            continue
        if chal and track and bundles > 0 and nonces > 0 and nonces % bundles == 0:
            samples[(str(chal), str(track))].append(nonces // bundles)
    return {key: int(statistics.median(vals)) for key, vals in samples.items() if vals}


def _derive_bundles(num_nonces, nonces_per_bundle) -> int | None:
    try:
        nonces = int(num_nonces)
        npp = int(nonces_per_bundle)
    except (TypeError, ValueError):
        return None
    if nonces <= 0 or npp <= 0 or nonces % npp != 0:
        return None
    return nonces // npp


CHALLENGE_NAME_TO_ID = {
    "satisfiability": "c001",
    "vehicle_routing": "c002",
    "vehicle": "c002",
    "knapsack": "c003",
    "vector_search": "c004",
    "vector": "c004",
    "hypergraph": "c005",
    "neuralnet_optimizer": "c006",
    "neuralnet": "c006",
    "job_scheduling": "c007",
    "job": "c007",
    "energy": "c008",
}


def _challenge_id_from_algorithm(algorithm_id: str | None) -> str | None:
    prefix = str(algorithm_id or "").split("_", 1)[0]
    if prefix.startswith("c") and prefix[1:].isdigit():
        return prefix
    return None


def _canonical_challenge(*values: str | None) -> str:
    for raw in values:
        text = str(raw or "").strip()
        if not text:
            continue
        if text.startswith("c") and len(text) >= 4 and text[1:4].isdigit():
            return text[:4]
        mapped = CHALLENGE_NAME_TO_ID.get(text.lower()) or CHALLENGE_NAME_TO_ID.get(text.split("_", 1)[0].lower())
        if mapped:
            return mapped
    return str(next((v for v in values if v), "") or "")


def _qualifier_floors(challenges) -> dict[tuple[str, str], int]:
    floors: dict[tuple[str, str], int] = {}
    for challenge in _as_list(challenges):
        if not isinstance(challenge, dict):
            continue
        cid = challenge.get("id") or challenge.get("challenge_id")
        cfg = challenge.get("config") or {}
        name = cfg.get("name") or challenge.get("name")
        aliases = {str(cid)} if cid else set()
        if name:
            aliases.add(str(name))
            aliases.add(str(name).split("_", 1)[0])
        canonical = _canonical_challenge(cid, name)
        if canonical:
            aliases.add(canonical)
        block_data = challenge.get("block_data") or {}
        qualities = block_data.get("qualifier_qualities_by_track") or {}
        if not isinstance(qualities, dict):
            continue
        for track, vals in qualities.items():
            nums = [int(x) for x in (vals or []) if isinstance(x, (int, float))]
            if not track or not nums:
                continue
            floor = min(nums)
            for alias in aliases:
                if alias:
                    floors[(str(alias), str(track))] = floor
    return floors


def _lookup_floor(
    floors: dict[tuple[str, str], int],
    challenge: str,
    track: str,
    algorithm_id: str | None = None,
) -> int | None:
    candidates = [
        challenge,
        _canonical_challenge(challenge, algorithm_id, _challenge_id_from_algorithm(algorithm_id)),
        _challenge_id_from_algorithm(algorithm_id),
    ]
    for cid in candidates:
        if cid and (cid, track) in floors:
            return floors[(cid, track)]
    matches = [v for (cid, t), v in floors.items() if t == track]
    if matches and len(set(matches)) == 1:
        return matches[0]
    return None


def _index_by_benchmark_id(items, id_key: str) -> dict:
    out = {}
    for item in _as_list(items):
        if not isinstance(item, dict):
            continue
        bid = item.get(id_key) or item.get("id") or item.get("benchmark_id")
        if bid:
            out[str(bid)] = item
    return out


def _fetch_json(url: str, timeout: int = 8) -> dict:
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "InnoPool-hit-rate/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310 — MASTER_URL / configured api_url
        return json.loads(resp.read())


def _fetch_tig_context() -> dict:
    global _TIG_CACHE, _TIG_CACHE_AT_MS
    now_ms = int(time.time() * 1000)
    if _TIG_CACHE is not None and now_ms - _TIG_CACHE_AT_MS < HIT_RATE_CACHE_MS:
        return _TIG_CACHE
    payload = {
        "config": {},
        "latest": {},
        "error": None,
        "block_height": None,
        "fetched_at_ms": now_ms,
    }
    try:
        payload["config"] = _fetch_json(f"{MASTER_URL}/get-config", timeout=5)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        payload["error"] = f"get-config: {exc}"
        logger.warning("hit-rate get-config failed: %s", exc)
    try:
        payload["latest"] = _fetch_json(f"{MASTER_URL}/get-latest-data", timeout=8)
        block = (payload["latest"] or {}).get("block") or {}
        details = block.get("details") or {}
        payload["block_height"] = details.get("height") or block.get("height")
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        err = f"get-latest-data: {exc}"
        payload["error"] = f"{payload['error']}; {err}" if payload["error"] else err
        logger.warning("hit-rate get-latest-data failed: %s", exc)
    latest = payload.get("latest") or {}
    api_url = str((payload.get("config") or {}).get("api_url") or "").rstrip("/")
    player_id = str((payload.get("config") or {}).get("player_id") or "").lower()
    need_floors = not _qualifier_floors(latest.get("challenges"))
    need_benchmarks = not latest.get("precommits") or not latest.get("proofs")
    if api_url and (need_floors or need_benchmarks):
        try:
            block = _fetch_json(f"{api_url}/get-block", timeout=8)
            block_id = (block.get("block") or {}).get("id")
            details = (block.get("block") or {}).get("details") or {}
            payload["block_height"] = payload["block_height"] or details.get("height")
            latest = dict(latest)
            if block_id and need_floors:
                challenges = _fetch_json(
                    f"{api_url}/get-challenges?block_id={block_id}",
                    timeout=8,
                )
                latest["challenges"] = challenges.get("challenges") or latest.get("challenges")
            if block_id and player_id and need_benchmarks:
                bms = _fetch_json(
                    f"{api_url}/get-benchmarks?block_id={block_id}&player_id={player_id}",
                    timeout=12,
                )
                latest["precommits"] = bms.get("precommits") or latest.get("precommits")
                latest["proofs"] = bms.get("proofs") or latest.get("proofs")
            payload["latest"] = latest
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            err = f"tig fallback: {exc}"
            payload["error"] = f"{payload['error']}; {err}" if payload["error"] else err
            logger.warning("hit-rate TIG fallback failed: %s", exc)
    _TIG_CACHE = payload
    _TIG_CACHE_AT_MS = now_ms
    return payload


def _job_rows(cutoff_ms: int) -> list[dict]:
    return db.fetch_all(
        """
        SELECT
            j.benchmark_id,
            j.challenge,
            j.algorithm,
            j.settings->>'track_id' AS track,
            j.settings->>'algorithm_id' AS algorithm_id,
            j.settings->>'challenge_id' AS challenge_id,
            j.num_nonces,
            j.block_started,
            j.start_time,
            j.end_time,
            j.proof_submit_time,
            j.benchmark_submit_time,
            j.proof_submitted,
            j.stopped,
            jd.solution_quality,
            jd.average_quality
        FROM job j
        LEFT JOIN job_data jd ON jd.benchmark_id = j.benchmark_id
        WHERE COALESCE(j.end_time, j.proof_submit_time, j.start_time, 0) >= %s
          AND jd.solution_quality IS NOT NULL
        ORDER BY COALESCE(j.end_time, j.proof_submit_time, j.start_time) DESC
        """,
        (cutoff_ms,),
    ) or []


def annotate_job(
    row: dict,
    *,
    floors: dict[tuple[str, str], int],
    configured: dict[tuple[str, str], int],
    precommits: dict,
    proofs: dict,
    nonces_per_bundle: dict[tuple[str, str], int] | None = None,
) -> dict:
    bid = str(row.get("benchmark_id") or "")
    algorithm_id = str(row.get("algorithm_id") or row.get("algorithm") or "")
    track = str(row.get("track") or "")
    challenge = _canonical_challenge(
        row.get("challenge_id"),
        algorithm_id,
        row.get("challenge"),
    )
    max_q = _max_quality(row.get("solution_quality"))
    floor = _lookup_floor(floors, challenge, track, algorithm_id)
    pre = precommits.get(bid) or {}
    pre_details = pre.get("details") or {}
    bundles = pre_details.get("num_bundles")
    if bundles is None:
        npp = (nonces_per_bundle or {}).get((challenge, track))
        bundles = _derive_bundles(row.get("num_nonces"), npp)
    # Never fall back to live config — that relabels old jobs after a bundle change.
    proof = proofs.get(bid) or {}
    proof_state = proof.get("state") or {}
    proof_details = proof.get("details") or {}
    block_started = row.get("block_started")
    proof_confirmed = proof_state.get("block_confirmed")
    blocks_to_proof = None
    if block_started is not None and proof_confirmed is not None:
        blocks_to_proof = int(proof_confirmed) - int(block_started)
    elif proof_details.get("submission_delay") is not None:
        blocks_to_proof = int(proof_details["submission_delay"])
    start_ms = row.get("start_time")
    end_ms = row.get("end_time")
    proof_ms = row.get("proof_submit_time")
    wall_sec = None
    proof_sec = None
    if start_ms and end_ms and int(end_ms) >= int(start_ms):
        wall_sec = (int(end_ms) - int(start_ms)) / 1000.0
    if start_ms and proof_ms and int(proof_ms) >= int(start_ms):
        proof_sec = (int(proof_ms) - int(start_ms)) / 1000.0
    hit = None
    gap = None
    if max_q is not None and floor is not None:
        hit = max_q >= floor
        gap = max_q - floor
    return {
        "benchmark_id": bid,
        "challenge": challenge,
        "algorithm_id": algorithm_id,
        "track": track,
        "num_bundles": int(bundles) if bundles is not None else None,
        "num_nonces": row.get("num_nonces"),
        "max_nonce_quality": max_q,
        "average_quality": row.get("average_quality"),
        "qualifier_floor": floor,
        "hit": hit,
        "gap": gap,
        "wall_clock_sec": wall_sec,
        "proof_wall_clock_sec": proof_sec,
        "blocks_to_proof": blocks_to_proof,
        "proof_submitted": bool(row.get("proof_submitted")),
        "stopped": bool(row.get("stopped")),
    }


def aggregate_rows(jobs: list[dict]) -> list[dict]:
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for job in jobs:
        key = (
            job.get("challenge") or "",
            job.get("algorithm_id") or "",
            job.get("track") or "",
            job.get("num_bundles"),
        )
        groups[key].append(job)
    out = []
    for (challenge, algorithm_id, track, bundles), rows in groups.items():
        maxqs = [r["max_nonce_quality"] for r in rows if r.get("max_nonce_quality") is not None]
        gaps = [r["gap"] for r in rows if r.get("gap") is not None]
        proved = [
            r for r in rows
            if r.get("blocks_to_proof") is not None or r.get("proof_submitted")
        ]
        walls = [
            r["proof_wall_clock_sec"] or r["wall_clock_sec"]
            for r in proved
            if (r.get("proof_wall_clock_sec") or r.get("wall_clock_sec")) is not None
        ]
        proofs = [r["proof_wall_clock_sec"] for r in rows if r.get("proof_wall_clock_sec") is not None]
        blocks = [r["blocks_to_proof"] for r in rows if r.get("blocks_to_proof") is not None]
        judged = [r for r in rows if r.get("hit") is not None]
        hits = sum(1 for r in judged if r.get("hit"))
        floor = next((r.get("qualifier_floor") for r in rows if r.get("qualifier_floor") is not None), None)
        out.append({
            "challenge": challenge,
            "algorithm_id": algorithm_id,
            "track": track,
            "num_bundles": bundles,
            "jobs": len(rows),
            "jobs_vs_floor": len(judged),
            "hits": hits,
            "hit_rate": (hits / len(judged)) if judged else None,
            "qualifier_floor": floor,
            "max_nonce_quality_mean": (sum(maxqs) / len(maxqs)) if maxqs else None,
            "max_nonce_quality_best": max(maxqs) if maxqs else None,
            "gap_mean": (sum(gaps) / len(gaps)) if gaps else None,
            "gap_best": max(gaps) if gaps else None,
            "wall_clock_sec_mean": (sum(walls) / len(walls)) if walls else None,
            "wall_clock_sec_p50": _median(walls),
            "wall_clock_sec_p90": _p90(walls),
            "proof_wall_clock_sec_p50": _median(proofs),
            "blocks_to_proof_p50": _median([float(x) for x in blocks]),
            "blocks_to_proof_p90": _p90([float(x) for x in blocks]),
        })
    out.sort(key=lambda r: (
        0 if str(r.get("challenge") or "").startswith("c00") and str(r.get("challenge")) in {"c004", "c005", "c006"} else 1,
        r.get("challenge") or "",
        r.get("track") or "",
        r.get("num_bundles") if r.get("num_bundles") is not None else 10**9,
        -(r.get("gap_best") if r.get("gap_best") is not None else -10**12),
    ))
    return out


def build_hit_rate_report(window_ms: int | None = None) -> dict:
    now_ms = int(time.time() * 1000)
    window_ms = int(window_ms or HIT_RATE_WINDOW_MS)
    cutoff_ms = now_ms - window_ms
    tig = _fetch_tig_context()
    latest = tig.get("latest") or {}
    floors = _qualifier_floors(latest.get("challenges"))
    configured = _configured_bundles(tig.get("config") or {})
    precommits = _index_by_benchmark_id(latest.get("precommits"), "benchmark_id")
    proofs = _index_by_benchmark_id(latest.get("proofs"), "benchmark_id")
    nonces_per_bundle = _nonces_per_bundle_from_precommits(latest.get("precommits"))
    raw_jobs = _job_rows(cutoff_ms)
    jobs = [
        annotate_job(
            row,
            floors=floors,
            configured=configured,
            precommits=precommits,
            proofs=proofs,
            nonces_per_bundle=nonces_per_bundle,
        )
        for row in raw_jobs
    ]
    tracks = aggregate_rows(jobs)
    judged = [j for j in jobs if j.get("hit") is not None]
    return _json_safe({
        "generated_at_ms": now_ms,
        "window_ms": window_ms,
        "block_height": tig.get("block_height"),
        "tig_error": tig.get("error"),
        "jobs_with_quality": len(jobs),
        "jobs_vs_floor": len(judged),
        "hits": sum(1 for j in judged if j.get("hit")),
        "hit_rate": (sum(1 for j in judged if j.get("hit")) / len(judged)) if judged else None,
        "tracks": tracks,
        "recent_jobs": jobs[:40],
    })
