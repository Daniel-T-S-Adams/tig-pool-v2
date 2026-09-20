"""Whole-benchmark selection over one complete, validated current snapshot."""

from copy import deepcopy
from dataclasses import dataclass
import secrets

from .money import collateral
from .protocol import ProtocolDataError, Snapshot, _index, _integer, _text


COMPUTE_FAMILIES = {
    "aws_t3": "CPU", "aws_t3a": "CPU", "aws_t4g": "CPU",
    "aws_c7i": "CPU", "aws_c7a": "CPU", "aws_c7g": "CPU",
    "aws_m7i": "CPU", "aws_m7a": "CPU", "aws_m7g": "CPU", "aws_g4dn": "GPU",
}


class NoCompatibleWork(ProtocolDataError):
    pass


@dataclass(frozen=True)
class Selection:
    creation_round: int
    payload: dict
    evidence: dict
    base_collateral: int
    max_submission_fee: int


@dataclass(frozen=True)
class ReferenceIndex:
    block_id: str
    values: dict


def references(snapshot: Snapshot):
    """Index once per snapshot; equal scores use stable benchmark/array order."""
    best = {}
    for bundle in snapshot.bundles:
        key = (bundle.algorithm_id, bundle.track_id)
        previous = best.get(key)
        if previous is None or (-bundle.quality, bundle.benchmark_id, bundle.active_index) < (
                -previous.quality, previous.benchmark_id, previous.active_index):
            best[key] = bundle
    return ReferenceIndex(snapshot.block_id, best)


def _precise(value, label):
    if not isinstance(value, str) or not value.isascii() or not value.isdigit():
        raise ProtocolDataError(f"{label}: expected exact nonnegative units")
    return int(value)


def choose(snapshot, binary_rows, *, player_id, resource, compute_type, now,
           max_age=120, fuel_budget=None, rng=None, reference_index=None):
    try:
        return _choose(snapshot, binary_rows, player_id=player_id, resource=resource,
            compute_type=compute_type, now=now, max_age=max_age, fuel_budget=fuel_budget,
            rng=rng, reference_index=reference_index)
    except (KeyError, TypeError, AttributeError) as exc:
        raise ProtocolDataError(f"incomplete selection data: {exc}") from exc


