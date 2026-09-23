"""Read-only ERC-20 receipt verification for explicitly configured custody.

TIG's advertised chain ID is not trusted as configuration. No signing key or
transaction-sending RPC exists in this adapter.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import re
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from .members import address
from .money import FundsError, units


TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def hex_bytes(value, size):
    if not isinstance(value, str) or not re.fullmatch(r"0x[0-9a-fA-F]{" + str(size * 2) + "}", value):
        raise FundsError("malformed chain hash or ABI data")
    return value.lower()


def quantity(value):
    if not isinstance(value, str) or not re.fullmatch(r"0x[0-9a-fA-F]+", value):
        raise FundsError("malformed chain quantity")
    return int(value, 16)


@dataclass(frozen=True)
class Network:
    chain_id: int
    token: str
    custody: str
    confirmations: int
    decimals: int = 18
    require_finalized: bool = True

    def __post_init__(self):
        units(self.chain_id, positive=True)
        units(self.confirmations, positive=True)
        if self.decimals != 18:
            raise FundsError("this TIG ledger requires a verified 18-decimal token")
        if type(self.require_finalized) is not bool:
            raise FundsError("finality policy must be explicit")
        object.__setattr__(self, "token", address(self.token))
        object.__setattr__(self, "custody", address(self.custody))


@dataclass(frozen=True)
class ConfirmedTransfer:
    network: Network
    tx_hash: str
    log_index: int
    block_number: int
    block_hash: str
    block_timestamp: datetime
    sender: str
    recipient: str
    amount: int
    evidence: dict

    @property
    def event_id(self):
        return f"{self.network.chain_id}:{self.network.token}:{self.tx_hash}:{self.log_index}"


@dataclass(frozen=True)
class ConfirmedTransaction:
    network: Network
    tx_hash: str
    sender: str
    recipient: str | None
    nonce: int
    successful: bool
    value: int
    fee: int
    fee_model: str
    block_number: int
    block_hash: str
    block_timestamp: datetime
    evidence: dict


@dataclass(frozen=True)
class CustodyPreflight:
    network: Network
    nonce: int
    token_balance: int
    native_balance: int
    block_number: int
    checked_at: datetime
    evidence: dict


FEE_MODELS = {'ethereum', 'op-isthmus', 'op-jovian'}


def transaction_fee(receipt, model):
    """Use an explicitly configured, verified chain fee rule, never estimates."""
    if model not in FEE_MODELS:
        raise FundsError('unknown transaction fee model')
    gas = quantity(receipt['gasUsed'])
    execution = gas * quantity(receipt['effectiveGasPrice'])
    if model == 'ethereum':
        if any(key in receipt for key in ('l1Fee', 'operatorFeeScalar', 'daFootprintGasScalar')):
            raise FundsError('OP Stack receipt cannot use the Ethereum-only fee rule')
        if quantity(receipt.get('blobGasUsed', '0x0')):
            execution += quantity(receipt['blobGasUsed']) * quantity(receipt['blobGasPrice'])
        return execution
    # L1 fee is mandatory on an OP user transaction, including when it is zero.
    fee = execution + quantity(receipt['l1Fee'])
    scalar, constant = receipt.get('operatorFeeScalar'), receipt.get('operatorFeeConstant')
    if (scalar is None) != (constant is None):
        raise FundsError('incomplete OP operator fee fields')
    if model == 'op-isthmus' and 'daFootprintGasScalar' in receipt:
        raise FundsError('Jovian receipt requires the updated operator fee rule')
    if model == 'op-jovian' and 'daFootprintGasScalar' not in receipt:
        raise FundsError('Jovian receipt marker is missing; verify the historical fee model')
    if scalar is not None:
        scaled = gas * quantity(scalar)
        fee += (scaled // 10**6 if model == 'op-isthmus' else scaled * 100) + quantity(constant)
    # Jovian blobGasUsed describes DA footprint, not an additional blob fee.
    return fee


class Rpc:
    METHODS = {"eth_chainId", "eth_call", "eth_getTransactionReceipt", "eth_getBlockByNumber", "eth_getLogs",
               "eth_getTransactionByHash", "eth_getTransactionCount", "eth_getBalance", "eth_getCode",
               "trace_transaction"}

    def __init__(self, url, timeout=20):
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username or parsed.password or parsed.fragment:
            raise ValueError("invalid RPC URL")
        self.url, self.timeout = url, timeout

    def __call__(self, method, params):
        if method not in self.METHODS:
            raise FundsError("RPC method is not read-only/allowed")
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
        request = Request(self.url, data=body, headers={"Content-Type": "application/json", "User-Agent": "innopool-v2-readonly-probe/0.1"})
        with urlopen(request, timeout=self.timeout) as response:
            raw = response.read(16 * 1024 * 1024 + 1)
        if len(raw) > 16 * 1024 * 1024:
            raise FundsError("RPC response too large")
        result = json.loads(raw)
        if result.get("error") or result.get("id") != 1 or "result" not in result:
            raise FundsError("RPC request failed")
        return result["result"]


class Chain:
    def __init__(self, network, rpc):
        self.network, self.rpc = network, rpc

    def verify_network(self):
        actual = quantity(self.rpc("eth_chainId", []))
        decimals = quantity(self.rpc("eth_call", [{"to": self.network.token, "data": "0x313ce567"}, "latest"]))
        if actual != self.network.chain_id or decimals != self.network.decimals:
            raise FundsError("RPC chain ID/token decimals differ from configured network")

    def _confirmed_receipt(self, tx_hash):
        tx_hash = hex_bytes(tx_hash, 32)
        self.verify_network()
        receipt = self.rpc("eth_getTransactionReceipt", [tx_hash])
        if not receipt or quantity(receipt["status"]) not in (0, 1):
            raise FundsError("transaction has no definitive mined outcome")
        block_number = quantity(receipt["blockNumber"])
        block_hash = hex_bytes(receipt["blockHash"], 32)
        if hex_bytes(receipt["transactionHash"], 32) != tx_hash:
            raise FundsError("receipt does not match requested transaction")
        latest = self.rpc("eth_getBlockByNumber", ["latest", False])
        if quantity(latest["number"]) - block_number + 1 < self.network.confirmations:
            raise FundsError("transfer has insufficient confirmations")
        finalized = self.rpc("eth_getBlockByNumber", ["finalized", False]) if self.network.require_finalized else None
        if self.network.require_finalized and (not finalized or quantity(finalized["number"]) < block_number):
            raise FundsError("transfer is not finalized")
        header = self.rpc("eth_getBlockByNumber", [hex(block_number), False])
        if (not header or hex_bytes(header["hash"], 32) != block_hash
                or quantity(header["number"]) != block_number):
            raise FundsError("receipt block is not canonical")
        return {"receipt": receipt, "header": header, "latest": latest, "finalized": finalized}

    def transfer(self, tx_hash, log_index):
        tx_hash = hex_bytes(tx_hash, 32)
        units(log_index)
        evidence = self._confirmed_receipt(tx_hash)
        receipt, header = evidence['receipt'], evidence['header']
        if quantity(receipt['status']) != 1:
            raise FundsError('transfer is not successfully mined')
        block_number = quantity(receipt['blockNumber'])
        block_hash = hex_bytes(receipt['blockHash'], 32)
        matches = [log for log in receipt["logs"] if quantity(log["logIndex"]) == log_index]
        if len(matches) != 1:
            raise FundsError("transfer event is missing or ambiguous")
        log = matches[0]
        if (log.get("removed") or address(log["address"]) != self.network.token
                or hex_bytes(log["transactionHash"], 32) != tx_hash
                or hex_bytes(log["blockHash"], 32) != block_hash
                or quantity(log["blockNumber"]) != block_number):
            raise FundsError("event token or canonical identity does not match")
        topics = log["topics"]
        if len(topics) != 3 or hex_bytes(topics[0], 32) != TRANSFER_TOPIC:
            raise FundsError("event is not an ERC-20 Transfer")
        addresses = [hex_bytes(topic, 32) for topic in topics[1:]]
        if any(value[2:26] != "0" * 24 for value in addresses):
            raise FundsError("malformed indexed transfer address")
        sender, recipient = ["0x" + value[-40:] for value in addresses]
        amount = int(hex_bytes(log["data"], 32), 16)
        units(amount)
        if self.network.custody not in (sender, recipient):
            raise FundsError("transfer does not involve configured custody")
        return ConfirmedTransfer(self.network, tx_hash, log_index, block_number, block_hash,
            datetime.fromtimestamp(quantity(header["timestamp"]), timezone.utc), sender, recipient, amount,
            evidence)

    def transaction(self, tx_hash, *, fee_model):
        tx_hash = hex_bytes(tx_hash, 32)
        evidence = self._confirmed_receipt(tx_hash)
        receipt, header = evidence['receipt'], evidence['header']
        transaction = self.rpc('eth_getTransactionByHash', [tx_hash])
        if not transaction or hex_bytes(transaction['hash'], 32) != tx_hash:
            raise FundsError('transaction identity is missing or mismatched')
        if (quantity(transaction['chainId']) != self.network.chain_id
                or hex_bytes(transaction['blockHash'], 32) != hex_bytes(receipt['blockHash'], 32)
                or quantity(transaction['blockNumber']) != quantity(receipt['blockNumber'])):
            raise FundsError('transaction network or canonical inclusion differs from its receipt')
        if quantity(transaction['type']) not in (0, 1, 2):
            raise FundsError('manual custody payments currently require a standard direct EOA transaction')
        sender = address(transaction['from'])
        recipient = address(transaction['to']) if transaction.get('to') else None
        for key, expected in (('from', sender), ('to', recipient)):
            if key in receipt and (address(receipt[key]) if receipt[key] else None) != expected:
                raise FundsError('transaction sender or recipient differs from its receipt')
        try:
            fee = transaction_fee(receipt, fee_model)
        except (KeyError, TypeError) as failure:
            raise FundsError('actual transaction fee evidence is incomplete') from failure
        return ConfirmedTransaction(self.network, tx_hash, sender, recipient,
            quantity(transaction['nonce']), quantity(receipt['status']) == 1, quantity(transaction['value']),
            fee, fee_model, quantity(receipt['blockNumber']), hex_bytes(receipt['blockHash'], 32),
            datetime.fromtimestamp(quantity(header['timestamp']), timezone.utc), {**evidence, 'transaction': transaction})

    def preflight(self):
        """Read a final custody balance and a pending nonce; never sign or send."""
        self.verify_network()
        if not self.network.require_finalized:
            raise FundsError('manual withdrawals require finalized chain evidence')
        header = self.rpc('eth_getBlockByNumber', ['finalized', False])
        latest = self.rpc('eth_getBlockByNumber', ['latest', False])
        number = quantity(header['number'])
        if quantity(latest['number']) - number + 1 < self.network.confirmations:
            raise FundsError('finalized balance anchor lacks required confirmations')
        tag = hex(number)
        code = self.rpc('eth_getCode', [self.network.custody, tag])
        if code != '0x':
            raise FundsError('manual custody payments currently require an undelegated EOA wallet')
        token = self.rpc('eth_call', [{'to': self.network.token,
            'data': '0x70a08231'+'0'*24+self.network.custody[2:]}, tag])
        native = self.rpc('eth_getBalance', [self.network.custody, tag])
        nonce = self.rpc('eth_getTransactionCount', [self.network.custody, 'pending'])
        repeated = self.rpc('eth_getBlockByNumber', [tag, False])
        if not repeated or repeated['hash'] != header['hash'] or quantity(repeated['number']) != number:
            raise FundsError('custody balance anchor changed during observation')
        return CustodyPreflight(self.network, quantity(nonce), int(hex_bytes(token, 32), 16),
            quantity(native), number, datetime.now(timezone.utc),
            {'header': header, 'latest': latest, 'token_balance': token, 'native_balance': native, 'pending_nonce': nonce, 'code': code})

    def find_nonce(self, nonce, *, after_height):
        """Find the finalized transaction that consumed a frozen EOA nonce.

        Binary search avoids scanning every intervening Base block. The caller
        must still verify the returned transaction and exact token event.
        """
        units(nonce); units(after_height)
        self.verify_network()
        final = self.rpc('eth_getBlockByNumber', ['finalized', False])
        end = quantity(final['number'])
        if end <= after_height: return None
        count = lambda height: quantity(self.rpc('eth_getTransactionCount', [self.network.custody, hex(height)]))
        if count(end) <= nonce: return None
        if count(after_height) > nonce:
            raise FundsError('reserved nonce was already consumed before the send anchor')
        first, last = after_height+1, end
        while first < last:
            middle = (first+last)//2
            if count(middle) > nonce: last = middle
            else: first = middle+1
        block = self.rpc('eth_getBlockByNumber', [hex(first), True])
        if not block or quantity(block['number']) != first:
            raise FundsError('nonce recovery block is unavailable')
        matches = [tx for tx in block['transactions'] if tx['from'].lower()==self.network.custody and quantity(tx['nonce'])==nonce]
        if len(matches)!=1: raise FundsError('finalized nonce has no unique matching transaction')
        return hex_bytes(matches[0]['hash'],32)
