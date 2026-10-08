from copy import deepcopy
from pathlib import Path
import random
import unittest

from pool_manager.pool_v2.money import TIG
from pool_manager.pool_v2.observation import read_archive
from pool_manager.pool_v2.protocol import ProtocolDataError, validate_snapshot
from pool_manager.pool_v2.selection import NoCompatibleWork, choose
from observer_helpers import observation


class SelectionTests(unittest.TestCase):
    def select(self, data, resource="CPU", player=None, **kwargs):
        snapshot = validate_snapshot(data)
        return choose(snapshot, data["algorithms"]["binarys"], player_id=player or "0x" + "1"*40,
                      resource=resource, compute_type="aws_c7a" if resource == "CPU" else "aws_g4dn",
                      now=snapshot.timestamp, rng=random.Random(42), **kwargs)

    def test_resource_filter_then_fewest_pool_qualifiers(self):
        data = observation()
        cpu = self.select(data)
        gpu = self.select(data, "GPU")
        self.assertEqual(cpu.payload["settings"]["challenge_id"], "c2")
        self.assertEqual(gpu.payload["settings"]["challenge_id"], "g1")
        self.assertEqual(cpu.payload["settings"]["track_id"], "")
        self.assertEqual(cpu.base_collateral, 40*TIG)
        self.assertEqual(cpu.max_submission_fee, 7 + 3*4)
        for track in cpu.payload["track_settings"].values():
            self.assertEqual(track, {"num_bundles": 4, "fuel_budget": 99, "hyperparameters": None})

    def test_uses_track_minimum_when_present_and_challenge_minimum_otherwise(self):
        data = observation()
        data["challenges"]["challenges"][1]["config"]["active_tracks"]["t"]["min_num_bundles"] = 2
        result = self.select(data)
        self.assertEqual(result.payload["track_settings"]["t"]["num_bundles"], 2)
        self.assertEqual(result.payload["track_settings"]["empty"]["num_bundles"], 4)

    def test_highest_overall_adoption_uses_null_hyperparameter_defaults(self):
        data = observation()
        # Force c1 to be the only eligible CPU challenge.
        data["challenges"]["challenges"][1]["config"]["type"] = "gpu"
        extra = deepcopy(data["algorithms"]["codes"][0])
        extra["id"] = "higher"
        extra["block_data"] = {"adoption": "1000000000000000001", "num_qualifiers_by_track_by_player": {}}
        data["algorithms"]["codes"][0]["block_data"]["adoption"] = "1000000000000000000"
        data["algorithms"]["codes"].append(extra)
        binary = deepcopy(data["algorithms"]["binarys"][0]); binary["algorithm_id"] = "higher"
        data["algorithms"]["binarys"].append(binary)
        for side in ("start", "end"):
            data[side]["block"]["data"]["active_ids"]["code"].append("higher")
            data[side]["block"]["details"]["num_active"]["code"] += 1
        result = self.select(data)
        self.assertEqual(result.payload["settings"]["algorithm_id"], "higher")
        self.assertIsNone(result.payload["track_settings"]["t"]["hyperparameters"])
        extra["state"]["banned"] = True
        result = self.select(data)
        self.assertEqual(result.payload["settings"]["algorithm_id"], "a1")
        self.assertIsNone(result.payload["track_settings"]["t"]["hyperparameters"])
        self.assertEqual(result.evidence["hyperparameter_policy"], "algorithm-default-null-v1")
        self.assertIsNone(result.payload["track_settings"]["empty"]["hyperparameters"])

    def test_missing_binary_stale_data_and_resource_mismatch_fail(self):
        data = observation()
        snapshot = validate_snapshot(data)
        with self.assertRaisesRegex(ProtocolDataError, "missing binary"):
            choose(snapshot, [], player_id="pool", resource="CPU", compute_type="aws_c7a", now=snapshot.timestamp)
        with self.assertRaisesRegex(ProtocolDataError, "stale"):
            choose(snapshot, data["algorithms"]["binarys"], player_id="pool", resource="CPU", compute_type="aws_c7a", now=snapshot.timestamp+200)
        with self.assertRaises(NoCompatibleWork):
            choose(snapshot, data["algorithms"]["binarys"], player_id="pool", resource="GPU", compute_type="aws_c7a", now=snapshot.timestamp)

    def test_uniform_tie_uses_all_candidates_without_extra_weighting(self):
        data = observation()
        snapshot = validate_snapshot(data)
        class RecordedDraw:
            def __init__(self): self.options = []
            def choice(self, values):
                self.options.append(list(values)); return values[-1]
        draw = RecordedDraw()
        result = choose(snapshot, data["algorithms"]["binarys"], player_id="new-pool", resource="CPU",
                        compute_type="aws_c7a", now=snapshot.timestamp, rng=draw)
        self.assertEqual(draw.options[0], ["c1", "c2"])
        self.assertEqual(result.payload["settings"]["challenge_id"], "c2")

    def test_live_recorded_snapshot_uses_null_hyperparameters_for_cpu_and_gpu(self):
        data = read_archive(next((Path(__file__).parent / "fixtures").glob("block-1351111*")))["observation"]
        snapshot = validate_snapshot(data)
        for resource in ("CPU", "GPU"):
            result = self.select(data, resource, player="new-pool")
            chosen = result.payload["settings"]
            self.assertEqual(snapshot.challenges[chosen["challenge_id"]]["config"]["type"], resource.lower())
            for track in result.payload["track_settings"].values():
                self.assertIsNone(track["hyperparameters"])
