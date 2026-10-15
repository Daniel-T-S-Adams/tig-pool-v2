"""Validate the whole network once, then retain only pool accounting inputs.

The original block header is preserved. Scoped replay checks pool bundles
against the pool's authoritative qualifier totals; it does not pretend to
recreate the discarded network. Selection keeps each algorithm/track's best
reference, including the parameters and original active-array index.
"""

from collections import Counter
from copy import deepcopy
from dataclasses import asdict, replace
import hashlib
import re

from .observation import canonical_json
from .protocol import (Bundle, ProtocolDataError, _index, _integer, _text,
                       _validate_snapshot, validate_snapshot)
from .selection import references


FORMAT = "pool-network-v1"


def _digest(value):
    if not isinstance(value, str) or not re.fullmatch("[0-9a-f]{64}", value):
        raise ProtocolDataError("compact capture has an invalid source digest")
    return value


def metadata_summary(metadata):
    """Keep request counts and a source digest, rather than every player's URL."""
    requests = metadata.get("requests", [])
    return {key: value for key, value in metadata.items() if key != "requests"} | {
        "request_count": len(requests),
        "requests_sha256": hashlib.sha256(canonical_json(requests)).hexdigest(),
        "endpoints": dict(Counter(row.get("path", "unknown") for row in requests)),
    }


def compact_observation(observation, *, identity_version=2):
    if observation.get("format") == FORMAT:
        validate_compact(observation)
        return observation
    player = _text(observation.get("pool_player_id"), "compact pool player")
    snapshot = validate_snapshot(observation)
    feed = observation["players"].get(player, observation.get("pool_pending"))
    if not isinstance(feed, dict):
        raise ProtocolDataError("compact capture needs the complete pool feed")
    # Legacy block identity hashes include the pool feed's record ordering.
    # Preserve both accepted encodings so recapturing a legacy block cannot
    # create a false conflict during the transition.
    from .block_observer import _semantic, canonical_observation
    canonical = dict(observation)
    if player in observation["players"]:
        canonical["players"] = {player: canonical_observation(feed)}
    else:
        canonical["pool_pending"] = canonical_observation(feed)
    source_digests = {_semantic(snapshot, observation), _semantic(snapshot, canonical)}
    v1_source_digest = _semantic(snapshot, canonical)
    if identity_version == 2:
        # Paged legacy manifests canonicalized header record lists as well.
        # Keep the original header in the payload, but compare its replayable
        # encoding when identifying a block. Retain the v1 identity too.
        canonical['start'] = deepcopy(observation['start'])
        canonical['start']['block']['config'] = canonical_observation(observation['start']['block']['config'])
    elif identity_version != 1:
        raise ValueError('unsupported compact source identity version')
    source_digest = _semantic(snapshot, canonical)
    source_digests = sorted(source_digests | {source_digest})
    feed = canonical_observation(feed)
    pool_active = player in observation["players"]
    codes = []
    for identity, row in sorted(snapshot.algorithms.items()):
        counts = row["block_data"]["num_qualifiers_by_track_by_player"]
        codes.append({"id": identity, "details": deepcopy(row["details"]), "state": deepcopy(row["state"]),
            "block_data": {"adoption": row["block_data"]["adoption"],
                "num_qualifiers_by_track_by_player": {track: {player: values[player]}
                    for track, values in counts.items() if player in values}}})
    selected = references(snapshot)
    best = [{"bundle": asdict(bundle),
             "hyperparameters": deepcopy(snapshot.precommits[bundle.benchmark_id]["details"]["hyperparameters"])}
            for _, bundle in sorted(selected.values.items())]
    result = {"format": FORMAT, "pool_player_id": player, "pool_active": pool_active,
        "source_semantic_digests": source_digests,
        "source_semantic_digest": source_digest,
        "start": {"block": {key: deepcopy(observation["start"]["block"][key]) for key in ("id", "details", "config")}},
        "end": deepcopy(observation["end"]),
        "algorithms": {"codes": codes, "binarys": deepcopy(observation["algorithms"].get("binarys", []))},
        "challenges": {"challenges": [{"id": key, "config": deepcopy(row["config"]),
            "state": deepcopy(row["state"])} for key, row in sorted(snapshot.challenges.items())]},
        "players": {player: deepcopy(feed)} if pool_active else {},
        "pool_active_benchmark_ids": sorted(key for key, row in snapshot.precommits.items()
            if row["settings"]["player_id"] == player),
        "selection_references": best,
        "opow": {"opow": [deepcopy(row) for row in observation["opow"]["opow"] if row["player_id"] == player]},
    }
    # End responses normally have no data; explicitly omit it if supplied.
    result["end"] = {"block": {key: deepcopy(observation["end"]["block"][key]) for key in ("id", "details", "config")}}
    if not pool_active:
        result["pool_pending"] = deepcopy(feed)
    if identity_version == 2:
        result['source_identity_version'] = 2
        # Existing compact observations have immutable v1 projection hashes.
        # Accept both original and legacy header encodings during recapture.
        old = deepcopy(result)
        old.pop('source_identity_version')
        old['source_semantic_digest'] = v1_source_digest
        compatible = {_semantic(validate_compact(old), old)}
        old['start']['block']['config'] = deepcopy(canonical['start']['block']['config'])
        old['end']['block']['config'] = deepcopy(old['start']['block']['config'])
        old['source_semantic_digest'] = source_digest
        compatible.add(_semantic(validate_compact(old), old))
        result['compatible_projection_digests'] = sorted(compatible)
    validate_compact(result)
    return result


