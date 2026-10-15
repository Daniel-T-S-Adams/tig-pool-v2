import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('custody_rpc_router',
    Path(__file__).resolve().parents[2] / 'tools/route_custody_rpc_v2.py')
router = importlib.util.module_from_spec(spec)
spec.loader.exec_module(router)

class RpcRouterTests(unittest.TestCase):
    def setUp(self):
        self.config = {'basic_reads': 'https://mainnet.base.org',
                       'log_queries': 'https://base.api.pocket.network'}

    def test_finalized_headers_and_balances_keep_the_basic_read_authority(self):
        for method in ('eth_getBlockByNumber', 'eth_getBalance', 'eth_getCode', 'eth_call',
                       'eth_getTransactionReceipt', 'eth_getTransactionCount'):
            self.assertEqual(router.route(self.config, method), self.config['basic_reads'])
        self.assertEqual(router.route(self.config, 'eth_getLogs'), self.config['log_queries'])

    def test_writing_signing_and_admin_methods_are_refused(self):
        for method in ('eth_sendRawTransaction', 'eth_sendTransaction', 'personal_sign',
                       'eth_sign', 'admin_nodeInfo', 'debug_traceTransaction'):
            with self.assertRaises(ValueError):
                router.request({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': []})
            with self.assertRaises(ValueError):
                router.route(self.config, method)

    def test_batch_invalid_envelopes_and_credentials_are_refused(self):
        for value in ([], {}, {'jsonrpc': '2.0', 'id': True, 'method': 'eth_chainId', 'params': []},
                      {'jsonrpc': '2.0', 'id': 1, 'method': 'eth_chainId', 'params': {}}):
            with self.assertRaises(ValueError):
                router.request(value)
        for url in ('http://example.org', 'https://user:secret@example.org', 'https://example.org/#fragment'):
            with self.assertRaises(ValueError):
                router.endpoints({**self.config, 'log_queries': url})

    def test_forward_preserves_request_and_response_without_authentication_headers(self):
        value = {'jsonrpc': '2.0', 'id': 7, 'method': 'eth_getLogs', 'params': [{'fromBlock': '0x1'}]}
        result = {'jsonrpc': '2.0', 'id': 7, 'result': []}
        with patch.object(router, 'urlopen', return_value=io.BytesIO(json.dumps(result).encode())) as open:
            self.assertEqual(router.forward(self.config, value), result)
            request = open.call_args.args[0]
            self.assertEqual(request.full_url, self.config['log_queries'])
            self.assertEqual(json.loads(request.data), value)
            self.assertNotIn('Authorization', request.headers)
            self.assertNotIn('Cookie', request.headers)

    def test_upstream_errors_are_preserved_and_inconsistent_envelopes_rejected(self):
        value = {'jsonrpc': '2.0', 'id': 7, 'method': 'eth_chainId', 'params': []}
        error = {'jsonrpc': '2.0', 'id': 7, 'error': {'code': -32005, 'message': 'busy'}}
        with patch.object(router, 'urlopen', return_value=io.BytesIO(json.dumps(error).encode())):
            self.assertEqual(router.forward(self.config, value), error)
        for result in ({'jsonrpc': '2.0', 'id': 8, 'result': '0x2105'},
                       {'jsonrpc': '2.0', 'id': 7, 'result': '0x2105', 'error': {}}, {}):
            with patch.object(router, 'urlopen', return_value=io.BytesIO(json.dumps(result).encode())):
                with self.assertRaises(ValueError):
                    router.forward(self.config, value)

    def test_response_size_is_bounded(self):
        with patch.object(router, 'MAX_RESPONSE', 8), patch.object(router, 'urlopen', return_value=io.BytesIO(b'x' * 9)):
            with self.assertRaises(ValueError):
                router.forward(self.config, {'jsonrpc': '2.0', 'id': 1, 'method': 'eth_chainId', 'params': []})

if __name__ == '__main__':
    unittest.main()
