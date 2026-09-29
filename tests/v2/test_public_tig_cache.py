"""Mutable head reads must not be falsely bracketed by an edge cache."""

from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import unittest
from urllib.parse import parse_qs, urlsplit

from pool_manager.pool_v2.observation import PublicTigClient, capture_snapshot
from pool_manager.pool_v2.protocol import ProtocolDataError, validate_snapshot
from observer_helpers import observation


class PublicCacheTests(unittest.TestCase):
    def setUp(self):
        self.head = 8
        self.cache = {}
        self.requests = []
        self.advance_on_algorithms = False
        test = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                test.requests.append(self.path)
                path = urlsplit(self.path).path
                query = parse_qs(urlsplit(self.path).query)
                cached = self.path in test.cache
                if cached:
                    payload = test.cache[self.path]
                elif path == "/get-block":
                    payload = observation(test.head)["start"]
                else:
                    data = observation(int(query["block_id"][0].split("-")[-1]))
                    if path == "/get-algorithms" and test.advance_on_algorithms:
                        test.head += 1
                    if path == "/get-benchmarks":
                        payload = data["players"][query["player_id"][0]]
                    else:
                        payload = data[{"/get-algorithms": "algorithms", "/get-challenges": "challenges",
                                        "/get-opow": "opow"}[path]]
                test.cache[self.path] = deepcopy(payload)
                body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "max-age=15")
                self.send_header("CF-Cache-Status", "HIT" if cached else "MISS")
                self.send_header("Set-Cookie", "must-not-be-archived=secret")
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.client = PublicTigClient(f"http://127.0.0.1:{self.server.server_port}")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_repeated_head_reads_see_new_block_despite_ignored_no_cache_header(self):
        params = {"include_data": "true"}
        self.assertEqual(self.client.get("/get-block", params)["block"]["details"]["height"], 8)
        self.head = 9
        self.assertEqual(self.client.get("/get-block", params)["block"]["details"]["height"], 9)
        self.assertEqual(params, {"include_data": "true"})
        self.assertNotEqual(self.requests[0], self.requests[1])
        for record in self.client.records:
            self.assertEqual(record["response_headers"]["cf-cache-status"], "MISS")
            self.assertNotIn("set-cookie", record["response_headers"])
            self.assertGreaterEqual(record["duration_seconds"], 0)

    def test_head_moving_during_capture_is_detected_through_cached_endpoint(self):
        self.advance_on_algorithms = True
        data = capture_snapshot(self.client)
        with self.assertRaisesRegex(ProtocolDataError, "block changed"):
            validate_snapshot(data)

    def test_immutable_block_feeds_keep_their_cache_key(self):
        for _ in range(2):
            self.client.get("/get-algorithms", {"block_id": "block-8"})
        self.assertEqual(self.requests[0], self.requests[1])
        self.assertEqual(self.client.records[-1]["response_headers"]["cf-cache-status"], "HIT")

    def test_historical_head_request_is_rejected_before_network_access(self):
        for params in ({"block_id": "old-block"}, {"height": 7}):
            with self.assertRaisesRegex(ValueError, "historical"):
                self.client.get("/get-block", params)
        self.assertEqual(self.requests, [])