def _choose(snapshot, binary_rows, *, player_id, resource, compute_type, now,
            max_age, fuel_budget, rng, reference_index):
    if not isinstance(snapshot, Snapshot):
        raise ProtocolDataError("selection requires a validated snapshot")
    if resource not in ("CPU", "GPU") or COMPUTE_FAMILIES.get(compute_type) != resource:
        raise NoCompatibleWork("offer must declare one compatible CPU/GPU verification compute type")
    _integer(now, "current timestamp")
    _integer(max_age, "maximum snapshot age", 1)
    _text(player_id, "pool player ID")
    if snapshot.timestamp > now + 5 or now - snapshot.timestamp > max_age:
        raise ProtocolDataError("current snapshot is stale or from the future")
    binaries = _index(binary_rows, "algorithm_id", "algorithm binaries")
    candidates, pool_counts = {}, {}
    for challenge_id, challenge in snapshot.challenges.items():
        config = challenge["config"]
        family = config["type"]
        if family not in ("cpu", "gpu"):
            raise ProtocolDataError("unknown challenge resource family")
        if family.upper() != resource:
            continue
        if _integer(challenge["state"]["round_active"], "challenge activation", 1) > snapshot.round:
            continue
        if not config["active_tracks"]:
            continue
        usable = []
        for identity, algorithm in snapshot.algorithms.items():
            if algorithm["details"]["challenge_id"] != challenge_id:
                continue
            state = algorithm["state"]
            if type(state["banned"]) is not bool:
                raise ProtocolDataError("algorithm ban status is unknown")
            if state["banned"] or state["round_active"] is None:
                continue
            if _integer(state["round_active"], "algorithm activation", 1) > snapshot.round:
                continue
            if identity not in binaries:
                raise ProtocolDataError("active algorithm is missing binary metadata")
            binary = binaries[identity]
            if binary["details"]["compile_success"] is not True:
                continue
            if _integer(binary["state"]["block_confirmed"], "binary confirmation") > snapshot.height:
                raise ProtocolDataError("binary metadata comes from after this snapshot")
            _text(binary["details"]["download_url"], "algorithm binary URL")
            usable.append(identity)
        if usable:
            candidates[challenge_id] = usable
            pool_counts[challenge_id] = sum(count for (player, challenge, _, _), count in snapshot.qualifiers.items()
                                            if player == player_id and challenge == challenge_id)
    if not candidates:
        raise NoCompatibleWork("no active executable challenge fits this offer")
    rng = rng or secrets.SystemRandom()
    fewest = min(pool_counts.values())
    challenge_ties = sorted(key for key, count in pool_counts.items() if count == fewest)
    challenge_id = rng.choice(challenge_ties)
    adoptions = {key: _precise(snapshot.algorithms[key]["block_data"]["adoption"], "algorithm adoption")
                 for key in candidates[challenge_id]}
    highest = max(adoptions.values())
    algorithm_ties = sorted(key for key, adoption in adoptions.items() if adoption == highest)
    algorithm_id = rng.choice(algorithm_ties)
    config = snapshot.challenges[challenge_id]["config"]
    max_fuel = _integer(config["max_fuel_budget"], "maximum fuel budget")
    fuel = max_fuel if fuel_budget is None else _integer(fuel_budget, "fuel budget")
    if fuel > max_fuel:
        raise ProtocolDataError("configured fuel budget exceeds the protocol maximum")
    default_min = _integer(config["min_num_bundles"], "minimum bundles", 1)
    best = references(snapshot) if reference_index is None else reference_index
    if not isinstance(best, ReferenceIndex) or best.block_id != snapshot.block_id:
        raise ProtocolDataError("benchmark reference index belongs to another block")
    tracks, reference_evidence = {}, {}
    for track_id, track_config in sorted(config["active_tracks"].items()):
        count = _integer(track_config.get("min_num_bundles", default_min), "track minimum bundles", 1) + 1
        reference = best.values.get((algorithm_id, track_id))
        if reference:
            parameters = deepcopy(snapshot.precommits[reference.benchmark_id]["details"]["hyperparameters"])
            reference_evidence[track_id] = {"benchmark_id": reference.benchmark_id,
                "active_index": reference.active_index, "quality": reference.quality}
        else:
            parameters = None
            reference_evidence[track_id] = {"reason": "complete-snapshot-has-no-reference"}
        tracks[track_id] = {"num_bundles": count, "fuel_budget": fuel, "hyperparameters": parameters}
    base, _ = collateral([track["num_bundles"] for track in tracks.values()], "1")
    # Deployed contract uses num_bundles even though the config says per_nonce_fee.
    fee = _precise(config["base_fee"], "base fee") + _precise(config["per_nonce_fee"], "per-bundle fee") * max(
        track["num_bundles"] for track in tracks.values())
    payload = {"settings": {"player_id": player_id, "block_id": snapshot.block_id,
                            "challenge_id": challenge_id, "algorithm_id": algorithm_id, "track_id": ""},
               "track_settings": tracks, "compute_type": compute_type}
    evidence = {"rule": "least-qualifiers-overall-adoption-v1", "block_id": snapshot.block_id,
        "height": snapshot.height, "round": snapshot.round, "resource": resource,
        "compute_type": compute_type, "pool_counts": pool_counts, "challenge_ties": challenge_ties,
        "selected_challenge": challenge_id, "adoption_units": {key: str(value) for key, value in adoptions.items()},
        "algorithm_ties": algorithm_ties, "selected_algorithm": algorithm_id, "references": reference_evidence,
        "binary": deepcopy(binaries[algorithm_id])}
    return Selection(snapshot.round, payload, evidence, base, fee)
