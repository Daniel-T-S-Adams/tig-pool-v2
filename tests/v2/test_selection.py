from copy import deepcopy
from pathlib import Path
import random
import unittest

from pool_manager.pool_v2.money import TIG
from pool_manager.pool_v2.observation import read_archive
from pool_manager.pool_v2.protocol import ProtocolDataError, validate_snapshot
from pool_manager.pool_v2.selection import NoCompatibleWork, choose, references
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
        self.assertEqual(cpu.base_collateral, 50*TIG)
        self.assertEqual(cpu.max_submission_fee, 7 + 3*5)
        for track in cpu.payload["track_settings"].values():
            self.assertEqual(track, {"num_bundles": 5, "fuel_budget": 99, "hyperparameters": None})

    def test_highest_overall_adoption_and_best_current_reference(self):
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
        self.assertEqual(result.payload["track_settings"]["t"]["hyperparameters"], {"tune": "best"})
        self.assertEqual(result.evidence["references"]["t"]["active_index"], 0)
        self.assertIsNone(result.payload["track_settings"]["empty"]["hyperparameters"])

    def test_missing_binary_stale_data_and_mismatched_reference_index_fail(self):
        data = observation()
        snapshot = validate_snapshot(data)
        with self.assertRaisesRegex(ProtocolDataError, "missing binary"):
            choose(snapshot, [], player_id="pool", resource="CPU", compute_type="aws_c7a", now=snapshot.timestamp)
        with self.assertRaisesRegex(ProtocolDataError, "stale"):
            choose(snapshot, data["algorithms"]["binarys"], player_id="pool", resource="CPU", compute_type="aws_c7a", now=snapshot.timestamp+200)
        with self.assertRaises(NoCompatibleWork):
            choose(snapshot, data["algorithms"]["binarys"], player_id="pool", resource="GPU", compute_type="aws_c7a", now=snapshot.timestamp)
        with self.assertRaisesRegex(ProtocolDataError, "another block"):
            self.select(data, reference_index=references(validate_snapshot(observation(9))))

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

    def test_live_recorded_snapshot_selects_cpu_and_gpu_with_exact_references(self):
        data = read_archive(next((Path(__file__).parent / "fixtures").glob("block-1351111*")))["observation"]
        snapshot = validate_snapshot(data)
        index = references(snapshot)
        for resource in ("CPU", "GPU"):
            result = self.select(data, resource, player="new-pool", reference_index=index)
            chosen = result.payload["settings"]
            self.assertEqual(snapshot.challenges[chosen["challenge_id"]]["config"]["type"], resource.lower())
            for track_id, track in result.payload["track_settings"].items():
                ref = result.evidence["references"][track_id]
                if "benchmark_id" in ref:
                    self.assertEqual(track["hyperparameters"], snapshot.precommits[ref["benchmark_id"]]["details"]["hyperparameters"])
