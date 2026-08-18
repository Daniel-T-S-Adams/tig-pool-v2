#!/usr/bin/env python3
"""Observe-only pool health monitor for InnoPool ops metrics.

Samples /api/admin/ops/metrics on an interval, appends JSONL, and writes a
human summary + verdict (helping / hurting / mixed / neutral) so you can leave
it running while testing adaptive create/batch changes.

Examples (on VPS):

  set -a; source .env; set +a
  python3 tools/monitor_pool_health.py --interval 60 --duration-hours 8

  # background
  nohup python3 tools/monitor_pool_health.py --interval 60 --duration-hours 12 \\
    > /tmp/pool_health_monitor.log 2>&1 &

  # later
  python3 tools/monitor_pool_health.py --summary-only
  # human events: logs/pool_health/events.txt
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


DEFAULT_OUT_DIR = Path("logs/pool_health")
DEFAULT_URL = "http://127.0.0.1:{port}/api/admin/ops/metrics"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip("'").strip('"')
        if key and key not in os.environ:
            os.environ[key] = val


def fetch_metrics(url: str, secret: str, timeout: float = 20.0) -> Dict[str, Any]:
    req = urllib.request.Request(
        url,
        headers={
            "X-Admin-Secret": secret,
            "Accept": "application/json",
            "User-Agent": "innopool-pool-health-monitor/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def sample_from_metrics(d: Dict[str, Any]) -> Dict[str, Any]:
    slaves = d.get("slaves") or {}
    by = slaves.get("by_profile") or {}
    cpu = by.get("cpu") or {}
    gpu = by.get("gpu") or {}
    gov = d.get("governor") or {}
    counts = gov.get("counts") or {}
    caps = gov.get("caps") or {}
    creates = d.get("creates") or {}
    finishes = d.get("finishes") or {}
    blocks = gov.get("profile_blocks") or {}

    online = int(slaves.get("online") or 0)
    idle = int(slaves.get("idle") or 0)
    busy = int(slaves.get("busy") or 0)
    sustained_idle = int(slaves.get("sustained_idle") or 0)
    mean_idle_frac_window = slaves.get("mean_idle_frac_window")
    fill = slaves.get("fill_rate")
    cpu_online = int(cpu.get("online") or 0)
    cpu_idle = int(cpu.get("idle") or 0)
    cpu_sustained_idle = int(cpu.get("sustained_idle") or 0)
    cpu_mean_idle_frac_window = cpu.get("mean_idle_frac_window")
    cpu_fill = cpu.get("fill_rate")
    gpu_fill = gpu.get("fill_rate")
    claimable = int(d.get("claimable_root_total") or 0)
    sticky = int(d.get("sticky_reserved_root_total") or 0)
    unassigned = int(d.get("unassigned_root_total") or 0)
    creates_15m = int(creates.get("creates_15m") or 0)
    roots_done_15m = int(finishes.get("roots_done_15m") or 0)
    gov_counts = counts

    idle_frac = (idle / online) if online > 0 else None
    cpu_idle_frac = (cpu_idle / cpu_online) if cpu_online > 0 else None
    sustained_idle_frac = (sustained_idle / online) if online > 0 else None
    cpu_sustained_idle_frac = (
        (cpu_sustained_idle / cpu_online) if cpu_online > 0 else None
    )
    sticky_frac = (sticky / unassigned) if unassigned > 0 else 0.0

    return {
        "ts": _now_iso(),
        "epoch": int(time.time()),
        "online": online,
        "busy": busy,
        "idle": idle,
        "idle_frac": idle_frac,
        "sustained_idle": sustained_idle,
        "sustained_idle_frac": sustained_idle_frac,
        "mean_idle_frac_window": mean_idle_frac_window,
        "fill_rate": fill,
        "cpu_online": cpu_online,
        "cpu_busy": int(cpu.get("busy") or 0),
        "cpu_idle": cpu_idle,
        "cpu_idle_frac": cpu_idle_frac,
        "cpu_sustained_idle": cpu_sustained_idle,
        "cpu_sustained_idle_frac": cpu_sustained_idle_frac,
        "cpu_mean_idle_frac_window": cpu_mean_idle_frac_window,
        "cpu_fill": cpu_fill,
        "gpu_online": int(gpu.get("online") or 0),
        "gpu_busy": int(gpu.get("busy") or 0),
        "gpu_idle": int(gpu.get("idle") or 0),
        "gpu_fill": gpu_fill,
        "unassigned": unassigned,
        "claimable": claimable,
        "sticky": sticky,
        "sticky_frac": sticky_frac,
        "oldest_unassigned_min": d.get("oldest_unassigned_root_age_min"),
        "creates_15m": creates_15m,
        "roots_done_15m": roots_done_15m,
        "idle_cpu_needs_work": bool(gov.get("idle_cpu_needs_work")),
        "cpu_profile_blocked": bool(blocks.get("cpu")),
        "gpu_profile_blocked": bool(blocks.get("gpu")),
        "cpu_reasons": list(blocks.get("cpu_reasons") or []),
        "gpu_reasons": list(blocks.get("gpu_reasons") or []),
        "cpu_unassigned_cap": caps.get("cpu_unassigned_cap"),
        "cpu_unassigned_claimable_gov": counts.get("cpu_unassigned_claimable"),
        "online_idle_cpu_slaves_gov": gov_counts.get("online_idle_cpu_slaves"),
        "sustained_idle_cpu_slaves_gov": gov_counts.get("sustained_idle_cpu_slaves"),
        "open_jobs": gov_counts.get("open_jobs"),
        "max_concurrent": gov_counts.get("max_concurrent_benchmarks"),
        "block_reasons": list(gov.get("block_reasons") or []),
        "would_block_global": bool(gov.get("would_block_global")),
        "at_max_concurrent": bool(gov.get("at_max_concurrent")),
        "stopped_15m": int(creates.get("created_stopped_15m") or 0),
    }


_SQL_SNAPSHOT = r"""
SELECT json_build_object(
  'cpu_root_phase', (
    SELECT COUNT(*) FROM job
    WHERE stopped IS NULL AND end_time IS NULL AND merkle_root_ready IS NULL
      AND settings->>'challenge_id' IN ('c001','c002','c003','c007','c008')
  ),
  'cpu_proof_phase', (
    SELECT COUNT(*) FROM job
    WHERE stopped IS NULL AND end_time IS NULL
      AND merkle_root_ready IS TRUE AND merkle_proofs_ready IS NULL
      AND settings->>'challenge_id' IN ('c001','c002','c003','c007','c008')
  ),
  'gpu_root_phase', (
    SELECT COUNT(*) FROM job
    WHERE stopped IS NULL AND end_time IS NULL AND merkle_root_ready IS NULL
      AND settings->>'challenge_id' IN ('c004','c005','c006')
  ),
  'gpu_proof_phase', (
    SELECT COUNT(*) FROM job
    WHERE stopped IS NULL AND end_time IS NULL
      AND merkle_root_ready IS TRUE AND merkle_proofs_ready IS NULL
      AND settings->>'challenge_id' IN ('c004','c005','c006')
  ),
  'unowned_cpu', (
    SELECT COUNT(*) FROM job j
    WHERE j.stopped IS NULL AND j.end_time IS NULL AND j.merkle_root_ready IS NULL
      AND j.settings->>'challenge_id' IN ('c001','c002','c003','c007','c008')
      AND NOT EXISTS (
        SELECT 1 FROM root_batch rb
        WHERE rb.benchmark_id = j.benchmark_id AND rb.slave IS NOT NULL
      )
  ),
  'unowned_gpu', (
    SELECT COUNT(*) FROM job j
    WHERE j.stopped IS NULL AND j.end_time IS NULL AND j.merkle_root_ready IS NULL
      AND j.settings->>'challenge_id' IN ('c004','c005','c006')
      AND NOT EXISTS (
        SELECT 1 FROM root_batch rb
        WHERE rb.benchmark_id = j.benchmark_id AND rb.slave IS NOT NULL
      )
  ),
  'proved_1h_n', (
    SELECT COUNT(*) FROM job
    WHERE merkle_proofs_ready IS TRUE AND end_time IS NOT NULL
      AND start_time >= (EXTRACT(EPOCH FROM NOW())*1000 - 3600000)
  ),
  'proved_1h_p50_min', (
    SELECT ROUND(PERCENTILE_CONT(0.50) WITHIN GROUP (
      ORDER BY (end_time - start_time)/60000.0)::numeric, 1)
    FROM job
    WHERE merkle_proofs_ready IS TRUE AND end_time IS NOT NULL
      AND start_time >= (EXTRACT(EPOCH FROM NOW())*1000 - 3600000)
  ),
  'proved_1h_p90_min', (
    SELECT ROUND(PERCENTILE_CONT(0.90) WITHIN GROUP (
      ORDER BY (end_time - start_time)/60000.0)::numeric, 1)
    FROM job
    WHERE merkle_proofs_ready IS TRUE AND end_time IS NOT NULL
      AND start_time >= (EXTRACT(EPOCH FROM NOW())*1000 - 3600000)
  ),
  'open_p50_age_min', (
    SELECT ROUND(PERCENTILE_CONT(0.50) WITHIN GROUP (
      ORDER BY (EXTRACT(EPOCH FROM NOW())*1000 - start_time)/60000.0)::numeric, 1)
    FROM job
    WHERE COALESCE(stopped,false) = false AND end_time IS NULL
  ),
  'open_max_age_min', (
    SELECT ROUND(MAX((EXTRACT(EPOCH FROM NOW())*1000 - start_time)/60000.0)::numeric, 1)
    FROM job
    WHERE COALESCE(stopped,false) = false AND end_time IS NULL
  )
);
"""


def keep_ahead_want(proving: int, online: int) -> int:
    raw = max(0, int(proving or 0))
    online_n = max(0, int(online or 0))
    if online_n > 0:
        return min(raw, online_n)
    return raw


def fetch_sql_snapshot(compose_dir: Path, timeout: float = 25.0) -> Dict[str, Any]:
    """Cheap extra counts the ops JSON does not expose yet."""
    proc = subprocess.run(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "db",
            "psql",
            "-U",
            "postgres",
            "-d",
            "innopool",
            "-Atqc",
            _SQL_SNAPSHOT,
        ],
        cwd=str(compose_dir),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "psql failed").strip()[:400])
    line = (proc.stdout or "").strip().splitlines()
    if not line:
        raise RuntimeError("psql returned no snapshot")
    data = json.loads(line[-1])
    if not isinstance(data, dict):
        raise RuntimeError("psql snapshot was not an object")
    return data


def merge_sql_snapshot(sample: Dict[str, Any], snap: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    out = dict(sample)
    if not snap:
        out["sql_ok"] = False
        return out
    out["sql_ok"] = True
    out["cpu_root_phase"] = int(snap.get("cpu_root_phase") or 0)
    out["cpu_proof_phase"] = int(snap.get("cpu_proof_phase") or 0)
    out["gpu_root_phase"] = int(snap.get("gpu_root_phase") or 0)
    out["gpu_proof_phase"] = int(snap.get("gpu_proof_phase") or 0)
    out["unowned_cpu"] = int(snap.get("unowned_cpu") or 0)
    out["unowned_gpu"] = int(snap.get("unowned_gpu") or 0)
    out["proved_1h_n"] = int(snap.get("proved_1h_n") or 0)
    out["proved_1h_p50_min"] = snap.get("proved_1h_p50_min")
    out["proved_1h_p90_min"] = snap.get("proved_1h_p90_min")
    out["open_p50_age_min"] = snap.get("open_p50_age_min")
    out["open_max_age_min"] = snap.get("open_max_age_min")
    out["want_cpu"] = keep_ahead_want(out["cpu_proof_phase"], int(out.get("cpu_online") or 0))
    out["want_gpu"] = keep_ahead_want(out["gpu_proof_phase"], int(out.get("gpu_online") or 0))
    return out


def _clock(ts: str) -> str:
    if not ts or "T" not in ts:
        return ts or "?"
    return ts[11:16] + " UTC"


def _cpu_idle_threshold(sample: Dict[str, Any]) -> int:
    online = int(sample.get("cpu_online") or 0)
    return max(8, int(0.15 * online)) if online else 8


def describe_states(sample: Dict[str, Any]) -> str:
    bits = [
        f"CPU idle {sample.get('cpu_idle')}/{sample.get('cpu_online')} "
        f"(busy {sample.get('cpu_busy')})",
        f"GPU idle {sample.get('gpu_idle')}/{sample.get('gpu_online')}",
        f"claimable roots {sample.get('claimable')}, sticky {sample.get('sticky')}, "
        f"unassigned {sample.get('unassigned')}",
        f"open jobs {sample.get('open_jobs')}/{sample.get('max_concurrent')}",
    ]
    if sample.get("sql_ok"):
        bits.append(
            f"CPU jobs rooting {sample.get('cpu_root_phase')} / proving {sample.get('cpu_proof_phase')}; "
            f"unowned replacements {sample.get('unowned_cpu')} (want {sample.get('want_cpu')})"
        )
        bits.append(
            f"GPU jobs rooting {sample.get('gpu_root_phase')} / proving {sample.get('gpu_proof_phase')}; "
            f"unowned {sample.get('unowned_gpu')} (want {sample.get('want_gpu')})"
        )
        if sample.get("proved_1h_n"):
            bits.append(
                f"proofs started in last hour: n={sample.get('proved_1h_n')} "
                f"p50={sample.get('proved_1h_p50_min')}m p90={sample.get('proved_1h_p90_min')}m"
            )
        if sample.get("open_max_age_min") is not None:
            bits.append(
                f"open job age p50={sample.get('open_p50_age_min')}m "
                f"max={sample.get('open_max_age_min')}m"
            )
    blocks = list(sample.get("block_reasons") or [])
    if sample.get("cpu_profile_blocked"):
        blocks.extend(sample.get("cpu_reasons") or [])
    if sample.get("gpu_profile_blocked"):
        blocks.extend(sample.get("gpu_reasons") or [])
    if blocks:
        bits.append("create blockers: " + "; ".join(str(b) for b in blocks))
    else:
        bits.append("no create blockers")
    bits.append(
        f"idle-CPU override {'on' if sample.get('idle_cpu_needs_work') else 'off'}, "
        f"creates_15m={sample.get('creates_15m')}, roots_done_15m={sample.get('roots_done_15m')}, "
        f"stopped_15m={sample.get('stopped_15m')}"
    )
    return bits


def _join_states(sample: Dict[str, Any]) -> str:
    return " ".join(f"{i+1}) {b}" for i, b in enumerate(describe_states(sample)))


def detect_events(
    prev: Optional[Dict[str, Any]],
    curr: Dict[str, Any],
    active: set,
) -> List[Dict[str, Any]]:
    """Rising/falling edges only, so a 10-minute hole is one story plus a clear."""
    events: List[Dict[str, Any]] = []
    ts = curr.get("ts") or _now_iso()
    clock = _clock(str(ts))
    thresh = _cpu_idle_threshold(curr)
    cpu_idle = int(curr.get("cpu_idle") or 0)
    blocked = bool(curr.get("cpu_profile_blocked") or curr.get("at_max_concurrent"))
    hole = cpu_idle >= thresh
    claimable = int(curr.get("claimable") or 0)
    want_cpu = int(curr.get("want_cpu") or 0)
    unowned_cpu = int(curr.get("unowned_cpu") or 0)
    proving = int(curr.get("cpu_proof_phase") or 0)
    states = _join_states(curr)

    if hole and "cpu_idle_hole" not in active:
        active.add("cpu_idle_hole")
        if blocked:
            kind = "cpu_idle_hole_blocked"
            lead = (
                f"At {clock} there were many idle CPU slaves "
                f"({cpu_idle} of {curr.get('cpu_online')}) and creates were constrained."
            )
        elif claimable <= 0:
            kind = "cpu_idle_hole_no_work"
            lead = (
                f"At {clock} there were many idle CPU slaves "
                f"({cpu_idle} of {curr.get('cpu_online')}) and no claimable roots. "
                f"Jobs were not sitting ready for them."
            )
        else:
            kind = "cpu_idle_hole_with_claimable"
            lead = (
                f"At {clock} there were many idle CPU slaves "
                f"({cpu_idle} of {curr.get('cpu_online')}) even though {claimable} "
                f"roots were claimable (assign/sticky issue, not a create starve)."
            )
        events.append({"ts": ts, "kind": kind, "text": f"{lead} Recorded states: {states}"})

    if (not hole) and "cpu_idle_hole" in active:
        active.discard("cpu_idle_hole")
        events.append({
            "ts": ts,
            "kind": "cpu_idle_hole_cleared",
            "text": (
                f"At {clock} the CPU idle hole cleared "
                f"({cpu_idle} of {curr.get('cpu_online')} idle). "
                f"Recorded states: {states}"
            ),
        })

    keep_short = (
        curr.get("sql_ok")
        and proving >= 4
        and want_cpu > 0
        and unowned_cpu < want_cpu
        and cpu_idle < thresh
    )
    if keep_short and "keep_ahead_short" not in active:
        active.add("keep_ahead_short")
        events.append({
            "ts": ts,
            "kind": "keep_ahead_short",
            "text": (
                f"At {clock} the fleet still looked busy but keep-ahead was short: "
                f"{unowned_cpu} unowned CPU jobs vs want {want_cpu} "
                f"({proving} already proving). The next idle wave is already committed. "
                f"Recorded states: {states}"
            ),
        })
    if (not keep_short) and "keep_ahead_short" in active:
        active.discard("keep_ahead_short")

    p90 = curr.get("proved_1h_p90_min")
    if p90 is not None and float(p90) >= 90 and "slow_proofs" not in active:
        active.add("slow_proofs")
        events.append({
            "ts": ts,
            "kind": "slow_proofs",
            "text": (
                f"At {clock} jobs that started in the last hour and already proved "
                f"had p90 {p90} min (n={curr.get('proved_1h_n')}). "
                f"Recorded states: {states}"
            ),
        })
    if (p90 is None or float(p90) < 75) and "slow_proofs" in active:
        active.discard("slow_proofs")

    max_age = curr.get("open_max_age_min")
    if max_age is not None and float(max_age) >= 90 and "old_open_jobs" not in active:
        active.add("old_open_jobs")
        events.append({
            "ts": ts,
            "kind": "old_open_jobs",
            "text": (
                f"At {clock} an open job was already {max_age} min old "
                f"(p50 {curr.get('open_p50_age_min')} min). "
                f"Recorded states: {states}"
            ),
        })
    if (max_age is None or float(max_age) < 75) and "old_open_jobs" in active:
        active.discard("old_open_jobs")

    return events


def append_event(path: Path, event: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, separators=(",", ":")) + "\n")
        f.write(event.get("text", "") + "\n\n")


def _mean(xs: List[float]) -> Optional[float]:
    return statistics.mean(xs) if xs else None


def _trend(xs: List[float]) -> Optional[float]:
    """Simple end-vs-start delta using half-window means."""
    if len(xs) < 4:
        return None
    mid = len(xs) // 2
    a = _mean(xs[:mid])
    b = _mean(xs[mid:])
    if a is None or b is None:
        return None
    return b - a


def analyze(samples: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not samples:
        return {"verdict": "NO_DATA", "score": 0, "reasons": ["no samples yet"]}

    fills = [float(s["fill_rate"]) for s in samples if s.get("fill_rate") is not None]
    cpu_fills = [float(s["cpu_fill"]) for s in samples if s.get("cpu_fill") is not None]
    idle_fracs = [float(s["idle_frac"]) for s in samples if s.get("idle_frac") is not None]
    cpu_idle_fracs = [
        float(s["cpu_idle_frac"]) for s in samples if s.get("cpu_idle_frac") is not None
    ]
    sustained_idle_fracs = [
        float(s["sustained_idle_frac"])
        for s in samples
        if s.get("sustained_idle_frac") is not None
    ]
    cpu_sustained_idle_fracs = [
        float(s["cpu_sustained_idle_frac"])
        for s in samples
        if s.get("cpu_sustained_idle_frac") is not None
    ]
    claimables = [float(s["claimable"]) for s in samples]
    stickies = [float(s["sticky"]) for s in samples]
    sticky_fracs = [float(s["sticky_frac"]) for s in samples]
    creates = [float(s["creates_15m"]) for s in samples]
    roots = [float(s["roots_done_15m"]) for s in samples]
    cpu_blocked_n = sum(1 for s in samples if s.get("cpu_profile_blocked"))

    latest = samples[-1]
    fill_trend = _trend(fills)
    cpu_fill_trend = _trend(cpu_fills)
    idle_trend = _trend(idle_fracs)
    claim_trend = _trend(claimables)
    sticky_trend = _trend(sticky_fracs)

    score = 0
    reasons: List[str] = []

    # Fill up / idle down = good
    if fill_trend is not None:
        if fill_trend >= 0.05:
            score += 2
            reasons.append(f"fill_rate rising ({fill_trend:+.3f})")
        elif fill_trend <= -0.05:
            score -= 2
            reasons.append(f"fill_rate falling ({fill_trend:+.3f})")
        else:
            reasons.append(f"fill_rate flat ({fill_trend:+.3f})")

    if idle_trend is not None:
        if idle_trend <= -0.05:
            score += 2
            reasons.append(f"idle_frac falling ({idle_trend:+.3f})")
        elif idle_trend >= 0.05:
            score -= 2
            reasons.append(f"idle_frac rising ({idle_trend:+.3f})")

    if cpu_fill_trend is not None:
        if cpu_fill_trend >= 0.05:
            score += 1
            reasons.append(f"cpu_fill rising ({cpu_fill_trend:+.3f})")
        elif cpu_fill_trend <= -0.05:
            score -= 1
            reasons.append(f"cpu_fill falling ({cpu_fill_trend:+.3f})")

    # Claimable flood / sticky warehouse = bad
    avg_claim = _mean(claimables) or 0.0
    avg_sticky_frac = _mean(sticky_fracs) or 0.0
    if avg_claim >= 100:
        score -= 1
        reasons.append(f"high avg claimable ({avg_claim:.0f})")
    if claim_trend is not None and claim_trend >= 40:
        score -= 1
        reasons.append(f"claimable rising hard ({claim_trend:+.0f})")
    if avg_sticky_frac >= 0.4:
        score -= 1
        reasons.append(f"sticky heavy ({avg_sticky_frac:.0%} of unassigned)")
    if sticky_trend is not None and sticky_trend >= 0.15:
        score -= 1
        reasons.append(f"sticky_frac rising ({sticky_trend:+.2f})")

    # Create starvation vs finish rate
    avg_creates = _mean(creates) or 0.0
    avg_roots = _mean(roots) or 0.0
    if avg_creates < 10 and avg_roots > 200:
        score -= 1
        reasons.append(
            f"create starved vs finishes (creates_15m={avg_creates:.0f}, roots_done_15m={avg_roots:.0f})"
        )
    elif avg_creates >= 20:
        score += 1
        reasons.append(f"healthy create volume (creates_15m≈{avg_creates:.0f})")

    blocked_frac = cpu_blocked_n / max(1, len(samples))
    if blocked_frac >= 0.3:
        score -= 1
        reasons.append(f"CPU profile blocked {blocked_frac:.0%} of samples")

    # Absolute latest health — prefer sustained idle (less between-job flicker).
    latest_fill = latest.get("fill_rate")
    latest_cpu_idle_frac = latest.get("cpu_sustained_idle_frac")
    if latest_cpu_idle_frac is None:
        latest_cpu_idle_frac = latest.get("cpu_idle_frac")
    if latest_fill is not None and float(latest_fill) >= 0.75:
        score += 1
        reasons.append(f"latest fill healthy ({float(latest_fill):.0%})")
    if latest_cpu_idle_frac is not None and float(latest_cpu_idle_frac) >= 0.4:
        score -= 1
        reasons.append(f"latest cpu sustained idle high ({float(latest_cpu_idle_frac):.0%})")

    if score >= 2:
        verdict = "HELPING"
    elif score <= -2:
        verdict = "HURTING"
    elif score == 0:
        verdict = "NEUTRAL"
    else:
        verdict = "MIXED"

    return {
        "verdict": verdict,
        "score": score,
        "reasons": reasons,
        "n_samples": len(samples),
        "window": {
            "first_ts": samples[0]["ts"],
            "last_ts": samples[-1]["ts"],
            "avg_fill": _mean(fills),
            "avg_cpu_fill": _mean(cpu_fills),
            "avg_idle_frac": _mean(idle_fracs),
            "avg_cpu_idle_frac": _mean(cpu_idle_fracs),
            "avg_sustained_idle_frac": _mean(sustained_idle_fracs),
            "avg_cpu_sustained_idle_frac": _mean(cpu_sustained_idle_fracs),
            "avg_claimable": _mean(claimables),
            "avg_sticky": _mean(stickies),
            "avg_sticky_frac": _mean(sticky_fracs),
            "avg_creates_15m": _mean(creates),
            "avg_roots_done_15m": _mean(roots),
            "fill_trend": fill_trend,
            "idle_frac_trend": idle_trend,
            "claimable_trend": claim_trend,
            "sticky_frac_trend": sticky_trend,
        },
        "latest": latest,
    }


def write_summary(path: Path, analysis: Dict[str, Any]) -> None:
    w = analysis.get("window") or {}
    latest = analysis.get("latest") or {}
    lines = [
        f"updated_utc: {_now_iso()}",
        f"verdict: {analysis.get('verdict')}",
        f"score: {analysis.get('score')}",
        f"samples: {analysis.get('n_samples')}",
        f"window: {w.get('first_ts')} -> {w.get('last_ts')}",
        "",
        "averages:",
        f"  fill={_fmt_pct(w.get('avg_fill'))}  cpu_fill={_fmt_pct(w.get('avg_cpu_fill'))}",
        f"  idle_frac={_fmt_pct(w.get('avg_idle_frac'))}  cpu_idle_frac={_fmt_pct(w.get('avg_cpu_idle_frac'))}",
        f"  sustained_idle_frac={_fmt_pct(w.get('avg_sustained_idle_frac'))}  cpu_sustained_idle_frac={_fmt_pct(w.get('avg_cpu_sustained_idle_frac'))}",
        f"  claimable={_fmt_num(w.get('avg_claimable'))}  sticky={_fmt_num(w.get('avg_sticky'))}  sticky_frac={_fmt_pct(w.get('avg_sticky_frac'))}",
        f"  creates_15m={_fmt_num(w.get('avg_creates_15m'))}  roots_done_15m={_fmt_num(w.get('avg_roots_done_15m'))}",
        "",
        "trends (2nd half - 1st half):",
        f"  fill={_fmt_signed(w.get('fill_trend'))}  idle_frac={_fmt_signed(w.get('idle_frac_trend'))}",
        f"  claimable={_fmt_signed(w.get('claimable_trend'), digits=1)}  sticky_frac={_fmt_signed(w.get('sticky_frac_trend'))}",
        "",
        "latest:",
        f"  online={latest.get('online')} busy={latest.get('busy')} idle={latest.get('idle')} fill={_fmt_pct(latest.get('fill_rate'))}",
        f"  cpu_idle={latest.get('cpu_idle')}/{latest.get('cpu_online')} cpu_fill={_fmt_pct(latest.get('cpu_fill'))}",
        f"  claimable={latest.get('claimable')} sticky={latest.get('sticky')} unassigned={latest.get('unassigned')}",
        f"  creates_15m={latest.get('creates_15m')} roots_done_15m={latest.get('roots_done_15m')}",
        f"  idle_cpu_needs_work={latest.get('idle_cpu_needs_work')} cpu_blocked={latest.get('cpu_profile_blocked')}",
        f"  open_jobs={latest.get('open_jobs')}/{latest.get('max_concurrent')}",
        f"  cpu_root/proof={latest.get('cpu_root_phase')}/{latest.get('cpu_proof_phase')} "
        f"unowned_cpu={latest.get('unowned_cpu')} want_cpu={latest.get('want_cpu')}",
        f"  proved_1h n={latest.get('proved_1h_n')} p50={latest.get('proved_1h_p50_min')} "
        f"p90={latest.get('proved_1h_p90_min')} open_max_age={latest.get('open_max_age_min')}",
        "",
        "reasons:",
    ]
    for r in analysis.get("reasons") or []:
        lines.append(f"  - {r}")
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def _fmt_pct(v: Any) -> str:
    if v is None:
        return "n/a"
    return f"{float(v):.1%}"


def _fmt_num(v: Any) -> str:
    if v is None:
        return "n/a"
    return f"{float(v):.1f}"


def _fmt_signed(v: Any, digits: int = 3) -> str:
    if v is None:
        return "n/a"
    return f"{float(v):+.{digits}f}"


def load_samples(jsonl_path: Path) -> List[Dict[str, Any]]:
    if not jsonl_path.is_file():
        return []
    out: List[Dict[str, Any]] = []
    for line in jsonl_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def append_sample(jsonl_path: Path, sample: Dict[str, Any]) -> None:
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    with jsonl_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(sample, separators=(",", ":")) + "\n")


def one_line(sample: Dict[str, Any], verdict: str) -> str:
    return (
        f"{sample['ts']} verdict={verdict} "
        f"fill={_fmt_pct(sample.get('fill_rate'))} "
        f"cpu_fill={_fmt_pct(sample.get('cpu_fill'))} "
        f"idle={sample.get('idle')}/{sample.get('online')} "
        f"cpu_idle={sample.get('cpu_idle')}/{sample.get('cpu_online')} "
        f"claim={sample.get('claimable')} sticky={sample.get('sticky')} "
        f"creates15={sample.get('creates_15m')} roots15={sample.get('roots_done_15m')} "
        f"idle_cpu={sample.get('idle_cpu_needs_work')} "
        f"cpu_blocked={sample.get('cpu_profile_blocked')} "
        f"root/proof={sample.get('cpu_root_phase')}/{sample.get('cpu_proof_phase')} "
        f"unowned={sample.get('unowned_cpu')}/{sample.get('want_cpu')}"
    )


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--interval", type=int, default=60, help="seconds between samples (default 60)")
    p.add_argument("--duration-hours", type=float, default=0.0, help="stop after N hours (0=forever)")
    p.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR, help="output directory")
    p.add_argument(
        "--url",
        default="",
        help="ops metrics URL (default http://127.0.0.1:$WEB_PORT/api/admin/ops/metrics)",
    )
    p.add_argument("--dotenv", type=Path, default=Path(".env"), help="optional .env to load")
    p.add_argument("--summary-only", action="store_true", help="rebuild summary from existing JSONL and exit")
    p.add_argument("--once", action="store_true", help="take one sample and exit")
    p.add_argument(
        "--compose-dir",
        type=Path,
        default=Path("."),
        help="directory with docker-compose.yml for the extra SQL snapshot",
    )
    p.add_argument(
        "--skip-sql",
        action="store_true",
        help="do not query Postgres (ops metrics only)",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    _load_dotenv(args.dotenv)

    secret = os.environ.get("ADMIN_SECRET") or ""
    if not secret and not args.summary_only:
        print("ERROR: ADMIN_SECRET not set (source .env or export it)", file=sys.stderr)
        return 2

    port = os.environ.get("WEB_PORT") or "80"
    url = args.url or DEFAULT_URL.format(port=port)

    out_dir = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = out_dir / "samples.jsonl"
    summary_path = out_dir / "summary.txt"
    latest_path = out_dir / "latest.json"
    events_path = out_dir / "events.txt"

    if args.summary_only:
        samples = load_samples(jsonl_path)
        analysis = analyze(samples)
        write_summary(summary_path, analysis)
        latest_path.write_text(json.dumps(analysis, indent=2) + "\n", encoding="utf-8")
        print(summary_path.read_text(encoding="utf-8"))
        if events_path.is_file():
            print("--- events ---")
            print(events_path.read_text(encoding="utf-8"))
        return 0

    deadline = None
    if args.duration_hours and args.duration_hours > 0:
        deadline = time.time() + args.duration_hours * 3600.0

    print(
        f"monitoring {url} every {args.interval}s -> {out_dir} "
        f"(duration_hours={args.duration_hours or 'forever'} sql={not args.skip_sql})",
        flush=True,
    )

    prev_sample: Optional[Dict[str, Any]] = None
    active_events: set = set()
    compose_dir = args.compose_dir.resolve()

    while True:
        try:
            metrics = fetch_metrics(url, secret)
            sample = sample_from_metrics(metrics)
            if not args.skip_sql:
                try:
                    sample = merge_sql_snapshot(sample, fetch_sql_snapshot(compose_dir))
                except Exception as exc:
                    sample["sql_ok"] = False
                    sample["sql_error"] = str(exc)[:300]
                    print(f"{_now_iso()} WARN sql snapshot failed: {exc}", flush=True)
            append_sample(jsonl_path, sample)
            for event in detect_events(prev_sample, sample, active_events):
                append_event(events_path, event)
                print(f"{event['ts']} EVENT {event['kind']}: {event['text']}", flush=True)
            prev_sample = sample
            samples = load_samples(jsonl_path)
            analysis = analyze(samples)
            write_summary(summary_path, analysis)
            latest_path.write_text(json.dumps(analysis, indent=2) + "\n", encoding="utf-8")
            print(one_line(sample, analysis["verdict"]), flush=True)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
            print(f"{_now_iso()} ERROR fetch failed: {exc}", flush=True)

        if args.once:
            break
        if deadline is not None and time.time() >= deadline:
            print(f"{_now_iso()} duration reached; final summary in {summary_path}", flush=True)
            break
        time.sleep(max(5, int(args.interval)))

    return 0


if __name__ == "__main__":
    sys.exit(main())
