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


class Rpc:
    METHODS = {"eth_chainId", "eth_call", "eth_getTransactionReceipt", "eth_getBlockByNumber", "eth_getLogs"}

    def __init__(self, url, timeout=20):
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username or parsed.password or parsed.fragment:
            raise ValueError("invalid RPC URL")
        self.url, self.timeout = url, timeout

    def __call__(self, method, params):
        if method not in self.METHODS:
            raise FundsError("RPC method is not read-only/allowed")
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
        request = Request(self.url, data=body, headers={"Content-Type": "application/json"})
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

    def transfer(self, tx_hash, log_index):
        tx_hash = hex_bytes(tx_hash, 32)
        units(log_index)
        self.verify_network()
        receipt = self.rpc("eth_getTransactionReceipt", [tx_hash])
        if not receipt or quantity(receipt["status"]) != 1:
            raise FundsError("transfer is not successfully mined")
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
        units(amount, positive=True)
        if self.network.custody not in (sender, recipient):
            raise FundsError("transfer does not involve configured custody")
        return ConfirmedTransfer(self.network, tx_hash, log_index, block_number, block_hash,
            datetime.fromtimestamp(quantity(header["timestamp"]), timezone.utc), sender, recipient, amount,
            {"receipt": receipt, "header": header, "latest": latest, "finalized": finalized})
