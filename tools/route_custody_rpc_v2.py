#!/usr/bin/env python3
"""Private read-only JSON-RPC routing for custody observers and verification.

Basic reads, including latest/finalized headers, use the configured authority.
Log queries can use a separately checked endpoint. Caller validation still
checks receipts, balances, canonical hashes and finalized range boundaries.
"""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

METHODS = frozenset(('eth_chainId', 'eth_call', 'eth_getBalance', 'eth_getTransactionCount',
    'eth_getCode', 'eth_getBlockByNumber', 'eth_getBlockByHash', 'eth_getTransactionByHash',
    'eth_getTransactionReceipt', 'eth_getLogs', 'eth_estimateGas', 'eth_gasPrice',
    'eth_maxPriorityFeePerGas', 'eth_feeHistory', 'eth_blockNumber'))
MAX_REQUEST = 64 * 1024
MAX_RESPONSE = 8 * 1024 * 1024

def endpoints(config):
    if not isinstance(config, dict) or set(config) != {'basic_reads', 'log_queries'}:
        raise ValueError('router requires exactly two explicit upstream roles')
    for value in config.values():
        parsed = urlsplit(value)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise ValueError('upstreams require credential-free HTTPS authorities')
    return dict(config)

def route(config, method):
    if method not in METHODS:
        raise ValueError('RPC method is not a supported read')
    return config['log_queries' if method == 'eth_getLogs' else 'basic_reads']

def request(value):
    if (not isinstance(value, dict) or set(value) - {'jsonrpc', 'id', 'method', 'params'}
            or value.get('jsonrpc') != '2.0' or 'id' not in value
            or type(value['id']) not in (int, str, type(None))
            or not isinstance(value.get('method'), str) or value['method'] not in METHODS
            or not isinstance(value.get('params'), list) or len(value['params']) > 32):
        raise ValueError('invalid or unsupported read-only RPC request')
    return value

def forward(config, value):
    value = request(value)
    upstream = Request(route(config, value['method']), data=json.dumps(value).encode(),
        headers={'Content-Type': 'application/json', 'Accept': 'application/json',
                 'User-Agent': 'innopool-v2-readonly-rpc/1'})
    with urlopen(upstream, timeout=20) as response:
        data = response.read(MAX_RESPONSE + 1)
    if len(data) > MAX_RESPONSE:
        raise ValueError('upstream response exceeds the bounded read size')
    result = json.loads(data)
    if (not isinstance(result, dict) or result.get('jsonrpc') != '2.0'
            or result.get('id') != value['id'] or ('result' in result) == ('error' in result)):
        raise ValueError('upstream returned an inconsistent RPC envelope')
    return result

class Server(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, address, config):
        self.config = endpoints(config)
        self.capacity = threading.BoundedSemaphore(2)
        super().__init__(address, Handler)

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass  # Never log request bodies, query strings or endpoint credentials.

    def send(self, status, value):
        data = json.dumps(value, separators=(',', ':')).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if self.path != '/':
            return self.send(404, {'error': 'unknown private RPC path'})
        identity = None
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= MAX_REQUEST:
                raise ValueError('invalid request size')
            value = request(json.loads(self.rfile.read(size)))
            identity = value['id']
        except (ValueError, TypeError, json.JSONDecodeError):
            return self.send(400, {'jsonrpc': '2.0', 'id': identity,
                'error': {'code': -32600, 'message': 'invalid read-only RPC request'}})
        if not self.server.capacity.acquire(blocking=False):
            return self.send(503, {'jsonrpc': '2.0', 'id': identity,
                'error': {'code': -32005, 'message': 'private read capacity is occupied'}})
        try:
            self.send(200, forward(self.server.config, value))
        except Exception as failure:
            # Keep provider URLs, raw response bodies and headers private.
            self.send(503, {'jsonrpc': '2.0', 'id': identity,
                'error': {'code': -32005, 'message': 'upstream read unavailable'}})
        finally:
            self.server.capacity.release()

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--port', type=int, default=18545)
    args = parser.parse_args(argv)
    if not 1024 <= args.port <= 65535:
        parser.error('use a nonprivileged private port')
    server = Server(('127.0.0.1', args.port), json.loads(Path(args.config).read_text()))
    try:
        server.serve_forever()
    finally:
        server.server_close()

if __name__ == '__main__':
    main()
