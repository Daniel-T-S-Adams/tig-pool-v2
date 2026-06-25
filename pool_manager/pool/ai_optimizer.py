"""
Read-only AI pool optimizer.

The AI optimizer asks DeepSeek for a structured recommendation using stable
operator context plus live autopilot telemetry. It never applies changes itself;
recommendations are stored for review and future guarded execution.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import requests

from pool import autopilot
from pool import database as db

logger = logging.getLogger("pool.ai_optimizer")

AI_OPTIMIZER_MODE = os.environ.get("AI_OPTIMIZER_MODE", "off").lower()
AI_OPTIMIZER_INTERVAL_S = int(os.environ.get("AI_OPTIMIZER_INTERVAL_S", "600"))
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
DEEPSEEK_API_URL = os.environ.get(
    "DEEPSEEK_API_URL",
    "https://api.deepseek.com/chat/completions",
)
AI_CONTEXT_PATH = os.environ.get("AI_CONTEXT_PATH", "/app/docs/ai_pool_operator_context.md")
AI_OPTIMIZER_TIMEOUT_S = int(os.environ.get("AI_OPTIMIZER_TIMEOUT_S", "90"))
AI_OPTIMIZER_HISTORY_LIMIT = int(os.environ.get("AI_OPTIMIZER_HISTORY_LIMIT", "8"))
AI_OPTIMIZER_MAX_TOKENS = int(os.environ.get("AI_OPTIMIZER_MAX_TOKENS", "1800"))

_last_run_ts = 0.0
_decision_table_ready = False

KNOWN_SCHEMA = {
    "job": {
        "benchmark_id",
        "settings",
        "hyperparameters",
        "num_nonces",
        "num_batches",
        "rand_hash",
        "fuel_budget",
        "batch_size",
        "challenge",
        "algorithm",
        "download_url",
        "block_started",
        "start_time",
        "sampled_nonces",
        "merkle_root_ready",
        "merkle_proofs_ready",
        "stopped",
        "end_time",
    },
    "root_batch": {
        "benchmark_id",
        "batch_idx",
        "slave",
        "start_time",
        "end_time",
        "ready",
        "num_attempts",
    },
    "proofs_batch": {
        "benchmark_id",
        "batch_idx",
        "sampled_nonces",
        "slave",
        "start_time",
        "end_time",
        "ready",
        "num_attempts",
    },
    "benchmark_slot": {
        "slot_id",
        "slot_type",
        "benchmark_id",
        "challenge",
        "algorithm_id",
        "track_id",
        "assigned_at",
        "last_activity_at",
        "state",
    },
    "pool_members": {
        "slave_name",
        "wallet_address",
        "invite_code",
        "registered_at",
        "active",
        "notes",
        "fleet_id",
        "worker_type",
        "machine_index",
        "declared_cores",
        "declared_gpu_model",
    },
    "autopilot_decisions": {
        "id",
        "mode",
        "generated_at_ms",
        "clean_windows",
        "healthy",
        "applied",
        "reason",
        "changes",
        "report",
        "created_at",
    },
    "ai_optimizer_decisions": {
        "id",
        "mode",
        "generated_at_ms",
        "model",
        "status",
        "decision_category",
        "confidence",
        "summary",
        "recommendation",
        "raw_response",
        "prompt_context",
        "error",
        "created_at",
    },
}


def _ensure_decision_table():
    global _decision_table_ready
    if _decision_table_ready:
        return
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS ai_optimizer_decisions (
            id BIGSERIAL PRIMARY KEY,
            mode TEXT NOT NULL,
            generated_at_ms BIGINT NOT NULL,
            model TEXT,
            status TEXT NOT NULL,
            decision_category TEXT,
            confidence DOUBLE PRECISION,
            summary TEXT,
            recommendation JSONB NOT NULL DEFAULT '{}'::JSONB,
            raw_response TEXT,
            prompt_context JSONB NOT NULL DEFAULT '{}'::JSONB,
            error TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    _decision_table_ready = True


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _load_context() -> str:
    path = Path(AI_CONTEXT_PATH)
    if path.exists():
        return path.read_text(encoding="utf-8")

    fallback = Path(__file__).resolve().parents[2] / "docs" / "ai_pool_operator_context.md"
    if fallback.exists():
        return fallback.read_text(encoding="utf-8")

    raise FileNotFoundError(f"AI context document not found: {AI_CONTEXT_PATH}")


def _recent_ai_decisions() -> list[dict]:
    _ensure_decision_table()
    rows = db.fetch_all(
        """
        SELECT
            id,
            created_at,
            status,
            decision_category,
            confidence,
            summary,
            recommendation,
            error
        FROM ai_optimizer_decisions
        ORDER BY id DESC
        LIMIT %s
        """,
        (AI_OPTIMIZER_HISTORY_LIMIT,),
    )
    return [_json_safe(dict(r)) for r in rows]


def _recent_autopilot_decisions() -> list[dict]:
    try:
        rows = db.fetch_all(
            """
            SELECT
                id,
                created_at,
                reason,
                applied,
                clean_windows,
                changes
            FROM autopilot_decisions
            ORDER BY id DESC
            LIMIT %s
            """,
            (AI_OPTIMIZER_HISTORY_LIMIT,),
        )
        return [_json_safe(dict(r)) for r in rows]
    except Exception as exc:
        logger.info("recent autopilot decisions unavailable: %s", exc)
        return []


def _slot_state_counts(report: dict) -> dict:
    counts: dict[str, dict[str, int]] = {}
    for row in (report.get("slots") or {}).get("summary", []):
        slot_type = str(row.get("slot_type") or "unknown")
        state = str(row.get("state") or "unknown")
        counts.setdefault(slot_type, {})
        counts[slot_type][state] = counts[slot_type].get(state, 0) + int(row.get("count") or 0)
    return counts


def _slave_health_note(slave: dict) -> str:
    completed = int(slave.get("completed_recent") or 0)
    stale = int(slave.get("stale_roots") or 0) + int(slave.get("stale_proofs") or 0)
    live = int(slave.get("active_unfinished") or 0) + int(slave.get("active_proofs") or 0)
    if stale:
        return "attention: stale work exists"
    if completed >= 10 and live:
        return "healthy: completing work and still has live assignments"
    if completed >= 5:
        return "acceptable: recent completions present"
    if live:
        return "warming_or_slow: live assignments but low recent completions"
    return "idle_or_recently_quiet"


def _recommendation_signals(report: dict) -> list[dict]:
    signals = []
    for rec in report.get("recommendations") or []:
        key = rec.get("key")
        current = rec.get("current")
        signal = {
            "key": key,
            "current": current,
            "proposed": rec.get("proposed"),
            "reason": rec.get("reason"),
            "signals": rec.get("signals") or {},
        }
        if key == "proof_queue" and isinstance(current, dict):
            signal["stale_proofs"] = int(current.get("stale_proofs") or 0)
        if str(key or "").startswith("challenge_health.") and isinstance(current, dict):
            signal["stale_roots"] = int(current.get("stale_roots") or 0)
            signal["stale_proofs"] = int(current.get("stale_proofs") or 0)
        signals.append(signal)
    return signals


def _derived_pool_facts(report: dict) -> dict:
    gpu_slaves = []
    cpu_slaves = []
    exact_stale_totals = report.get("stale_totals") or {}
    stale_roots = int(exact_stale_totals.get("roots") or 0)
    stale_proofs = int(exact_stale_totals.get("proofs") or 0)
    for slave in report.get("slaves") or []:
        if not exact_stale_totals:
            stale_roots += int(slave.get("stale_roots") or 0)
            stale_proofs += int(slave.get("stale_proofs") or 0)
        if not slave.get("active_now"):
            continue
        item = {
            "slave_name": slave.get("slave_name"),
            "completed_recent": int(slave.get("completed_recent") or 0),
            "live_roots": int(slave.get("active_unfinished") or 0),
            "live_proofs": int(slave.get("active_proofs") or 0),
            "stale_total": int(slave.get("stale_roots") or 0) + int(slave.get("stale_proofs") or 0),
            "avg_runtime_sec": slave.get("avg_runtime_sec"),
            "idle_for_min": slave.get("idle_for_min"),
            "health_note": _slave_health_note(slave),
        }
        if slave.get("profile") == "gpu":
            gpu_slaves.append(item)
        elif slave.get("profile") == "cpu":
            cpu_slaves.append(item)

    if not exact_stale_totals:
        challenge_stale_roots = 0
        challenge_stale_proofs = 0
        for challenge in report.get("challenges") or []:
            challenge_stale_roots += int(challenge.get("stale_roots") or 0)
            challenge_stale_proofs += int(challenge.get("stale_proofs") or 0)
        stale_roots = max(stale_roots, challenge_stale_roots)
        stale_proofs = max(stale_proofs, challenge_stale_proofs)

    recommendation_signals = _recommendation_signals(report)
    stale_track_signals = [
        signal for signal in recommendation_signals
        if str(signal.get("key") or "").startswith("challenge_health.")
    ]
    if not exact_stale_totals:
        stale_roots = max(
            stale_roots,
            sum(int(signal.get("stale_roots") or 0) for signal in recommendation_signals),
        )
        stale_proofs = max(
            stale_proofs,
            sum(int(signal.get("stale_proofs") or 0) for signal in recommendation_signals),
        )

    safe_capacity_upscale = []
    for signal in recommendation_signals:
        key = signal.get("key")
        current = signal.get("current")
        proposed = signal.get("proposed")
        if key == "max_concurrent_benchmarks":
            try:
                if int(proposed) > int(current):
                    safe_capacity_upscale.append({
                        "key": key,
                        "current": current,
                        "proposed": proposed,
                    })
            except (TypeError, ValueError):
                pass
        elif key == "adaptive_slave_caps" and isinstance(current, dict) and isinstance(proposed, dict):
            cap_changes = {
                cap_key: {"current": current.get(cap_key), "proposed": proposed.get(cap_key)}
                for cap_key in ("cpu_max_cap", "gpu_max_cap")
                if int(proposed.get(cap_key) or 0) > int(current.get(cap_key) or 0)
            }
            if cap_changes:
                safe_capacity_upscale.append({
                    "key": key,
                    "changes": cap_changes,
                })
        elif key == "per_challenge_max_benchmarks" and isinstance(current, dict) and isinstance(proposed, dict):
            cap_changes = {
                challenge_id: {"current": current.get(challenge_id), "proposed": proposed.get(challenge_id)}
                for challenge_id, target in proposed.items()
                if int(target or 0) > int(current.get(challenge_id) or 0)
            }
            if cap_changes:
                safe_capacity_upscale.append({
                    "key": key,
                    "changes": cap_changes,
                })

    stale_roots_tolerated_for_capacity = (
        stale_roots <= autopilot.PRODUCTIVE_IDLE_STALE_ROOT_TOLERANCE
        and stale_proofs == 0
        and not (report.get("stranded_classification") or {}).get("unserved")
    )
    selective_challenge_upscale_allowed = (
        stale_proofs == 0
        and not (report.get("stranded_classification") or {}).get("unserved")
        and any(item.get("key") == "per_challenge_max_benchmarks" for item in safe_capacity_upscale)
    )

    return {
        "slot_state_counts": _slot_state_counts(report),
        "active_gpu_slaves": gpu_slaves,
        "active_cpu_slave_count": len(cpu_slaves),
        "stranded_classification": report.get("stranded_classification", {}),
        "stale_totals": {
            "roots": stale_roots,
            "proofs": stale_proofs,
            "combined": stale_roots + stale_proofs,
        },
        "stale_track_signals": stale_track_signals,
        "autopilot_recommendation_signals": recommendation_signals,
        "safe_capacity_upscale": safe_capacity_upscale,
        "stale_roots_tolerated_for_capacity_upscale": stale_roots_tolerated_for_capacity,
        "selective_challenge_upscale_allowed": selective_challenge_upscale_allowed,
        "productive_idle_stale_root_tolerance": autopilot.PRODUCTIVE_IDLE_STALE_ROOT_TOLERANCE,
        "interpretation_hints": [
            "Do not describe a GPU slave with completed_recent >= 10 and stale_total == 0 as low throughput.",
            "A C3 GPU dispatcher with live_roots > 0 and completed_recent >= 10 is healthy unless stale work exists.",
            "Occupied GPU slots plus active GPU slaves usually means observe unless stale work or idle live assignments grow.",
            "If stale_totals.proofs is greater than zero, never say there are no stale proofs.",
            "If autopilot_recommendation_signals includes proof_queue, mention it as a proof queue signal.",
            "If stranded_classification.capacity_waiting is non-empty and unserved is empty, describe it as queued behind saturated capacity, not broken.",
            "If safe_capacity_upscale is non-empty and stale_roots_tolerated_for_capacity_upscale is true, do not say autopilot is blocked by stale work.",
            "If selective_challenge_upscale_allowed is true, say autopilot can selectively raise non-stale challenge caps even while stale tracks are investigated.",
            "Use exact values from derived_pool_facts when summarizing throughput.",
        ],
    }


def _allowed_followup_checks() -> list[dict]:
    return [
        {
            "check_id": "admin_autopilot",
            "command": "python3 admin.py autopilot",
            "purpose": "Refresh the deterministic pool health report.",
        },
        {
            "check_id": "ai_optimizer_json",
            "command": "python3 admin.py ai-optimizer --json",
            "purpose": "Show the full AI recommendation and evidence.",
        },
        {
            "check_id": "gpu_slot_detail",
            "command": "docker compose exec -T db psql -U postgres -d innopool -c \"select slot_type, slot_id, left(benchmark_id, 10) as benchmark, challenge, track_id, algorithm_id, state, round((extract(epoch from now())*1000 - coalesce(last_activity_at, assigned_at))/60000.0::numeric, 1) as idle_min from benchmark_slot where slot_type in ('hypergraph','vector_search','neuralnet_optimizer') order by slot_type, slot_id;\"",
            "purpose": "Inspect GPU slot occupancy and slot age.",
        },
        {
            "check_id": "active_gpu_roots",
            "command": "docker compose exec -T db psql -U postgres -d innopool -c \"select j.challenge, j.settings->>'track_id' as track, j.settings->>'algorithm_id' as algorithm_id, count(*) as active_unassigned_roots from root_batch rb join job j on j.benchmark_id = rb.benchmark_id where rb.ready is null and rb.slave is null and j.stopped is null and j.end_time is null and j.challenge in ('hypergraph','vector_search','neuralnet_optimizer') group by j.challenge, j.settings->>'track_id', j.settings->>'algorithm_id' order by active_unassigned_roots desc;\"",
            "purpose": "Check whether usable active GPU roots exist.",
        },
        {
            "check_id": "unserved_gpu_stranded",
            "command": "docker compose exec -T db psql -U postgres -d innopool -c \"select left(j.benchmark_id, 10) as benchmark, j.challenge, j.settings->>'track_id' as track, j.settings->>'algorithm_id' as algorithm_id, count(rb.*) filter (where rb.ready is null) as pending_roots, count(rb.*) filter (where rb.ready is null and rb.slave is not null and rb.start_time is not null) as assigned_roots, bs.slot_id, bs.slot_type, bs.state from job j join root_batch rb on rb.benchmark_id = j.benchmark_id left join benchmark_slot bs on bs.benchmark_id = j.benchmark_id where j.stopped is null and j.end_time is null and j.merkle_root_ready is null and j.challenge in ('hypergraph','vector_search','neuralnet_optimizer') group by j.benchmark_id, j.challenge, j.settings, bs.slot_id, bs.slot_type, bs.state having count(rb.*) filter (where rb.ready is null) > 0 and count(rb.*) filter (where rb.ready is null and rb.slave is not null and rb.start_time is not null) = 0 order by j.challenge, track;\"",
            "purpose": "Inspect active GPU benchmarks that have pending roots but no assigned roots.",
        },
        {
            "check_id": "c3_master_logs",
            "command": "docker compose logs --tail=160 master | grep -Ei \"pool-gpu-a330c544ec5b-c3-001|get-batches|submitted root|adaptive cap\"",
            "purpose": "Verify C3 assignment, adaptive cap, and root submission activity.",
        },
    ]


def _build_prompt_payload(report: dict) -> dict:
    return {
        "generated_at_ms": int(time.time() * 1000),
        "mode": AI_OPTIMIZER_MODE,
        "autopilot_report": report,
        "derived_pool_facts": _derived_pool_facts(report),
        "known_database_schema": {table: sorted(cols) for table, cols in KNOWN_SCHEMA.items()},
        "allowed_followup_checks": _allowed_followup_checks(),
        "recent_autopilot_decisions": _recent_autopilot_decisions(),
        "recent_ai_optimizer_decisions": _recent_ai_decisions(),
        "instructions": {
            "output": "Return strict JSON only. Follow the schema in the context document.",
            "apply_policy": "Read-only analysis. Do not claim any change has been applied.",
            "if_uncertain": "Use request_more_data or observe_only.",
            "sql_policy": "Do not invent SQL. Prefer allowed_followup_checks check_id values. If SQL is included, it must use only known_database_schema tables and columns.",
            "accuracy_policy": "Use derived_pool_facts for throughput statements; do not call healthy GPU completion counts low throughput.",
        },
    }


def _strip_json_fences(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    return stripped


def _parse_model_json(text: str) -> dict:
    try:
        parsed = json.loads(_strip_json_fences(text))
    except json.JSONDecodeError as exc:
        parsed = {
            "schema_version": 1,
            "decision_category": "investigate",
            "summary": "AI model returned malformed JSON; deterministic autopilot facts were used for fallback actions.",
            "confidence": 0.0,
            "evidence": [],
            "recommended_actions": [],
            "blocked_actions": [],
            "queries_to_run_next": [{"check_id": "admin_autopilot", "purpose": "Refresh deterministic autopilot report."}],
            "requires_human_approval": False,
            "parse_warning": {
                "error": str(exc),
                "raw_prefix": text[:500],
            },
        }
    if not isinstance(parsed, dict):
        raise ValueError("model response JSON must be an object")
    parsed.setdefault("schema_version", 1)
    parsed.setdefault("decision_category", "observe_only")
    parsed.setdefault("summary", "")
    parsed.setdefault("confidence", 0.0)
    parsed.setdefault("recommended_actions", [])
    parsed.setdefault("requires_human_approval", False)
    _sanitize_followup_queries(parsed)
    return parsed


def _ensure_evidence_metric(recommendation: dict, metric: str, value: int, interpretation: str):
    evidence = recommendation.setdefault("evidence", [])
    for item in evidence:
        if isinstance(item, dict) and item.get("metric") == metric:
            item["value"] = value
            item["interpretation"] = interpretation
            return
    evidence.append({
        "metric": metric,
        "value": value,
        "interpretation": interpretation,
    })


def _enforce_recommendation_consistency(recommendation: dict, prompt_context: dict):
    """Correct AI text that contradicts deterministic derived facts."""
    derived = prompt_context.get("derived_pool_facts") or {}
    stale_totals = derived.get("stale_totals") or {}
    stranded = derived.get("stranded_classification") or {}
    stale_roots = int(stale_totals.get("roots") or 0)
    stale_proofs = int(stale_totals.get("proofs") or 0)
    unserved_count = len(stranded.get("unserved") or [])
    capacity_waiting_count = len(stranded.get("capacity_waiting") or [])
    safe_capacity_upscale = derived.get("safe_capacity_upscale") or []
    stale_track_signals = derived.get("stale_track_signals") or []
    stale_tolerated_for_capacity = bool(derived.get("stale_roots_tolerated_for_capacity_upscale"))
    selective_challenge_upscale_allowed = bool(derived.get("selective_challenge_upscale_allowed"))
    unserved_gpu_stranded = [
        item for item in stranded.get("unserved") or []
        if item.get("capacity_profile") == "gpu"
    ]
    warnings = []

    _ensure_evidence_metric(
        recommendation,
        "deterministic_stale_roots",
        stale_roots,
        f"Deterministic derived stale root total is {stale_roots}.",
    )
    _ensure_evidence_metric(
        recommendation,
        "deterministic_stale_proofs",
        stale_proofs,
        f"Deterministic derived stale proof total is {stale_proofs}.",
    )
    _ensure_evidence_metric(
        recommendation,
        "deterministic_unserved_stranded",
        unserved_count,
        f"Deterministic unserved stranded benchmark count is {unserved_count}.",
    )
    _ensure_evidence_metric(
        recommendation,
        "deterministic_capacity_waiting",
        capacity_waiting_count,
        f"Deterministic capacity-waiting benchmark count is {capacity_waiting_count}.",
    )
    if safe_capacity_upscale:
        _ensure_evidence_metric(
            recommendation,
            "deterministic_safe_capacity_upscale",
            len(safe_capacity_upscale),
            (
                "Deterministic autopilot has safe capacity-upscale recommendations. "
                f"Stale roots tolerated for capacity upscale: {stale_tolerated_for_capacity}."
            ),
        )
    if stale_track_signals:
        _ensure_evidence_metric(
            recommendation,
            "deterministic_stale_track_signals",
            len(stale_track_signals),
            "Deterministic autopilot reported stale active tracks that need investigation or drain handling.",
        )

        deduped_stale_tracks = []
        seen_stale_track_keys = set()
        for signal in stale_track_signals:
            key = signal.get("key")
            if key in seen_stale_track_keys:
                continue
            seen_stale_track_keys.add(key)
            deduped_stale_tracks.append(signal)

        actions = recommendation.setdefault("recommended_actions", [])
        recommendation["recommended_actions"] = [
            action for action in actions
            if not (
                isinstance(action, dict)
                and action.get("key") == "challenge_health"
                and action.get("action_type") in {"no_op", "investigate", "stale_track_attention"}
            )
        ]
        recommendation["recommended_actions"].append({
            "action_type": "stale_track_attention",
            "key": "challenge_health",
            "current": deduped_stale_tracks[:10],
            "proposed": "investigate_or_wait_for_stale_cleanup",
            "reason": "Active tracks have stale unfinished root work; broad capacity increases should wait, but stale-free challenge caps may still be raised selectively.",
            "risk": "Ignoring stale roots can keep weak or stuck slaves holding work and distort capacity estimates.",
            "rollback_condition": "If stale roots return to zero and workers remain idle, resume normal capacity scaling.",
        })
        if recommendation.get("decision_category") == "observe_only":
            recommendation["decision_category"] = "investigate"
        warnings.append({
            "field": "recommended_actions",
            "reason": "normalized_stale_track_action",
            "stale_track_count": len(deduped_stale_tracks),
        })
    if unserved_gpu_stranded:
        actions = recommendation.setdefault("recommended_actions", [])
        actions = [
            action for action in actions
            if not (
                isinstance(action, dict)
                and action.get("key") == "stranded_classification.unserved_gpu"
            )
        ]
        actions.append({
            "action_type": "gpu_unserved_stranded_attention",
            "key": "stranded_classification.unserved_gpu",
            "current": unserved_gpu_stranded[:10],
            "proposed": "inspect_gpu_slot_assignment_and_route_caps",
            "reason": "Active GPU benchmarks have pending roots but no assigned roots even though matching GPU slot capacity appears available.",
            "risk": "GPU work can starve while C3/local GPU capacity polls for batches and receives none.",
            "rollback_condition": "If unserved GPU stranded count returns to zero, resume normal GPU capacity scaling.",
        })
        recommendation["recommended_actions"] = actions
        if recommendation.get("decision_category") == "observe_only":
            recommendation["decision_category"] = "investigate"
        warnings.append({
            "field": "recommended_actions",
            "reason": "added_unserved_gpu_stranded_action",
            "unserved_gpu_count": len(unserved_gpu_stranded),
        })

    summary = str(recommendation.get("summary") or "")
    if stale_track_signals and summary:
        summary = (
            summary
            .replace(
                "Pool is stable",
                "Pool has active capacity, but stale root pressure needs investigation",
            )
            .replace(
                "pool is stable",
                "pool has active capacity, but stale root pressure needs investigation",
            )
            .replace(
                "No safe config change is recommended now.",
                "No broad capacity change is recommended until stale tracks clear; stale-track investigation or cleanup is recommended.",
            )
            .replace(
                "no safe config change is recommended now.",
                "no broad capacity change is recommended until stale tracks clear; stale-track investigation or cleanup is recommended.",
            )
        )
        recommendation["summary"] = summary
        warnings.append({
            "field": "summary",
            "reason": "corrected_stale_track_observe_only_wording",
            "stale_track_count": len(stale_track_signals),
        })
    if stale_proofs > 0 and "no stale proofs" in summary.lower():
        recommendation["summary"] = (
            summary.rstrip(".")
            + f". Deterministic check: stale_proofs={stale_proofs}, so proof queue should be monitored."
        )
        warnings.append({
            "field": "summary",
            "reason": "model_claimed_no_stale_proofs_but_derived_total_is_positive",
            "derived_stale_proofs": stale_proofs,
        })
    if unserved_count == 0 and capacity_waiting_count > 0 and "blocked by stranded" in summary.lower():
        recommendation["summary"] = (
            summary
            .replace(
                "Autopilot is blocked by stranded benchmarks at drain target.",
                f"Deterministic classification shows 0 unserved stranded benchmarks and {capacity_waiting_count} benchmarks waiting behind saturated capacity.",
            )
            .replace(
                "autopilot is blocked by stranded benchmarks at drain target.",
                f"deterministic classification shows 0 unserved stranded benchmarks and {capacity_waiting_count} benchmarks waiting behind saturated capacity.",
            )
        )
        warnings.append({
            "field": "summary",
            "reason": "model_called_capacity_waiting_stranded_blocked",
            "unserved": unserved_count,
            "capacity_waiting": capacity_waiting_count,
        })
    if safe_capacity_upscale and stale_tolerated_for_capacity and "blocked by stale work" in summary.lower():
        recommendation["summary"] = (
            summary
            .replace(
                "Autopilot is blocked by stale work.",
                "Deterministic autopilot is not blocked: stale roots are within the productive-capacity tolerance and there are no stale proofs or unserved stranded benchmarks.",
            )
            .replace(
                "autopilot is blocked by stale work.",
                "deterministic autopilot is not blocked: stale roots are within the productive-capacity tolerance and there are no stale proofs or unserved stranded benchmarks.",
            )
        )
        warnings.append({
            "field": "summary",
            "reason": "model_called_tolerated_stale_roots_blocking",
            "safe_capacity_upscale": safe_capacity_upscale,
            "stale_roots": stale_roots,
            "stale_proofs": stale_proofs,
        })
    elif selective_challenge_upscale_allowed and "blocked by stale work" in summary.lower():
        recommendation["summary"] = (
            summary
            .replace(
                "Autopilot is blocked by stale work.",
                "Autopilot should hold broad capacity increases, but can selectively raise non-stale CPU challenge caps while stale tracks are investigated.",
            )
            .replace(
                "autopilot is blocked by stale work.",
                "autopilot should hold broad capacity increases, but can selectively raise non-stale CPU challenge caps while stale tracks are investigated.",
            )
        )
        warnings.append({
            "field": "summary",
            "reason": "model_called_selective_challenge_upscale_globally_blocked",
            "safe_capacity_upscale": safe_capacity_upscale,
            "stale_roots": stale_roots,
        })

    for action in recommendation.get("blocked_actions") or []:
        if not isinstance(action, dict):
            continue
        reason = str(action.get("reason") or "")
        if unserved_count == 0 and capacity_waiting_count > 0 and "stranded benchmarks" in reason.lower():
            action["reason"] = (
                f"Not recommended from current evidence: deterministic classification shows "
                f"0 unserved stranded benchmarks and {capacity_waiting_count} benchmarks "
                "waiting behind saturated capacity."
            )
            warnings.append({
                "field": f"blocked_actions.{action.get('key')}",
                "reason": "model_called_capacity_waiting_stranded_blocked",
                "unserved": unserved_count,
                "capacity_waiting": capacity_waiting_count,
            })
        if safe_capacity_upscale and stale_tolerated_for_capacity and "stale work" in reason.lower():
            action["reason"] = (
                "Deterministic autopilot does not treat the current stale roots as blocking: "
                "stale roots are within productive-capacity tolerance, stale proofs are zero, "
                "and unserved stranded benchmarks are zero."
            )
            warnings.append({
                "field": f"blocked_actions.{action.get('key')}",
                "reason": "model_blocked_safe_upscale_due_to_tolerated_stale_roots",
                "safe_capacity_upscale": safe_capacity_upscale,
                "stale_roots": stale_roots,
            })
        elif selective_challenge_upscale_allowed and "stale work" in reason.lower():
            action["reason"] = (
                "Broad capacity increases should wait, but deterministic autopilot can selectively "
                "raise non-stale CPU challenge caps because stale proofs and unserved stranded "
                "benchmarks are zero."
            )
            warnings.append({
                "field": f"blocked_actions.{action.get('key')}",
                "reason": "model_blocked_selective_challenge_upscale_due_to_stale_roots",
                "safe_capacity_upscale": safe_capacity_upscale,
                "stale_roots": stale_roots,
            })

    for item in recommendation.get("evidence") or []:
        if not isinstance(item, dict):
            continue
        metric = str(item.get("metric") or "")
        text = f"{item.get('value', '')} {item.get('interpretation', '')}".lower()
        if metric in {"stale_proofs", "proof_queue"} and stale_proofs > 0 and "no stale proofs" in text:
            item["value"] = stale_proofs
            item["interpretation"] = (
                f"Deterministic derived stale proof total is {stale_proofs}; "
                "proof_queue should be monitored."
            )
            warnings.append({
                "field": f"evidence.{metric}",
                "reason": "model_claimed_no_stale_proofs_but_derived_total_is_positive",
                "derived_stale_proofs": stale_proofs,
            })
        if metric == "stale_roots":
            item["value"] = stale_roots
        if metric == "stale_proofs":
            item["value"] = stale_proofs
        if metric in {"autopilot_blocked", "stranded_benchmarks"} and unserved_count == 0 and capacity_waiting_count > 0:
            item["value"] = {
                "unserved": unserved_count,
                "capacity_waiting": capacity_waiting_count,
            }
            item["interpretation"] = (
                f"Deterministic classification shows no unserved stranded benchmarks; "
                f"{capacity_waiting_count} benchmarks are waiting behind saturated capacity."
            )
            warnings.append({
                "field": f"evidence.{metric}",
                "reason": "model_called_capacity_waiting_stranded_blocked",
                "unserved": unserved_count,
                "capacity_waiting": capacity_waiting_count,
            })

    if warnings:
        recommendation["deterministic_consistency_warnings"] = warnings


def _validate_sql_query(sql: str) -> list[str]:
    import re

    errors = []
    alias_to_table = {}
    for table, alias in re.findall(r"\b(?:from|join)\s+([a-zA-Z_][a-zA-Z0-9_]*)\s+([a-zA-Z_][a-zA-Z0-9_]*)", sql, flags=re.I):
        if table not in KNOWN_SCHEMA:
            errors.append(f"unknown table '{table}'")
            continue
        alias_to_table[alias] = table
    for table in re.findall(r"\b(?:from|join)\s+([a-zA-Z_][a-zA-Z0-9_]*)\b", sql, flags=re.I):
        if table not in KNOWN_SCHEMA and table not in alias_to_table:
            errors.append(f"unknown table '{table}'")
    for alias, column in re.findall(r"\b([a-zA-Z_][a-zA-Z0-9_]*)\.([a-zA-Z_][a-zA-Z0-9_]*)\b", sql):
        table = alias_to_table.get(alias)
        if not table:
            continue
        if column not in KNOWN_SCHEMA[table]:
            errors.append(f"unknown column '{alias}.{column}' for table '{table}'")
    return sorted(set(errors))


def _sanitize_followup_queries(recommendation: dict):
    queries = recommendation.get("queries_to_run_next")
    if not isinstance(queries, list):
        recommendation["queries_to_run_next"] = []
        return
    warnings = []
    sanitized = []
    for query in queries:
        if not isinstance(query, dict):
            continue
        sql = query.get("sql")
        if not sql:
            sanitized.append(query)
            continue
        errors = _validate_sql_query(str(sql))
        if errors:
            warnings.append({
                "purpose": query.get("purpose"),
                "rejected_sql": sql,
                "errors": errors,
            })
            sanitized.append({
                "purpose": query.get("purpose"),
                "status": "rejected_invalid_sql",
                "reason": "; ".join(errors),
                "suggestion": "Use allowed_followup_checks instead of invented SQL.",
            })
        else:
            sanitized.append(query)
    recommendation["queries_to_run_next"] = sanitized
    if warnings:
        recommendation["query_validation_warnings"] = warnings


def _call_deepseek(context_doc: str, payload: dict) -> tuple[dict, str]:
    if not DEEPSEEK_API_KEY:
        raise RuntimeError("DEEPSEEK_API_KEY is not configured")

    messages = [
        {
            "role": "system",
            "content": context_doc,
        },
        {
            "role": "user",
            "content": (
                "Analyze this live InnoPool telemetry and return strict JSON only.\n\n"
                + json.dumps(payload, sort_keys=True)
            ),
        },
    ]
    body = {
        "model": DEEPSEEK_MODEL,
        "messages": messages,
        "temperature": 0.1,
        "max_tokens": AI_OPTIMIZER_MAX_TOKENS,
        "response_format": {"type": "json_object"},
    }
    resp = requests.post(
        DEEPSEEK_API_URL,
        headers={
            "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
            "Content-Type": "application/json",
        },
        json=body,
        timeout=AI_OPTIMIZER_TIMEOUT_S,
    )
    resp.raise_for_status()
    data = resp.json()
    raw = data["choices"][0]["message"]["content"]
    return _parse_model_json(raw), raw


def _save_decision(
    *,
    status: str,
    recommendation: dict | None,
    prompt_context: dict,
    raw_response: str = "",
    error: str = "",
):
    _ensure_decision_table()
    recommendation = recommendation or {}
    db.execute(
        """
        INSERT INTO ai_optimizer_decisions (
            mode,
            generated_at_ms,
            model,
            status,
            decision_category,
            confidence,
            summary,
            recommendation,
            raw_response,
            prompt_context,
            error
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s::JSONB, %s, %s::JSONB, %s)
        """,
        (
            AI_OPTIMIZER_MODE,
            int(time.time() * 1000),
            DEEPSEEK_MODEL,
            status,
            recommendation.get("decision_category"),
            float(recommendation.get("confidence") or 0),
            recommendation.get("summary"),
            json.dumps(recommendation),
            raw_response,
            json.dumps(prompt_context),
            error,
        ),
    )


def run_once(force: bool = False) -> dict:
    """Run one read-only AI recommendation cycle and store the result."""
    _ensure_decision_table()
    if AI_OPTIMIZER_MODE not in {"report", "off"}:
        return {"status": "skipped", "reason": f"unsupported_mode:{AI_OPTIMIZER_MODE}"}
    if AI_OPTIMIZER_MODE == "off" and not force:
        return {"status": "skipped", "reason": "mode_off"}

    prompt_context: dict = {}
    try:
        context_doc = _load_context()
        report = autopilot.build_report()
        prompt_context = _build_prompt_payload(report)
        recommendation, raw = _call_deepseek(context_doc, prompt_context)
        _enforce_recommendation_consistency(recommendation, prompt_context)
        _save_decision(
            status="ok",
            recommendation=recommendation,
            prompt_context={
                "generated_at_ms": prompt_context["generated_at_ms"],
                "autopilot_generated_at_ms": report.get("generated_at_ms"),
                "recent_autopilot_decisions": len(prompt_context["recent_autopilot_decisions"]),
                "recent_ai_optimizer_decisions": len(prompt_context["recent_ai_optimizer_decisions"]),
            },
            raw_response=raw,
        )
        logger.info(
            "ai_optimizer status=ok category=%s confidence=%s summary=%s",
            recommendation.get("decision_category"),
            recommendation.get("confidence"),
            recommendation.get("summary"),
        )
        return {"status": "ok", "recommendation": recommendation}
    except Exception as exc:
        error = str(exc)
        _save_decision(
            status="error",
            recommendation={"decision_category": "observe_only", "summary": "AI optimizer failed."},
            prompt_context=prompt_context,
            error=error,
        )
        logger.error("ai_optimizer error: %s", error)
        return {"status": "error", "error": error}


def maybe_run():
    global _last_run_ts
    if AI_OPTIMIZER_MODE != "report":
        return None
    now = time.time()
    if now - _last_run_ts < AI_OPTIMIZER_INTERVAL_S:
        return None
    _last_run_ts = now
    return run_once()


def latest(limit: int = 10) -> list[dict]:
    _ensure_decision_table()
    rows = db.fetch_all(
        """
        SELECT
            id,
            mode,
            created_at,
            model,
            status,
            decision_category,
            confidence,
            summary,
            recommendation,
            error
        FROM ai_optimizer_decisions
        ORDER BY id DESC
        LIMIT %s
        """,
        (max(1, min(50, int(limit or 10))),),
    )
    return [_json_safe(dict(r)) for r in rows]
