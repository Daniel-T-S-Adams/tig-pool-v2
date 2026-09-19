"""Pure-logic checks for master/batch_audit.py (no DB)."""

import json
import os
import random
import sys

import pytest

from master.batch_audit import (
    DEFAULTS,
    audit_settings,
    choose_audit_nonces,
    fetch_header_value,
    group_nonces_by_batch,
    validate_audit_leaves,
    verify_leaf_branch,
)

# ``common`` (tig merkle tree / structs) lives in the tig-benchmarker image;
# for local runs look in the usual sibling checkouts.
for _cand in (
    os.path.join(os.path.dirname(__file__), "..", "..", "tig-monorepo", "tig-benchmarker"),
    os.path.join(os.path.dirname(__file__), "..", "..", "innopool-slave"),
):
    if os.path.isdir(os.path.join(_cand, "common")) and _cand not in sys.path:
        sys.path.insert(0, os.path.abspath(_cand))


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


# ── merkle branch (fetch path) ───────────────────────────────────────────────

def test_validate_keeps_wellformed_branch_and_drops_extras():
    branch = "00" + "ab" * 32
    out = validate_audit_leaves(
        {"leaves": [_leaf(12, branch=branch, quality=999)]}, requested_nonces=[12], max_leaf_bytes=4096
    )
    assert out[0]["branch"] == branch
    assert "quality" not in out[0]


@pytest.mark.parametrize("branch", ["zz" * 33, "00" + "ab" * 31, 12345, "00" + "ab" * 32 + "0"])
def test_validate_rejects_malformed_branch(branch):
    with pytest.raises(ValueError):
        validate_audit_leaves({"leaves": [_leaf(12, branch=branch)]}, requested_nonces=[12], max_leaf_bytes=4096)


def test_verify_leaf_branch_none_without_branch_or_root():
    assert verify_leaf_branch(_leaf(12), merkle_root="00" * 32, start_nonce=0) is None
    assert verify_leaf_branch(_leaf(12, branch="00" + "ab" * 32), merkle_root="", start_nonce=0) is None


def _build_batch(start_nonce, n, batch_size):
    mt = pytest.importorskip("common.merkle_tree")
    st = pytest.importorskip("common.structs")
    leaves = [_leaf(start_nonce + i, solution=json.dumps({"i": i})) for i in range(n)]
    # from_dict pops keys from its argument — hash copies.
    hashes = [st.OutputData.from_dict(dict(l)).to_merkle_hash() for l in leaves]
    tree = mt.MerkleTree(hashes, batch_size)
    root = tree.calc_merkle_root().to_str()
    return leaves, tree, root


def test_verify_leaf_branch_accepts_committed_leaf():
    start, n, size = 64, 30, 32  # batch_idx 2 of a batch_size-32 job, short last batch
    leaves, tree, root = _build_batch(start, n, size)
    for i in (0, 7, 29):
        leaf = dict(leaves[i], branch=tree.calc_merkle_branch(branch_idx=i).to_str())
        assert verify_leaf_branch(leaf, merkle_root=root, start_nonce=start) is True


def test_verify_leaf_branch_rejects_tampered_leaf():
    start, n, size = 0, 32, 32
    leaves, tree, root = _build_batch(start, n, size)
    branch = tree.calc_merkle_branch(branch_idx=5).to_str()
    tampered = dict(leaves[5], solution='{"i":"better"}', branch=branch)
    assert verify_leaf_branch(tampered, merkle_root=root, start_nonce=start) is False
    # right leaf, wrong position
    misplaced = dict(leaves[5], nonce=6, branch=branch)
    assert verify_leaf_branch(misplaced, merkle_root=root, start_nonce=start) is False
    # right leaf, someone else's root
    assert verify_leaf_branch(dict(leaves[5], branch=branch), merkle_root="11" * 32, start_nonce=start) is False


def test_group_nonces_by_batch():
    assert group_nonces_by_batch([70, 3, 65, 3, 31], batch_size=32) == {0: [3, 31], 2: [65, 70]}


def test_fetch_header_value_compact_and_typed():
    rows = [{"id": "7", "benchmark_id": "abc", "batch_idx": 2, "requested_nonces": ["65", 70]}]
    value = fetch_header_value(rows)
    assert "\n" not in value and " " not in value
    assert json.loads(value) == [{"audit_id": 7, "batch_id": "abc_2", "nonces": [65, 70]}]
    assert fetch_header_value([]) == ""
