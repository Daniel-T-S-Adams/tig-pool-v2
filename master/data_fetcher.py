import json
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, Optional

import requests
from common.structs import *
from common.utils import *
from master.client_manager import CONFIG

logger = logging.getLogger(os.path.splitext(os.path.basename(__file__))[0])

def light_payload_from_cache(block, prev: Optional[dict], generation: int) -> dict:
    """New block header plus last-good TIG maps. Does not wait on tracks."""
    src = prev or {
        "algorithms": {},
        "binarys": {},
        "precommits": {},
        "benchmarks": {},
        "proofs": {},
        "frauds": {},
        "challenges": {},
        "tracks_data": {},
    }
    return {
        "block": block,
        "algorithms": src.get("algorithms") or {},
        "binarys": src.get("binarys") or {},
        "precommits": src.get("precommits") or {},
        "benchmarks": src.get("benchmarks") or {},
        "proofs": src.get("proofs") or {},
        "frauds": src.get("frauds") or {},
        "challenges": src.get("challenges") or {},
        "tracks_data": src.get("tracks_data") or {},
        "generation": int(generation),
        "complete": False,
    }


def _get(url: str) -> Dict[str, Any]:
    logger.debug(f"Fetching from {url}")
    resp = requests.get(url, timeout=10)
    if resp.status_code == 200:
        return json.loads(resp.text)
    else:
        if resp.headers.get("Content-Type") == "text/plain":
            err_msg = f"status code {resp.status_code} from {url}: {resp.text}"
        else:
            err_msg = f"status code {resp.status_code} from {url}"
        logger.error(err_msg)
        raise Exception(err_msg)


def _get_safe(url: str) -> Optional[Dict[str, Any]]:
    """Like _get but catches timeouts/errors and logs a clean warning instead of crashing."""
    try:
        return _get(url)
    except requests.exceptions.ReadTimeout:
        endpoint = url.split("?")[0].split("/")[-1]
        logger.warning(f"WARNING - TIG API timeout ({endpoint}) — will retry next cycle")
        return None
    except Exception as exc:
        endpoint = url.split("?")[0].split("/")[-1]
        logger.warning(f"WARNING - TIG API error ({endpoint}): {exc} — will retry next cycle")
        return None


class DataFetcher:
    def __init__(self):
        self.last_fetch = 0
        self._cache = None
        self._lock = threading.Lock()
        self._generation = 0
        self._bg_thread = None
        self._bg_block_id = None

    def run(self) -> dict:
        config = CONFIG
        logger.debug("fetching latest block")
        block_data = _get_safe(f"{config['api_url']}/get-block")
        if not block_data or not block_data.get("block"):
            with self._lock:
                cache = self._cache
            if cache is not None:
                logger.warning(
                    "get-block failed; using cached block so creates continue"
                )
                return cache
            raise Exception("get-block failed and no cached block")
        block = Block.from_dict(block_data["block"])

        with self._lock:
            cache = self._cache
            if cache is not None and block.id == cache["block"].id:
                return cache

        light = self._publish_light(block)
        self._start_background_fetch(block, config)
        return light

    def _publish_light(self, block) -> dict:
        with self._lock:
            self._generation += 1
            payload = light_payload_from_cache(block, self._cache, self._generation)
            self._cache = payload
        logger.info(
            "new block @ height %s, creating with cached TIG data while fetch continues",
            block.details.height,
        )
        return payload

    def _start_background_fetch(self, block, config) -> None:
        if (
            self._bg_thread is not None
            and self._bg_thread.is_alive()
            and self._bg_block_id == block.id
        ):
            return
        self._bg_block_id = block.id
        self._bg_thread = threading.Thread(
            target=self._background_fetch,
            args=(block, config),
            daemon=True,
            name=f"tig-fetch-{block.details.height}",
        )
        self._bg_thread.start()

    def _background_fetch(self, block, config) -> None:
        try:
            payload = self._fetch_full(block, config)
        except Exception as exc:
            logger.warning(
                "background TIG fetch failed for height %s: %s",
                block.details.height,
                exc,
            )
            return
        with self._lock:
            current = self._cache
            if current is not None and current["block"].id != block.id:
                logger.info(
                    "discarding stale background fetch for height %s",
                    block.details.height,
                )
                return
            self._generation += 1
            payload["generation"] = self._generation
            payload["complete"] = True
            self._cache = payload
        logger.info("background TIG fetch complete for height %s", block.details.height)

    def _fetch_full(self, block, config) -> dict:
        logger.info(f"new block @ height {block.details.height}, fetching data")
        tasks = [
            f"{config['api_url']}/get-algorithms?block_id={block.id}",
            f"{config['api_url']}/get-benchmarks?player_id={config['player_id']}&block_id={block.id}",
            f"{config['api_url']}/get-challenges?block_id={block.id}",
        ]

        with ThreadPoolExecutor(max_workers=4) as executor:
            algorithms_data, benchmarks_data, challenges_data = list(executor.map(_get, tasks))

        algorithms = {a["id"]: Code.from_dict(a) for a in algorithms_data["codes"]}
        binarys = {w["algorithm_id"]: Binary.from_dict(w) for w in algorithms_data["binarys"]}

        precommits = {b["benchmark_id"]: Precommit.from_dict(b) for b in benchmarks_data["precommits"]}
        benchmarks = {b["id"]: Benchmark.from_dict(b) for b in benchmarks_data["benchmarks"]}
        proofs = {p["benchmark_id"]: Proof.from_dict(p) for p in benchmarks_data["proofs"]}
        frauds = {f["benchmark_id"]: Fraud.from_dict(f) for f in benchmarks_data["frauds"]}
        challenges = {
            c["id"]: Challenge.from_dict(c)
            for c in challenges_data["challenges"]
            if c["state"]["round_active"] <= block.details.round
        }

        tracks_urls = [
            f"{config['api_url']}/get-tracks-data?block_id={block.id}&challenge_id={c_id}"
            for c_id in challenges
        ]

        with ThreadPoolExecutor(max_workers=4) as executor:
            tracks_responses = list(executor.map(_get_safe, tracks_urls))

        with self._lock:
            prev_tracks = (self._cache or {}).get("tracks_data") or {}

        if any(r is None for r in tracks_responses):
            logger.warning("WARNING - TIG API tracks fetch incomplete — using cached tracks")
            if prev_tracks:
                tracks_data = prev_tracks
            else:
                tracks_responses = [r if r is not None else {"data": {}} for r in tracks_responses]
                tracks_data = {
                    c_id: {
                        track_id: [TrackData.from_dict(x) for x in v]
                        for track_id, v in resp["data"].items()
                    }
                    for c_id, resp in zip(challenges, tracks_responses)
                }
        else:
            tracks_data = {
                c_id: {
                    track_id: [TrackData.from_dict(x) for x in v]
                    for track_id, v in resp["data"].items()
                }
                for c_id, resp in zip(challenges, tracks_responses)
            }

        return {
            "block": block,
            "algorithms": algorithms,
            "binarys": binarys,
            "precommits": precommits,
            "benchmarks": benchmarks,
            "proofs": proofs,
            "frauds": frauds,
            "challenges": challenges,
            "tracks_data": tracks_data,
        }
