"""Validate observed TIG data before using it for v2 accounting.

This module is deliberately independent of the legacy scheduler and database.
An observation contains raw responses fetched between two reads of one block.
It does not establish member ownership, receipt of money, or round settlement.
"""

from collections import Counter, defaultdict
from dataclasses import dataclass
from fractions import Fraction
from itertools import groupby
from typing import Any


class ProtocolDataError(ValueError):
    """An observation cannot support a complete, consistent calculation."""


def _object(value, label):
    if not isinstance(value, dict):
        raise ProtocolDataError(f"{label}: expected an object")
    return value


def _array(value, label):
    if not isinstance(value, list):
        raise ProtocolDataError(f"{label}: expected an array")
    return value


def _integer(value, label, minimum=0):
    if type(value) is not int or value < minimum:
        raise ProtocolDataError(f"{label}: expected an integer >= {minimum}")
    return value


def _text(value, label):
    if not isinstance(value, str) or not value:
        raise ProtocolDataError(f"{label}: expected a nonempty string")
    return value


def _index(rows, key, label):
    result = {}
    for row in _array(rows, label):
        row = _object(row, label)
        identity = _text(row.get(key), f"{label}.{key}")
        if identity in result:
            raise ProtocolDataError(f"{label}: duplicate {identity}")
        result[identity] = row
    return result


def _counts(value, label):
    return {
        _text(k, label): _integer(v, label)
        for k, v in _object(value, label).items()
    }


def _nonzero(mapping):
    return {key: value for key, value in mapping.items() if value}


@dataclass(frozen=True)
class Bundle:
    benchmark_id: str
    # Position in TIG's active-quality array, not an original nonce/bundle ID.
    active_index: int
    player_id: str
    challenge_id: str
    algorithm_id: str
    track_id: str
    quality: int

    @property
    def group(self):
        return self.player_id, self.challenge_id, self.algorithm_id, self.track_id


@dataclass(frozen=True)
class Snapshot:
    block_id: str
    previous_block_id: str
    height: int
    round: int
    timestamp: int
    bundles: tuple[Bundle, ...]
    qualifiers: dict[tuple[str, str, str, str], int]
    precommits: dict[str, dict[str, Any]]
    algorithms: dict[str, dict[str, Any]]
    challenges: dict[str, dict[str, Any]]


def validate_snapshot(observation: dict) -> Snapshot:
    """Reject mixed blocks, missing active benchmarks, and mismatched totals.

    All current OPoW players are queried through /get-benchmarks. Its compact
    response includes the active bundle scores and precommit parameters without
    the large solution/proof bodies returned by /get-benchmark-data. Coverage
    is checked against the block's authoritative active benchmark ID list.
    """
    try:
        return _validate_snapshot(observation)
    except (KeyError, TypeError, AttributeError) as error:
        raise ProtocolDataError(f"missing or malformed protocol field: {error}") from error


