"""Pure logic for the quality auditor. No DB, no docker — unit-testable."""

from __future__ import annotations

import json
import os
import random
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence

# Challenge container names the VPS runs. GPU challenges need a card and a
# --ptx; the VPS has neither, so they are stored (evidence) but not verified.
DEFAULT_CPU_CHALLENGES = (
    "satisfiability",
    "vehicle_routing",
    "knapsack",
    "job_scheduling",
    "energy_arbitrage",
)

_QUALITY_RE = re.compile(r"^quality:\s*(-?\d+)\s*$", re.MULTILINE)


@dataclass
class AuditorSettings:
    challenges: tuple = DEFAULT_CPU_CHALLENGES
    # Verify every leaf from members in these trust states.
    always_verify_states: tuple = ("probation", "unknown", "quarantined", "blocked", "disabled")
    # Sample fraction for trusted / operator members (0..1). 1.0 = verify all.
    trusted_sample_rate: float = 0.10
    # Strikes (failed audits) inside the window before auto-quarantine.
    fail_quarantine_threshold: int = 1
    # Requested-but-never-delivered audits before auto-quarantine. 0 = off
    # (leave off until the whole fleet runs a slave that answers audits).
    missing_quarantine_threshold: int = 0
    strike_window_ms: int = 7 * 24 * 3600 * 1000
    # Slave has this long after the root ack to deliver leaves.
    request_ttl_ms: int = 30 * 60 * 1000
    # Keep leaves of passed / skipped audits this long. Failed = forever.
    retention_days: int = 14
    # Verifier retries on infra error (container down etc.) before 'error'.
    max_attempts: int = 3
    verifier_timeout_s: int = 120
    poll_interval_s: float = 5.0
    batch_limit: int = 8
    # Parallel verifier execs per loop tick.
    workers: int = 2
    # Auto-promotion probation -> trusted. A probation member is verified on
    # every batch, so honesty shows fast: this many passed audits, first audit
    # at least min_age ago, and zero failed/missing inside the window.
    # 0 = never auto-promote.
    promote_min_passed: int = 50
    promote_min_age_ms: int = 24 * 3600 * 1000
    promote_window_ms: int = 24 * 3600 * 1000

    @classmethod
    def from_env(cls, env: Optional[Dict[str, str]] = None) -> "AuditorSettings":
        env = os.environ if env is None else env

        def _f(key, default, cast):
            raw = env.get(key)
            if raw is None or str(raw).strip() == "":
                return default
            try:
                return cast(raw)
            except Exception:
                return default

        challenges = tuple(
            c.strip() for c in str(env.get("AUDIT_CHALLENGES", ",".join(DEFAULT_CPU_CHALLENGES))).split(",") if c.strip()
        )
        return cls(
            challenges=challenges or DEFAULT_CPU_CHALLENGES,
            trusted_sample_rate=min(1.0, max(0.0, _f("AUDIT_TRUSTED_SAMPLE_RATE", 0.10, float))),
            fail_quarantine_threshold=max(0, _f("AUDIT_FAIL_QUARANTINE_THRESHOLD", 1, int)),
            missing_quarantine_threshold=max(0, _f("AUDIT_MISSING_QUARANTINE_THRESHOLD", 0, int)),
            strike_window_ms=max(60_000, _f("AUDIT_STRIKE_WINDOW_MS", 7 * 24 * 3600 * 1000, int)),
            request_ttl_ms=max(60_000, _f("AUDIT_REQUEST_TTL_MS", 30 * 60 * 1000, int)),
            retention_days=max(1, _f("AUDIT_RETENTION_DAYS", 14, int)),
            max_attempts=max(1, _f("AUDIT_MAX_ATTEMPTS", 3, int)),
            verifier_timeout_s=max(10, _f("AUDIT_VERIFIER_TIMEOUT_S", 120, int)),
            poll_interval_s=max(0.5, _f("AUDIT_POLL_INTERVAL_S", 5.0, float)),
            batch_limit=max(1, _f("AUDIT_BATCH_LIMIT", 8, int)),
            workers=max(1, min(8, _f("AUDIT_WORKERS", 2, int))),
            promote_min_passed=max(0, _f("AUDIT_PROMOTE_MIN_PASSED", 50, int)),
            promote_min_age_ms=max(0, int(_f("AUDIT_PROMOTE_MIN_AGE_H", 24.0, float) * 3600 * 1000)),
            promote_window_ms=max(3600_000, int(_f("AUDIT_PROMOTE_WINDOW_H", 24.0, float) * 3600 * 1000)),
        )


