"""Pure-logic checks for master/batch_audit.py (no DB)."""

import random

import pytest

from master.batch_audit import (
    DEFAULTS,
    audit_settings,
    choose_audit_nonces,
    validate_audit_leaves,
)


def _leaf(nonce, **over):
    d = {
        "nonce": nonce,
        "runtime_signature": 123456789,
        "fuel_consumed": 42,
        "solution": '{"items":[1,2,3]}',
        "cpu_arch": "amd64",
    }
    d.update(over)
    return d


# ── settings ─────────────────────────────────────────────────────────────────

def test_settings_defaults_when_missing():
    s = audit_settings({})
    assert s == audit_settings(None)
    assert s["enabled"] is True
    assert s["leaves_per_batch"] == DEFAULTS["leaves_per_batch"]


def test_settings_override_and_clamp():
    s = audit_settings({"audit": {"enabled": False, "leaves_per_batch": -3, "max_leaf_bytes": 10, "request_ttl_ms": 5}})
    assert s["enabled"] is False
    assert s["leaves_per_batch"] == 0
    assert s["max_leaf_bytes"] == 1024
    assert s["request_ttl_ms"] == 60_000


# ── sampler ──────────────────────────────────────────────────────────────────

def test_choose_includes_max_quality_and_random_extras():
    q = [10, 50, 999, 20, 30, 40]
    rng = random.Random(1)
    picked = choose_audit_nonces(600, q, leaves_per_batch=2, include_max_quality=True, rng=rng)
    assert 602 in picked  # start_nonce + argmax
    assert len(picked) == 3
    assert picked == sorted(picked)
    assert all(600 <= n < 606 for n in picked)


def test_choose_caps_at_batch_size():
    picked = choose_audit_nonces(0, [1, 2], leaves_per_batch=10, include_max_quality=True)
    assert picked == [0, 1]


def test_choose_zero_leaves_still_returns_max():
    picked = choose_audit_nonces(8, [5, 7, 6], leaves_per_batch=0, include_max_quality=True)
    assert picked == [9]


def test_choose_zero_leaves_no_max_is_empty():
    assert choose_audit_nonces(8, [5, 7, 6], leaves_per_batch=0, include_max_quality=False) == []
    assert choose_audit_nonces(8, [], leaves_per_batch=3) == []


def test_choose_is_not_predictable_from_qualities_alone():
    # Two runs with different rngs over the same quality list should differ
    # somewhere; the slave cannot precompute the sample.
    q = list(range(64))
    a = choose_audit_nonces(0, q, leaves_per_batch=3, include_max_quality=False, rng=random.Random(1))
    b = choose_audit_nonces(0, q, leaves_per_batch=3, include_max_quality=False, rng=random.Random(2))
    assert a != b


# ── payload validation ───────────────────────────────────────────────────────

def test_validate_accepts_requested_leaves_and_strips_extras():
    body = {"leaves": [_leaf(12, quality=555, extra="x"), _leaf(15)]}
    out = validate_audit_leaves(body, requested_nonces=[12, 15, 19], max_leaf_bytes=4096)
    assert [l["nonce"] for l in out] == [12, 15]
    assert "quality" not in out[0] and "extra" not in out[0]


def test_validate_partial_delivery_ok():
    out = validate_audit_leaves({"leaves": [_leaf(15)]}, requested_nonces=[12, 15], max_leaf_bytes=4096)
    assert len(out) == 1


@pytest.mark.parametrize(
    "body",
    [
        [],
        {"leaves": "nope"},
        {"leaves": [1]},
        {"leaves": [_leaf(99)]},                       # not requested
        {"leaves": [_leaf(12), _leaf(12)]},            # duplicate
        {"leaves": [_leaf(12), _leaf(15), _leaf(19), _leaf(12)]},  # more than requested
        {"leaves": [{k: v for k, v in _leaf(12).items() if k != "solution"}]},
        {"leaves": [_leaf(12, runtime_signature="abc")]},
        {"leaves": [_leaf(12, solution=12345)]},
        {"leaves": [_leaf("twelve")]},
    ],
)
def test_validate_rejects_malformed(body):
    with pytest.raises(ValueError):
        validate_audit_leaves(body, requested_nonces=[12, 15, 19], max_leaf_bytes=4096)


def test_validate_rejects_oversize_leaf():
    big = _leaf(12, solution="x" * 5000)
    with pytest.raises(ValueError):
        validate_audit_leaves({"leaves": [big]}, requested_nonces=[12], max_leaf_bytes=4096)