def _validate_snapshot(observation):
    start = _object(observation["start"]["block"], "start.block")
    end = _object(observation["end"]["block"], "end.block")
    details = _object(start["details"], "block.details")
    block_id = _text(start["id"], "block.id")
    if end["id"] != block_id or end["details"] != details or end["config"] != start["config"]:
        raise ProtocolDataError("block changed while collecting the observation")
    height = _integer(details["height"], "block.height")
    round_number = _integer(details["round"], "block.round", 1)
    timestamp = _integer(details["timestamp"], "block.timestamp")
    previous = _text(details["prev_block_id"], "block.prev_block_id")
    active = _object(start["data"]["active_ids"], "block.active_ids")
    ids = {}
    for kind in ("benchmark", "opow", "code", "challenge"):
        values = _array(active[kind], f"active_ids.{kind}")
        ids[kind] = {_text(value, kind) for value in values}
        expected_count = _integer(details["num_active"][kind], f"num_active.{kind}")
        if len(ids[kind]) != len(values) or len(values) != expected_count:
            raise ProtocolDataError(f"active {kind} ID count is inconsistent")

    algorithms = _index(observation["algorithms"]["codes"], "id", "codes")
    challenges = _index(observation["challenges"]["challenges"], "id", "challenges")
    opow = _index(observation["opow"]["opow"], "player_id", "opow")
    for kind, rows in (("code", algorithms), ("challenge", challenges), ("opow", opow)):
        if not ids[kind] <= rows.keys():
            raise ProtocolDataError(f"missing active {kind} records")
    algorithms = {key: algorithms[key] for key in ids["code"]}
    challenges = {key: challenges[key] for key in ids["challenge"]}
    players = _object(observation["players"], "players")
    if set(players) != ids["opow"]:
        raise ProtocolDataError("player observations do not cover the active OPoW players")

    precommits, benchmarks, proofs = {}, {}, {}
    frauds = set()
    for player_id, payload in players.items():
        for field, key, target in (
            ("precommits", "benchmark_id", precommits),
            ("benchmarks", "id", benchmarks),
            ("proofs", "benchmark_id", proofs),
        ):
            rows = _index(payload[field], key, field)
            if target.keys() & rows.keys():
                raise ProtocolDataError(f"{field}: record belongs to multiple player responses")
            if field == "precommits":
                if any(row["settings"]["player_id"] != player_id for row in rows.values()):
                    raise ProtocolDataError("precommit owner differs from the requested player")
            target.update(rows)
        frauds.update(_index(payload["frauds"], "benchmark_id", "frauds"))

    active_benchmarks = ids["benchmark"]
    for name, rows in (("precommits", precommits), ("benchmarks", benchmarks), ("proofs", proofs)):
        missing = active_benchmarks - rows.keys()
        if missing:
            raise ProtocolDataError(f"missing {len(missing)} active benchmark {name}")
    if frauds & active_benchmarks:
        raise ProtocolDataError("a failed-verification benchmark appears in the active set")

    bundles = []
    for benchmark_id in sorted(active_benchmarks):
        precommit, benchmark, proof = precommits[benchmark_id], benchmarks[benchmark_id], proofs[benchmark_id]
        settings, pre_details = precommit["settings"], precommit["details"]
        benchmark_details = benchmark["details"]
        for name, record in (("precommit", precommit), ("benchmark", benchmark), ("proof", proof)):
            confirmed = _integer(record["state"]["block_confirmed"], f"{name}.block_confirmed")
            if confirmed > height:
                raise ProtocolDataError(f"{name} confirmed after the observed block")
        activated = _integer(proof["details"]["block_active"], "proof.block_active")
        if activated > height or benchmark_details["stopped"] is not False:
            raise ProtocolDataError("active benchmark has an incompatible lifecycle state")
        _integer(pre_details["block_started"], "precommit.block_started")
        _integer(pre_details["num_bundles"], "precommit.num_bundles", 1)
        _integer(pre_details["fuel_budget"], "precommit.fuel_budget")
        _text(pre_details["compute_type"], "precommit.compute_type")
        # A present null invokes algorithm defaults. An absent field is a gap.
        if "hyperparameters" not in pre_details or (
            pre_details["hyperparameters"] is not None
            and not isinstance(pre_details["hyperparameters"], dict)
        ):
            raise ProtocolDataError("missing or invalid precommit hyperparameters")
        challenge_id = settings["challenge_id"]
        algorithm_id = settings["algorithm_id"]
        track_id = settings["track_id"]
        if challenge_id not in challenges or algorithm_id not in algorithms:
            raise ProtocolDataError("active benchmark refers to an unavailable challenge or algorithm")
        if algorithms[algorithm_id]["details"]["challenge_id"] != challenge_id:
            raise ProtocolDataError("benchmark algorithm/challenge mismatch")
        scores = _array(benchmark_details["average_quality_by_bundle"], "bundle scores")
        active_count = _integer(benchmark_details["num_active_bundles"], "num_active_bundles", 1)
        if len(scores) != active_count or active_count > pre_details["num_bundles"]:
            raise ProtocolDataError("active bundle count differs from score count")
        # A track can retire while an old benchmark remains in the live feed.
        if track_id not in challenges[challenge_id]["config"]["active_tracks"]:
            continue
        for index, quality in enumerate(scores):
            _integer(quality, "bundle quality", -(2**31))
            if quality >= 2**31:
                raise ProtocolDataError("bundle quality exceeds the protocol int32 range")
            bundles.append(Bundle(benchmark_id, index, settings["player_id"], challenge_id,
                                  algorithm_id, track_id, quality))

    candidates = Counter(bundle.group for bundle in bundles)
    qualifiers = {}
    by_player = Counter()
    by_challenge = Counter()
    for algorithm_id, algorithm in algorithms.items():
        block_data = _object(algorithm["block_data"], "algorithm.block_data")
        adoption = block_data["adoption"]
        if not isinstance(adoption, str) or not adoption.isascii() or not adoption.isdigit():
            raise ProtocolDataError("algorithm adoption must be an exact nonnegative unit string")
        challenge_id = algorithm["details"]["challenge_id"]
        for track_id, counts in _object(block_data["num_qualifiers_by_track_by_player"], "algorithm qualifiers").items():
            for player_id, count in _counts(counts, "algorithm player qualifiers").items():
                key = (player_id, challenge_id, algorithm_id, track_id)
                if count > candidates[key]:
                    raise ProtocolDataError("qualifying count exceeds the available eligible bundles")
                qualifiers[key] = count
                by_player[player_id, challenge_id, track_id] += count
                by_challenge[challenge_id, track_id] += count

    expected_players = {}
    for player_id in ids["opow"]:
        counts = _object(opow[player_id]["block_data"]["num_qualifiers_by_challenge_by_track"], "OPoW qualifiers")
        for challenge_id, tracks in counts.items():
            for track_id, count in _counts(tracks, "OPoW track qualifiers").items():
                expected_players[player_id, challenge_id, track_id] = count
    if _nonzero(by_player) != _nonzero(expected_players):
        raise ProtocolDataError("algorithm qualifiers do not reconcile with OPoW player totals")
    expected_challenges = {}
    for challenge_id, challenge in challenges.items():
        counts = _counts(challenge["block_data"]["num_qualifiers_by_track"], "challenge qualifiers")
        if set(counts) != set(challenge["config"]["active_tracks"]):
            raise ProtocolDataError("challenge qualifier totals omit an active track")
        for track_id, count in counts.items():
            expected_challenges[challenge_id, track_id] = count
    if _nonzero(by_challenge) != _nonzero(expected_challenges):
        raise ProtocolDataError("algorithm qualifiers do not reconcile with challenge totals")
    return Snapshot(block_id, previous, height, round_number, timestamp, tuple(bundles), qualifiers,
                    {key: precommits[key] for key in active_benchmarks}, algorithms, challenges)