def should_verify(trust_state: Optional[str], settings: AuditorSettings, rng: Optional[random.Random] = None) -> bool:
    """Decide whether a pending audit gets verified or just stored as evidence."""
    state = str(trust_state or "unknown").lower()
    if state in settings.always_verify_states:
        return True
    rate = float(settings.trusted_sample_rate)
    if rate >= 1.0:
        return True
    if rate <= 0.0:
        return False
    return (rng or random.SystemRandom()).random() < rate


def promotion_decision(
    trust_state: Optional[str],
    trust_source: Optional[str],
    *,
    passed_in_window: int,
    failed_in_window: int,
    missing_in_window: int,
    first_audit_at_ms: Optional[int],
    settings: AuditorSettings,
    now_ms: int,
) -> Optional[str]:
    """Return 'promote', 'demote' or None for one member.

    promote: probation member with a clean window of enough passed audits
             whose first audit is old enough (seen across a day of tracks).
    demote : member the auditor itself promoted (trust_source='auditor')
             that has a failed audit in the window. Operator-set trust is
             never touched; quarantine handles outright cheats first.
    """
    if settings.promote_min_passed <= 0:
        return None
    state = str(trust_state or "probation").lower()
    if state == "probation":
        if failed_in_window or missing_in_window:
            return None
        if passed_in_window < settings.promote_min_passed:
            return None
        if first_audit_at_ms is None or now_ms - int(first_audit_at_ms) < settings.promote_min_age_ms:
            return None
        return "promote"
    if state == "trusted" and str(trust_source or "").lower() == "auditor":
        if failed_in_window:
            return "demote"
    return None


def parse_verifier_quality(stdout: str) -> Optional[int]:
    """tig-verifier prints ``quality: N`` as its last line on success."""
    if not stdout:
        return None
    lines = [ln.strip() for ln in stdout.strip().splitlines() if ln.strip()]
    if not lines:
        return None
    m = _QUALITY_RE.match(lines[-1])
    if m is None:
        m = _QUALITY_RE.search(stdout)
    return int(m.group(1)) if m else None


def build_verifier_cmd(challenge: str, settings_obj: Any, rand_hash: str, nonce: int, leaf_path: str) -> List[str]:
    settings_json = settings_obj if isinstance(settings_obj, str) else json.dumps(settings_obj, separators=(",", ":"))
    return [
        "docker", "exec", challenge, "tig-verifier",
        settings_json,
        str(rand_hash),
        str(int(nonce)),
        leaf_path,
    ]


@dataclass
class LeafVerdict:
    nonce: int
    expected: Optional[int]
    actual: Optional[int]
    ok: bool
    error: Optional[str] = None

    def to_dict(self) -> dict:
        d = {"expected": self.expected, "actual": self.actual, "ok": self.ok}
        if self.error:
            d["error"] = self.error
        return d


@dataclass
class AuditOutcome:
    # passed | failed | error | skipped
    status: str
    verdicts: Dict[int, LeafVerdict] = field(default_factory=dict)
    error: Optional[str] = None

    def result_json(self) -> dict:
        return {str(n): v.to_dict() for n, v in self.verdicts.items()}


# Verifier stderr that means "our box is broken", not "the leaf is bad".
_INFRA_MARKERS = (
    "no such container",
    "is not running",
    "cannot connect to the docker daemon",
    "executable file not found",
    "oci runtime exec failed",
    "error response from daemon",
    "failed to read solution file",
)


def classify_verifier_failure(returncode: int, stderr: str) -> str:
    """'infra' when we should retry later, 'invalid' when the leaf itself failed."""
    text = (stderr or "").lower()
    if any(marker in text for marker in _INFRA_MARKERS):
        return "infra"
    if returncode in (125, 126, 127):  # docker CLI / exec failures
        return "infra"
    return "invalid"


def judge(expected: Sequence[int], nonces: Sequence[int], verdicts: Dict[int, LeafVerdict], *, delivered: Iterable[int]) -> AuditOutcome:
    """Combine per-leaf verdicts into one audit status.

    - any leaf where actual != expected            -> failed
    - any leaf invalid (verifier rejected solution) -> failed
    - any leaf infra error and nothing failed      -> error (retry)
    - all delivered leaves match                   -> passed
    Missing leaves (requested but not delivered) are recorded but do not by
    themselves fail the audit; the 'missing' status handles never-delivered.
    """
    delivered = set(int(n) for n in delivered)
    failed = False
    infra = False
    for nonce, exp in zip(nonces, expected):
        v = verdicts.get(int(nonce))
        if v is None:
            if int(nonce) in delivered:
                infra = True
            continue
        if v.error and v.error.startswith("infra:"):
            infra = True
            continue
        if not v.ok:
            failed = True
    if failed:
        return AuditOutcome("failed", verdicts)
    if infra:
        return AuditOutcome("error", verdicts, error="verifier infrastructure error; will retry")
    return AuditOutcome("passed", verdicts)
