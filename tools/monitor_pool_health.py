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
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
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
    fill = slaves.get("fill_rate")
    cpu_online = int(cpu.get("online") or 0)
    cpu_idle = int(cpu.get("idle") or 0)
    cpu_fill = cpu.get("fill_rate")
    gpu_fill = gpu.get("fill_rate")
    claimable = int(d.get("claimable_root_total") or 0)
    sticky = int(d.get("sticky_reserved_root_total") or 0)
    unassigned = int(d.get("unassigned_root_total") or 0)
    creates_15m = int(creates.get("creates_15m") or 0)
    roots_done_15m = int(finishes.get("roots_done_15m") or 0)

    idle_frac = (idle / online) if online > 0 else None
    cpu_idle_frac = (cpu_idle / cpu_online) if cpu_online > 0 else None
    sticky_frac = (sticky / unassigned) if unassigned > 0 else 0.0

    return {
        "ts": _now_iso(),
        "epoch": int(time.time()),
        "online": online,
        "busy": busy,
        "idle": idle,
        "idle_frac": idle_frac,
        "fill_rate": fill,
        "cpu_online": cpu_online,
        "cpu_busy": int(cpu.get("busy") or 0),
        "cpu_idle": cpu_idle,
        "cpu_idle_frac": cpu_idle_frac,
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
        "online_idle_cpu_slaves_gov": counts.get("online_idle_cpu_slaves"),
        "open_jobs": counts.get("open_jobs"),
        "max_concurrent": counts.get("max_concurrent_benchmarks"),
    }


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

    # Absolute latest health
    latest_fill = latest.get("fill_rate")
    latest_cpu_idle_frac = latest.get("cpu_idle_frac")
    if latest_fill is not None and float(latest_fill) >= 0.75:
        score += 1
        reasons.append(f"latest fill healthy ({float(latest_fill):.0%})")
    if latest_cpu_idle_frac is not None and float(latest_cpu_idle_frac) >= 0.4:
        score -= 1
        reasons.append(f"latest cpu idle high ({float(latest_cpu_idle_frac):.0%})")

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
        f"cpu_blocked={sample.get('cpu_profile_blocked')}"
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

    if args.summary_only:
        samples = load_samples(jsonl_path)
        analysis = analyze(samples)
        write_summary(summary_path, analysis)
        latest_path.write_text(json.dumps(analysis, indent=2) + "\n", encoding="utf-8")
        print(summary_path.read_text(encoding="utf-8"))
        return 0

    deadline = None
    if args.duration_hours and args.duration_hours > 0:
        deadline = time.time() + args.duration_hours * 3600.0

    print(
        f"monitoring {url} every {args.interval}s -> {out_dir} "
        f"(duration_hours={args.duration_hours or 'forever'})",
        flush=True,
    )

    while True:
        try:
            metrics = fetch_metrics(url, secret)
            sample = sample_from_metrics(metrics)
            append_sample(jsonl_path, sample)
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
