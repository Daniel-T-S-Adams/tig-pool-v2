from copy import deepcopy


def observation(height=8, identity=None, previous=None, length=4):
    identity = identity or f"block-{height}"
    previous = previous or f"block-{height-1}"
    player = "0x" + "1" * 40
    configs = {key: {"type": family, "active_tracks": {"t": {"num_nonces_per_bundle": 2, "min_active_quality": 1},
                                                              "empty": {"num_nonces_per_bundle": 2, "min_active_quality": 1}},
                      "min_num_bundles": 4, "max_fuel_budget": 99, "base_fee": "7", "per_nonce_fee": "3"}
               for key, family in (("c1", "cpu"), ("c2", "cpu"), ("g1", "gpu"))}
    block = {"id": identity, "config": {"rounds": {"blocks_per_round": length}},
             "details": {"height": height, "round": height//length+1, "timestamp": 1800000000+height,
                         "prev_block_id": previous, "num_active": {"benchmark": 1, "opow": 1, "code": 3, "challenge": 3}},
             "data": {"active_ids": {"benchmark": ["b"], "opow": [player], "code": ["a1", "a2", "g"], "challenge": list(configs)}}}
    algorithms = [{"id": algorithm, "state": {"banned": False, "round_active": 1},
                   "details": {"challenge_id": challenge,"name":"fixture_"+algorithm}, "block_data": {"adoption": "11",
                       "num_qualifiers_by_track_by_player": {"t": {player: 1}} if algorithm == "a1" else {}}}
                  for algorithm, challenge in (("a1", "c1"), ("a2", "c2"), ("g", "g1"))]
    binaries = [{"algorithm_id": row["id"], "details": {"compile_success": True,
                  "download_url": "https://example.invalid/binary/" + row["id"]}, "state": {"block_confirmed": 1}} for row in algorithms]
    precommit = {"benchmark_id": "b", "state": {"block_confirmed": 2},
        "settings": {"player_id": player, "challenge_id": "c1", "algorithm_id": "a1", "track_id": "t", "block_id": "block-1"},
        "details": {"block_started": 1, "num_bundles": 2, "fuel_budget": 0, "compute_type": "aws_c7a", "hyperparameters": {"tune": "best"}}}
    benchmark = {"id": "b", "state": {"block_confirmed": 3},
        "details": {"stopped": False, "num_active_bundles": 2, "average_quality_by_bundle": [9, 9]}}
    proof = {"benchmark_id": "b", "state": {"block_confirmed": 3}, "details": {"block_active": 4}}
    return {"start": {"block": block}, "end": {"block": deepcopy(block)},
        "algorithms": {"codes": algorithms, "binarys": binaries},
        "challenges": {"challenges": [{"id": key, "config": config, "state": {"round_active": 1},
            "block_data": {"num_qualifiers_by_track": {"t": int(key == "c1"), "empty": 0}}} for key, config in configs.items()]},
        "opow": {"opow": [{"player_id": player, "block_data": {"num_qualifiers_by_challenge_by_track": {"c1": {"t": 1}}}}]},
        "players": {player: {"precommits": [precommit], "benchmarks": [benchmark], "proofs": [proof], "frauds": []}}}
