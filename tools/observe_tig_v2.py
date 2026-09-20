#!/usr/bin/env python3
"""Independent read-only collector with local outage spool and durable DB history."""

import argparse
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import signal
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pool_manager.pool_v2.block_observer import BlockStore
from pool_manager.pool_v2.database import Database
from pool_manager.pool_v2.observation import CaptureError, PublicTigClient, capture_snapshot
from pool_manager.pool_v2.protocol import ProtocolDataError, validate_snapshot
from pool_manager.pool_v2.spool import Spool


log = logging.getLogger("innopool-v2-observer")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", default="https://mainnet-api.tig.foundation")
    parser.add_argument("--collector", required=True)
    parser.add_argument("--spool", required=True)
    parser.add_argument("--launch-height", required=True, type=int)
    parser.add_argument("--poll-seconds", type=float, default=3)
    parser.add_argument("--max-block-age", type=int, default=180)
    parser.add_argument("--captures", type=int, default=0, help="stop after this many new blocks; zero runs continuously")
    args = parser.parse_args(argv)
    if not 0 < args.poll_seconds <= 30 or args.max_block_age <= 0 or args.captures < 0:
        parser.error("invalid polling interval, age or capture count")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    database = Database(os.environ.get("POOL_V2_DATABASE_DSN"))
    store, spool = BlockStore(database), Spool(args.spool)
    # Schema migration is a separate deployment operation, not a collector privilege.
    stop = threading.Event()
    for name in (signal.SIGINT, signal.SIGTERM):
        signal.signal(name, lambda *_: stop.set())

    def drain():
        last_alarm, last_alarm_at = None, 0.0
        while not stop.is_set():
            try:
                store.initialize(args.launch_height)
                for path in spool.pending()[:3]:
                    observation, metadata, error = spool.read(path)
                    outcome = store.record(observation, collector=metadata["collector"], metadata=metadata, error=error)
                    spool.recorded(path)
                    log.info("database capture: %s", json.dumps(outcome))
                status = store.status()
                behind = bool(status.get("latest_timestamp") and time.time() - status["latest_timestamp"] > args.max_block_age)
                alarm = (status.get("missing_heights"), status.get("conflicting_heights"), behind)
                if alarm != last_alarm or time.monotonic() - last_alarm_at >= 60:
                    if alarm[0]: log.warning("missing block observations: %s", alarm[0])
                    if alarm[1]: log.error("conflicting block observations: %s", alarm[1])
                    if behind: log.warning("block collection is behind the configured freshness limit")
                    last_alarm, last_alarm_at = alarm, time.monotonic()
            except Exception:
                log.exception("database recording unavailable; local evidence retained")
            stop.wait(3)

    drainer = threading.Thread(target=drain, name="observation-recorder", daemon=True)
    drainer.start()
    last_id, captured = None, 0
    while not stop.is_set():
        # Only this thread captures live data. A database outage, lock, or replay
        # backlog is confined to the separate recorder and cannot stall capture.
        client = PublicTigClient(args.api_url)
        try:
            start = client.get("/get-block", {"include_data": "true"})
            block = start["block"]
            if block["id"] != last_id:
                error, observation = None, {"start": start}
                try:
                    age = time.time() - block["details"]["timestamp"]
                    if age > args.max_block_age or age < -5:
                        # Preserve every observable block even if publication is
                        # delayed. Work selection enforces freshness separately.
                        log.warning("TIG block timestamp differs from wall time by %.1fs; preserving history", age)
                    observation = capture_snapshot(client, start=start)
                except CaptureError as exc:
                    observation, error = exc.observation, str(exc)
                except Exception as exc:
                    error = str(exc)
                path = spool.save(observation, {"collector": args.collector, "requests": client.records,
                    "captured_at": datetime.now(timezone.utc).isoformat()}, error)
                # Invalid captures retry the same head while it remains available.
                try:
                    if error: raise ProtocolDataError(error)
                    validate_snapshot(observation)
                    last_id = block["id"]
                    captured += 1
                except ProtocolDataError:
                    log.warning("incomplete capture for block %s", block["details"]["height"])
                log.info("saved local observation %s", path.name)
        except Exception:
            log.exception("current block capture failed; retrying")
        if args.captures and captured >= args.captures:
            deadline = time.monotonic() + 30
            while spool.pending() and time.monotonic() < deadline and not stop.is_set():
                stop.wait(0.5)
            stop.set()
            drainer.join(timeout=1)
            if spool.pending():
                return 1
            status = store.status()
            return 2 if status.get("missing_heights") or status.get("conflicting_heights") else 0
        stop.wait(args.poll_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
