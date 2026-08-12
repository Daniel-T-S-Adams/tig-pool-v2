#!/usr/bin/env python3
"""Cached TIG maps must ride along with a new block header (no wait on tracks)."""

from __future__ import annotations

import pathlib
import sys


def main() -> int:
    src = (
        pathlib.Path(__file__).resolve().parents[1]
        / "master"
        / "data_fetcher.py"
    ).read_text(encoding="utf-8")
    start = src.index("def light_payload_from_cache")
    end = src.index("\ndef _get(")
    ns: dict = {}
    exec(src[start:end], ns, ns)
    fn = ns["light_payload_from_cache"]

    prev = {
        "algorithms": {"c001_a098": "algo"},
        "tracks_data": {"c001": {"t": []}},
        "precommits": {"old": 1},
        "block": "stale",
    }
    payload = fn("new-block", prev, 7)
    failed = 0
    cases = [
        (payload["block"] == "new-block", "uses the new block header"),
        (payload["algorithms"] == {"c001_a098": "algo"}, "keeps cached algorithms"),
        (payload["tracks_data"] == {"c001": {"t": []}}, "keeps cached tracks"),
        (payload["complete"] is False, "marked incomplete until background fetch"),
        (payload["generation"] == 7, "generation stamped"),
    ]
    for ok, label in cases:
        print(f"{'pass' if ok else 'FAIL'}: {label}")
        failed += 0 if ok else 1

    empty = fn("b2", None, 1)
    ok = empty["algorithms"] == {} and empty["complete"] is False
    print(f"{'pass' if ok else 'FAIL'}: empty cache still returns a light payload")
    failed += 0 if ok else 1
    return 2 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
