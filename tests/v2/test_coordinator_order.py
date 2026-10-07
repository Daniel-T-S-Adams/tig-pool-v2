from contextlib import contextmanager
import unittest
from unittest.mock import Mock, patch

from pool_manager.pool_v2.coordinator import Coordinator


class Cursor:
    def execute(self, _query):
        pass

    def fetchone(self):
        return {"id": "latest-block"}


class Database:
    @contextmanager
    def transaction(self):
        yield Cursor()


class CoordinatorOrderTests(unittest.TestCase):
    def test_live_work_precedes_one_block_of_historical_replay(self):
        events = []
        coordinator = Coordinator(Database(), "pool-player", Mock(), Mock(enabled=True), new_work=True)

        def replay(_database, _player_id, *, limit, **_kwargs):
            events.append(("history", limit))
            return {"completed": [], "held": []}

        with patch("pool_manager.pool_v2.coordinator.reconcile_block",
                   side_effect=lambda _db, block_id, *_args, **_kwargs: events.append(("current", block_id))), \
             patch("pool_manager.pool_v2.coordinator.reconcile_pending", side_effect=replay), \
             patch.object(coordinator, "dispatch_one", side_effect=lambda: events.append(("dispatch",))), \
             patch("pool_manager.pool_v2.coordinator.controls.blocked", return_value=None), \
             patch("pool_manager.pool_v2.coordinator.work_requests.reserve_next",
                   side_effect=lambda *_args, **_kwargs: events.append(("reserve",)) or {"id": "reservation-1"}):
            result = coordinator.step()

        self.assertEqual(events, [
            ("current", "latest-block"),
            ("dispatch",),
            ("reserve",),
            ("history", 1),
        ])
        self.assertEqual(result["reserved"], "reservation-1")


if __name__ == "__main__":
    unittest.main()