def validate_compact(observation):
    if type(observation.get('source_identity_version', 1)) is not int or observation.get('source_identity_version', 1) not in (1, 2):
        raise ProtocolDataError('unsupported compact source identity version')
    player = _text(observation["pool_player_id"], "compact pool player")
    if type(observation["pool_active"]) is not bool:
        raise ProtocolDataError("compact pool activity must be explicit")
    if not observation["source_semantic_digests"]:
        raise ProtocolDataError("compact capture needs its validated network digest")
    for value in observation["source_semantic_digests"]:
        _digest(value)
    for value in observation.get('compatible_projection_digests', []):
        _digest(value)
    if _digest(observation['source_semantic_digest']) not in observation['source_semantic_digests']:
        raise ProtocolDataError('compact canonical source identity is missing')
    start, end = observation["start"]["block"], observation["end"]["block"]
    if any(start[key] != end[key] for key in ("id", "details", "config")):
        raise ProtocolDataError("compact observation crossed a block boundary")
    codes = _index(observation["algorithms"]["codes"], "id", "compact codes")
    challenges = _index(observation["challenges"]["challenges"], "id", "compact challenges")
    feed = observation["players"].get(player, observation.get("pool_pending"))
    if set(observation["players"]) != ({player} if observation["pool_active"] else set()):
        raise ProtocolDataError("compact capture contains an unexpected player feed")
    for name, key in (("precommits", "benchmark_id"), ("benchmarks", "id"),
                      ("proofs", "benchmark_id"), ("frauds", "benchmark_id")):
        records = _index(feed[name], key, "compact pool " + name)
        if name == "precommits" and any(row["settings"]["player_id"] != player for row in records.values()):
            raise ProtocolDataError("compact precommit owner differs from the pool")
    active = observation["pool_active_benchmark_ids"]
    if not isinstance(active, list) or len(set(active)) != len(active):
        raise ProtocolDataError("compact active benchmarks must be unique")
    if active and not observation["pool_active"]:
        raise ProtocolDataError("inactive pool has active benchmark credit")
    original_counts = start["details"]["num_active"]
    if (len(codes) != original_counts["code"] or len(challenges) != original_counts["challenge"]
            or len(active) > original_counts["benchmark"]
            or int(observation["pool_active"]) > original_counts["opow"]):
        raise ProtocolDataError("compact scope contradicts the original block counts")
    totals = Counter()
    for code in codes.values():
        for track, counts in code["block_data"]["num_qualifiers_by_track_by_player"].items():
            if set(counts) - {player}:
                raise ProtocolDataError("compact qualifiers include another player")
            totals[code["details"]["challenge_id"], track] += _integer(counts.get(player, 0), "pool qualifiers")
    # Use the existing strict bundle/confirmation/total validator on an
    # explicitly scoped view. The returned snapshot keeps the actual header.
    view = deepcopy(observation)
    scoped = view["start"]["block"]
    scoped["data"] = {"active_ids": {"code": sorted(codes), "challenge": sorted(challenges),
        "opow": [player] if observation["pool_active"] else [], "benchmark": active}}
    scoped["details"]["num_active"] = {key: len(ids) for key, ids in scoped["data"]["active_ids"].items()}
    view["end"]["block"]["details"] = deepcopy(scoped["details"])
    for challenge in view["challenges"]["challenges"]:
        challenge["block_data"] = {"num_qualifiers_by_track": {track: totals[challenge["id"], track]
            for track in challenge["config"]["active_tracks"]}}
    snapshot = _validate_snapshot(view)
    best = {}
    for row in observation["selection_references"]:
        bundle = Bundle(**row["bundle"])
        pair = bundle.algorithm_id, bundle.track_id
        if pair in best or bundle.algorithm_id not in codes or bundle.challenge_id not in challenges:
            raise ProtocolDataError("compact selection reference has an invalid scope")
        if (codes[bundle.algorithm_id]["details"]["challenge_id"] != bundle.challenge_id
                or bundle.track_id not in challenges[bundle.challenge_id]["config"]["active_tracks"]):
            raise ProtocolDataError("compact selection reference has an invalid algorithm/track")
        _text(bundle.benchmark_id, "reference benchmark")
        _text(bundle.player_id, "reference player")
        _integer(bundle.active_index, "reference bundle index")
        if not -(2**31) <= _integer(bundle.quality, "reference quality", -(2**31)) < 2**31:
            raise ProtocolDataError("reference quality exceeds int32")
        parameters = row["hyperparameters"]
        if parameters is not None and not isinstance(parameters, dict):
            raise ProtocolDataError("compact reference hyperparameters are invalid")
        best[pair] = {"bundle": bundle, "hyperparameters": parameters}
    for bundle in snapshot.bundles:
        value = best.get((bundle.algorithm_id, bundle.track_id))
        if value is None:
            raise ProtocolDataError("compact selection summary omits an active pool reference")
        reference = value["bundle"]
        if (-reference.quality, reference.benchmark_id, reference.active_index) > (-bundle.quality, bundle.benchmark_id, bundle.active_index):
            raise ProtocolDataError("compact reference is worse than an available pool bundle")
        if reference.benchmark_id in snapshot.precommits and value["hyperparameters"] != snapshot.precommits[reference.benchmark_id]["details"]["hyperparameters"]:
            raise ProtocolDataError("compact pool reference parameters differ from its precommit")
    return replace(snapshot, selection_references=best)
