"""Public reporting evidence, independently captured before database access."""

from datetime import datetime, timezone
import time

from . import reports
from .protocol import ProtocolDataError


def capture(client, reporting_round, *, player_id=None, challenge_id=None, max_block_age=180):
    """One current-block bracket per public request; partial failures are evidence."""
    if bool(player_id) != bool(challenge_id):
        raise ValueError("reportable indices require both a player and a challenge")
    result = {"reporting_round": reporting_round, "kind": "index" if player_id else "reports"}
    if player_id:
        result.update(player_id=player_id, challenge_id=challenge_id)
    error = None
    try:
        result["start"] = client.get("/get-block")
        head = result["start"]["block"]
        if player_id:
            result["payload"] = client.get("/get-reportable-benchmark-ids", {
                "round": reporting_round, "player_id": player_id, "challenge_id": challenge_id})
        else:
            result["payload"] = client.get("/get-reports", {"round": reporting_round})
        result["end"] = client.get("/get-block")
        if result["end"]["block"]["id"] != head["id"]:
            raise ProtocolDataError("report request crossed a block boundary; retry it")
        age = time.time() - head["details"]["timestamp"]
        if not -5 <= age <= max_block_age:
            raise ProtocolDataError("report observation has a stale or future block anchor")
    except Exception as failure:
        error = type(failure).__name__ + ": " + str(failure)
    metadata = {"captured_at": datetime.now(timezone.utc).isoformat(), "requests": client.records}
    return result, metadata, error


def record(database, capture, metadata, error=None):
    """Called by the spool drainer; wait for the independent block archive."""
    try:
        block_id = capture["start"]["block"]["id"]
    except (KeyError, TypeError) as failure:
        raise ProtocolDataError("report evidence is missing its block anchor") from failure
    provenance = {**metadata, "start": capture["start"], "end": capture.get("end")}
    arguments = (database, capture["reporting_round"], block_id)
    options = {"metadata": provenance, "error": error}
    if capture["kind"] == "index":
        return reports.record_index(*arguments, capture["player_id"], capture["challenge_id"],
                                    capture.get("payload", {}), **options)
    if capture["kind"] != "reports":
        raise ProtocolDataError("unknown reporting observation kind")
    return reports.record(*arguments, capture.get("payload", {}), **options)