def equal_bundle_credit(snapshot: Snapshot) -> dict[tuple[str, int], Fraction]:
    """Apply D4 to validated candidates; these are credits, not token amounts."""
    grouped = defaultdict(list)
    for bundle in snapshot.bundles:
        grouped[bundle.group].append(bundle)
    credits = {}
    for key, bundles in grouped.items():
        remaining = snapshot.qualifiers.get(key, 0)
        ordered = sorted(bundles, key=lambda b: (-b.quality, b.benchmark_id, b.active_index))
        for _, tied in groupby(ordered, key=lambda b: b.quality):
            tied = list(tied)
            awarded = min(remaining, len(tied))
            share = Fraction(awarded, len(tied))
            for bundle in tied:
                credits[bundle.benchmark_id, bundle.active_index] = share
            remaining -= awarded
        if remaining:
            raise ProtocolDataError("unallocated qualifying credit")
    if sum(credits.values(), Fraction()) != sum(snapshot.qualifiers.values()):
        raise ProtocolDataError("qualifying credit does not reconcile exactly")
    return credits


def validate_reports(payload: dict) -> dict[str, dict]:
    """Preserve pending, inconclusive, reproducible, and upheld as distinct states."""
    try:
        reports = _index(payload["reports"], "id", "reports")
        arbitrations = _index(payload["arbitrations"], "report_id", "arbitrations")
        if not arbitrations.keys() <= reports.keys():
            raise ProtocolDataError("arbitration has no matching report")
        results = {}
        for report_id, report in reports.items():
            details = _object(report["details"], "report.details")
            _text(details["benchmark_id"], "report.benchmark_id")
            _text(details["benchmarker"], "report.benchmarker")
            _integer(details["nonce"], "report.nonce")
            _integer(details["round"], "report.round", 1)
            confirmed = None
            if report.get("state") is not None:
                confirmed = _integer(report["state"]["block_confirmed"], "report.block_confirmed")
            arbitration = arbitrations.get(report_id)
            result = None
            if arbitration is not None:
                result = arbitration["details"]["result"]
                if result not in {"nonreproducible", "reproducible", "inconclusive"}:
                    raise ProtocolDataError(f"unknown arbitration result: {result}")
                # A decision without authoritative confirmation is still pending.
                if arbitration.get("state") is None:
                    result = None
                else:
                    decided = _integer(arbitration["state"]["block_confirmed"], "arbitration.block_confirmed")
                    if confirmed is None or decided < confirmed:
                        raise ProtocolDataError("arbitration confirmation precedes its report")
            results[report_id] = {**details, "result": result}
        return results
    except (KeyError, TypeError, AttributeError) as error:
        raise ProtocolDataError(f"missing or malformed report field: {error}") from error
