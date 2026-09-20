"""Read-only collection and durable archives for the Stage 0 protocol probe."""

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from .protocol import ProtocolDataError


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


class PublicTigClient:
    """GET-only client. It cannot submit work, move tokens, or use a TIG API key."""

    def __init__(self, base_url, timeout=20, max_response_bytes=32 * 1024 * 1024):
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or (
            parsed.username or parsed.password or parsed.query or parsed.fragment
        ):
            raise ValueError("supply an HTTP(S) API base URL without credentials, query, or fragment")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_response_bytes = max_response_bytes
        self.records = []
        self._lock = threading.Lock()

    def get(self, path, params=None):
        if not path.startswith("/get-") or "?" in path or "#" in path:
            raise ValueError("the probe accepts only public /get- endpoints")
        params = params or {}
        url = self.base_url + path + ("?" + urlencode(params) if params else "")
        request = Request(url, headers={
            "Accept": "application/json", "Accept-Encoding": "identity",
            "Cache-Control": "no-cache", "User-Agent": "innopool-v2-readonly-probe/0.1",
        })
        with urlopen(request, timeout=self.timeout) as response:
            raw = response.read(self.max_response_bytes + 1)
        if len(raw) > self.max_response_bytes:
            raise ProtocolDataError(f"{path}: response exceeds the configured size limit")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ProtocolDataError(f"{path}: response is not a JSON object")
        with self._lock:
            self.records.append({
                "path": path, "params": params,
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "canonical_sha256": hashlib.sha256(canonical_json(payload)).hexdigest(),
            })
        return payload


class CaptureError(ProtocolDataError):
    def __init__(self, message, observation):
        super().__init__(message)
        self.observation = observation


def capture_snapshot(client, start=None, max_workers=4):
    """Collect complete player feeds between two reads of the same block.

    The caller validates the result. Partial successful responses are retained
    in CaptureError so the probe can archive failed attempts as incomplete.
    """
    observation = {"players": {}}
    try:
        observation["start"] = start or client.get("/get-block", {"include_data": "true"})
        block = observation["start"]["block"]
        block_id = block["id"]
        players = block["data"]["active_ids"]["opow"]
        if not isinstance(players, list) or len(set(players)) != len(players):
            raise ProtocolDataError("invalid active player IDs")
        requests = [
            ("algorithms", None, "/get-algorithms", {"block_id": block_id}),
            ("challenges", None, "/get-challenges", {"block_id": block_id}),
            ("opow", None, "/get-opow", {"block_id": block_id}),
        ] + [
            ("players", player, "/get-benchmarks", {"block_id": block_id, "player_id": player})
            for player in players
        ]
        errors = []
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(client.get, path, params): (name, player)
                for name, player, path, params in requests
            }
            for future in as_completed(futures):
                name, player = futures[future]
                try:
                    data = future.result()
                    if name == "players":
                        observation[name][player] = data
                    else:
                        observation[name] = data
                except Exception as error:
                    errors.append(f"{name}: {error}")
        observation["end"] = client.get("/get-block")
        if errors:
            raise ProtocolDataError("; ".join(errors))
        return observation
    except Exception as error:
        raise CaptureError(str(error), observation) from error


def write_archive(destination, payload):
    """Atomically store a compressed, replayable JSON observation."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(gzip.compress(canonical_json(payload), mtime=0))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        descriptor = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def read_archive(path):
    with gzip.open(path, "rt") as stream:
        return json.load(stream)
