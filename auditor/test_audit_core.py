"""Pure-logic checks for the auditor (no DB, no docker)."""

import random

from audit_core import (
    AuditorSettings,
    LeafVerdict,
    build_verifier_cmd,
    classify_verifier_failure,
    judge,
    parse_verifier_quality,
    promotion_decision,
    should_verify,
)


def test_settings_from_env_defaults_and_overrides():
    s = AuditorSettings.from_env({})
    assert "knapsack" in s.challenges
    assert s.fail_quarantine_threshold == 1
    assert s.missing_quarantine_threshold == 0
    s = AuditorSettings.from_env({
        "AUDIT_CHALLENGES": "knapsack, energy_arbitrage",
        "AUDIT_TRUSTED_SAMPLE_RATE": "5",  # clamps to 1.0
        "AUDIT_FAIL_QUARANTINE_THRESHOLD": "x",  # bad -> default
        "AUDIT_RETENTION_DAYS": "0",  # clamps to 1
    })
    assert s.challenges == ("knapsack", "energy_arbitrage")
    assert s.trusted_sample_rate == 1.0
    assert s.fail_quarantine_threshold == 1
    assert s.retention_days == 1


def test_should_verify_probation_always_trusted_sampled():
    s = AuditorSettings(trusted_sample_rate=0.0)
    assert should_verify("probation", s)
    assert should_verify(None, s)
    assert should_verify("quarantined", s)
    assert not should_verify("trusted", s)
    assert not should_verify("operator", s)
    s = AuditorSettings(trusted_sample_rate=1.0)
    assert should_verify("trusted", s)
    s = AuditorSettings(trusted_sample_rate=0.5)
    hits = sum(should_verify("trusted", s, rng=random.Random(i)) for i in range(400))
    assert 120 < hits < 280


def test_parse_verifier_quality():
    assert parse_verifier_quality("loading...\nquality: 3712345\n") == 3712345
    assert parse_verifier_quality("quality: -5") == -5
    assert parse_verifier_quality("quality:   17  ") == 17
    assert parse_verifier_quality("") is None
    assert parse_verifier_quality("error: invalid solution") is None


def test_build_verifier_cmd_never_runs_runtime():
    cmd = build_verifier_cmd("knapsack", {"a": 1}, "abc", 7, "/app/audit/scratch/x.json")
    assert cmd[:4] == ["docker", "exec", "knapsack", "tig-verifier"]
    assert cmd[4] == '{"a":1}'
    assert cmd[5:] == ["abc", "7", "/app/audit/scratch/x.json"]
    assert "tig-runtime" not in cmd
    assert "--fuel" not in cmd


def test_classify_failures():
    assert classify_verifier_failure(1, "Error: No such container: knapsack") == "infra"
    assert classify_verifier_failure(126, "") == "infra"
    assert classify_verifier_failure(1, "Failed to read solution file: x.json") == "infra"
    assert classify_verifier_failure(1, "invalid solution: weight exceeds capacity") == "invalid"


def _v(nonce, exp, act, ok=None, error=None):
    return LeafVerdict(nonce, exp, act, (exp == act) if ok is None else ok, error)


def test_judge_passed_when_all_match():
    out = judge([100, 200], [5, 9], {5: _v(5, 100, 100), 9: _v(9, 200, 200)}, delivered=[5, 9])
    assert out.status == "passed"
    assert out.result_json() == {"5": {"expected": 100, "actual": 100, "ok": True}, "9": {"expected": 200, "actual": 200, "ok": True}}


def test_judge_failed_on_any_mismatch():
    out = judge([100, 200], [5, 9], {5: _v(5, 100, 100), 9: _v(9, 200, 150)}, delivered=[5, 9])
    assert out.status == "failed"
    assert out.result_json()["9"]["ok"] is False


def test_judge_failed_on_invalid_solution():
    out = judge([100], [5], {5: _v(5, 100, None, ok=False, error="invalid solution: bad")}, delivered=[5])
    assert out.status == "failed"


def test_judge_error_on_infra_only():
    out = judge([100, 200], [5, 9], {5: _v(5, 100, 100), 9: _v(9, 200, None, ok=False, error="infra: container down")}, delivered=[5, 9])
    assert out.status == "error"


def test_judge_mismatch_beats_infra():
    out = judge([100, 200], [5, 9], {5: _v(5, 100, 1), 9: _v(9, 200, None, ok=False, error="infra: x")}, delivered=[5, 9])
    assert out.status == "failed"


def test_judge_partial_delivery_passes_on_delivered():
    # Slave only delivered nonce 5; nonce 9 never arrived. That is 'missing'
    # bookkeeping, not a mismatch.
    out = judge([100, 200], [5, 9], {5: _v(5, 100, 100)}, delivered=[5])
    assert out.status == "passed"


H = 3600 * 1000
NOW = 1_800_000_000_000


def _decide(state, source=None, passed=60, failed=0, missing=0, first_age_h=30.0, settings=None):
    s = settings or AuditorSettings()
    return promotion_decision(
        state, source,
        passed_in_window=passed, failed_in_window=failed, missing_in_window=missing,
        first_audit_at_ms=None if first_age_h is None else NOW - int(first_age_h * H),
        settings=s, now_ms=NOW,
    )


def test_promotion_env_defaults():
    s = AuditorSettings.from_env({})
    assert s.promote_min_passed == 50
    assert s.promote_min_age_ms == 24 * H
    assert s.promote_window_ms == 24 * H
    s = AuditorSettings.from_env({"AUDIT_PROMOTE_MIN_PASSED": "0", "AUDIT_PROMOTE_MIN_AGE_H": "6", "AUDIT_PROMOTE_WINDOW_H": "0.1"})
    assert s.promote_min_passed == 0
    assert s.promote_min_age_ms == 6 * H
    assert s.promote_window_ms == H  # floor 1h


def test_promote_clean_probation_member():
    assert _decide("probation") == "promote"
    assert _decide(None) == "promote"  # DB default


def test_no_promote_when_not_enough_evidence():
    assert _decide("probation", passed=49) is None
    assert _decide("probation", first_age_h=23.9) is None
    assert _decide("probation", first_age_h=None) is None


def test_no_promote_with_any_strike_in_window():
    assert _decide("probation", failed=1) is None
    assert _decide("probation", missing=1) is None


def test_promotion_disabled_by_zero():
    assert _decide("probation", settings=AuditorSettings(promote_min_passed=0)) is None
    assert _decide("trusted", "auditor", failed=1, settings=AuditorSettings(promote_min_passed=0)) is None


def test_demote_only_auditor_promoted_members():
    assert _decide("trusted", "auditor", failed=1) == "demote"
    assert _decide("trusted", "auditor", failed=0, missing=3) is None
    assert _decide("trusted", None, failed=1) is None       # operator-set trust
    assert _decide("trusted", "operator", failed=1) is None
    assert _decide("operator", "auditor", failed=1) is None
    assert _decide("quarantined", "auditor", failed=1) is None
