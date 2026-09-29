"""Recover an already sent, sponsored initial EIP-7702 withdrawal.

This is deliberately narrower than general smart-account custody. A verified
authorization must consume the frozen EOA nonce in the payment transaction.
We require one custody token transfer, no other custody calls, and a complete
zero-value call tree. Relayer gas is retained as evidence, never charged to
custody. New smart-account sends remain disabled by the ordinary preflight.
"""

from dataclasses import asdict, dataclass
from copy import deepcopy

from eth_keys import keys
from eth_keys.constants import SECPK1_N
from eth_keys.exceptions import BadSignature
from eth_utils import keccak
import rlp

from .chain import Chain, Network, ConfirmedTransaction, ConfirmedTransfer, TRANSFER_TOPIC, hex_bytes, quantity
from .members import address
from .money import Conflict, FundsError, units
from .native_funding import path


def delegation(code):
    if code == '0x':
        return None
    value = hex_bytes(code, 23)
    if not value.startswith('0xef0100'):
        raise FundsError('unsupported custody contract code')
    return address('0x' + value[8:])


def authority(authorization):
    """EIP-7702 signs keccak(0x05 || rlp([chain_id, delegate, nonce]))."""
    try:
        chain_id = quantity(authorization['chainId'])
        delegate = hex_bytes(authorization['address'], 20)
        nonce = quantity(authorization['nonce'])
        parity, r, s = (quantity(authorization[k]) for k in ('yParity', 'r', 's'))
        if (chain_id >= 2**256 or nonce >= 2**64 - 1 or parity not in (0, 1)
                or not 0 < r < SECPK1_N or not 0 < s <= SECPK1_N // 2):
            raise ValueError('invalid authorization signature or scope')
        digest = keccak(b'\x05' + rlp.encode([chain_id, bytes.fromhex(delegate[2:]), nonce]))
        signer = keys.Signature(vrs=(parity, r, s)).recover_public_key_from_msg_hash(digest)
        return signer.to_checksum_address().lower(), chain_id, delegate, nonce
    except (KeyError, TypeError, ValueError, BadSignature) as failure:
        raise FundsError('invalid EIP-7702 authorization') from failure


@dataclass(frozen=True)
class SponsoredWithdrawal:
    transaction: ConfirmedTransaction
    transfer: ConfirmedTransfer
    custody_nonce: int
    delegate: str
    evidence: dict


def _zero_value_trace(transaction, trace):
    if not isinstance(trace, list) or not 1 <= len(trace) <= 10000:
        raise FundsError('sponsored payment requires a complete bounded trace')
    frames = {}
    for frame in trace:
        try:
            location = path(frame['traceAddress'], root=True)
            if (location in frames or frame.get('error') or not isinstance(frame.get('result'), dict)
                    or frame['type'] != 'call' or frame['action']['callType'] not in ('call', 'staticcall')
                    or quantity(frame['action']['value']) != 0
                    or type(frame['blockNumber']) is not int or frame['blockNumber'] != transaction.block_number
                    or hex_bytes(frame['blockHash'], 32) != transaction.block_hash
                    or hex_bytes(frame['transactionHash'], 32) != transaction.tx_hash
                    or type(frame['transactionPosition']) is not int
                    or frame['transactionPosition'] != quantity(transaction.evidence['receipt']['transactionIndex'])
                    or type(frame['subtraces']) is not int or not 0 <= frame['subtraces'] <= 10000):
                raise FundsError('sponsored trace has unsupported effects or inconsistent identity')
            address(frame['action']['from']); address(frame['action']['to'])
            frames[location] = frame
        except (KeyError, TypeError) as failure:
            raise FundsError('sponsored trace evidence is incomplete') from failure
    children = {location: set() for location in frames}
    for location in frames:
        if location:
            if location[:-1] not in frames:
                raise FundsError('sponsored trace is missing an ancestor')
            children[location[:-1]].add(location[-1])
    if (() not in frames or sum(f['subtraces'] for f in frames.values()) != len(frames) - 1
            or any(children[p] != set(range(f['subtraces'])) for p, f in frames.items())):
        raise FundsError('sponsored trace tree is incomplete')
    root = frames[()]['action']
    if (root['callType'] != 'call' or address(root['from']) != transaction.sender
            or address(root['to']) != transaction.recipient
            or root['input'] != transaction.evidence['transaction']['input']):
        raise FundsError('sponsored trace root differs from the transaction')
    return frames


def verify(chain, trace_rpc, tx_hash, log_index, *, nonce, fee_model):
    units(nonce)
    if trace_rpc is None or not chain.network.require_finalized:
        raise FundsError('sponsored recovery requires finalized chain and trace evidence')
    tx = chain.authorization_transaction(tx_hash, fee_model=fee_model)
    transfer = chain.transfer(tx_hash, log_index)
    network = chain.network
    if (not tx.successful or tx.sender == network.custody or tx.value != 0
            or transfer.sender != network.custody or transfer.recipient == network.custody
            or transfer.amount <= 0):
        raise FundsError('expected a successful externally sponsored custody withdrawal')
    authorizations = tx.evidence['transaction'].get('authorizationList')
    if not isinstance(authorizations, list) or len(authorizations) != 1:
        raise FundsError('recovery requires one unambiguous custody authorization')
    signer, chain_id, delegate, signed_nonce = authority(authorizations[0])
    if (signer, chain_id, signed_nonce) != (network.custody, network.chain_id, nonce):
        raise Conflict('authorization does not consume the prepared custody nonce on this chain')
    before_tag, tag = hex(tx.block_number - 1), hex(tx.block_number)
    before_header = chain.rpc('eth_getBlockByNumber', [before_tag, False])
    block = chain.rpc('eth_getBlockByNumber', [tag, True])
    if (quantity(before_header['number']) != tx.block_number - 1
            or quantity(block['number']) != tx.block_number
            or hex_bytes(block['hash'], 32) != tx.block_hash
            or hex_bytes(block['parentHash'], 32) != hex_bytes(before_header['hash'], 32)):
        raise Conflict('authorization block ancestry differs from the receipt')
    transactions = block.get('transactions')
    if not isinstance(transactions, list) or not 1 <= len(transactions) <= 100000:
        raise FundsError('complete authorization block is required')
    relevant = []
    for item in transactions:
        if address(item['from']) == network.custody:
            raise FundsError('another custody transaction makes nonce attribution ambiguous')
        for auth in item.get('authorizationList', []):
            if authority(auth)[0] == network.custody:
                relevant.append(item)
    identity_fields = ('hash', 'from', 'to', 'nonce', 'chainId', 'type', 'blockNumber',
                       'blockHash', 'transactionIndex', 'input', 'value', 'authorizationList')
    if len(relevant) != 1 or any(relevant[0].get(k) != tx.evidence['transaction'].get(k) for k in identity_fields):
        raise Conflict('custody authorization is not unique in the canonical block')
    snapshots = []
    for anchor in (before_tag, tag):
        snapshots.append({
            'code': chain.rpc('eth_getCode', [network.custody, anchor]),
            'nonce': quantity(chain.rpc('eth_getTransactionCount', [network.custody, anchor])),
            'native': quantity(chain.rpc('eth_getBalance', [network.custody, anchor])),
            'tig': int(hex_bytes(chain.rpc('eth_call', [{'to': network.token,
                'data': '0x70a08231' + '0'*24 + network.custody[2:]}, anchor]), 32), 16),
        })
    before, after = snapshots
    if (before['code'] != '0x' or delegation(after['code']) != delegate
            or (before['nonce'], after['nonce']) != (nonce, nonce + 1)
            or before['native'] != after['native'] or before['tig'] - after['tig'] != transfer.amount):
        raise Conflict('custody code, nonce or balances do not prove this initial sponsored payment')
    outgoing = [log for log in tx.evidence['receipt']['logs']
        if log['address'].lower() == network.token and len(log.get('topics', [])) == 3
        and log['topics'][0].lower() == TRANSFER_TOPIC
        and log['topics'][1].lower() == '0x'+'0'*24+network.custody[2:]]
    if len(outgoing) != 1 or quantity(outgoing[0]['logIndex']) != transfer.log_index:
        raise FundsError('sponsored recovery requires exactly one custody token transfer')
    if quantity(trace_rpc('eth_chainId', [])) != network.chain_id:
        raise Conflict('trace provider chain differs from custody')
    trace_anchor = trace_rpc('eth_getBlockByNumber', [tag, False])
    if quantity(trace_anchor['number']) != tx.block_number or hex_bytes(trace_anchor['hash'], 32) != tx.block_hash:
        raise Conflict('trace provider block differs from the receipt')
    trace = trace_rpc('trace_transaction', [tx.tx_hash])
    frames = _zero_value_trace(tx, trace)
    effects = [f['action'] for f in frames.values() if address(f['action']['from']) == network.custody]
    calldata = '0xa9059cbb' + '0'*24 + transfer.recipient[2:] + f'{transfer.amount:064x}'
    if (len(effects) != 1 or effects[0]['callType'] != 'call'
            or address(effects[0]['to']) != network.token or effects[0]['input'].lower() != calldata):
        raise FundsError('custody performed effects beyond the exact requested token transfer')
    for rpc in (chain.rpc, trace_rpc):
        repeated = rpc('eth_getBlockByNumber', [tag, False])
        if quantity(repeated['number']) != tx.block_number or hex_bytes(repeated['hash'], 32) != tx.block_hash:
            raise Conflict('sponsored payment anchor changed during verification')
    return SponsoredWithdrawal(tx, transfer, nonce, delegate, {
        'version': 1, 'before_header': before_header, 'block': block,
        'before': before, 'after': after, 'trace': trace, 'trace_anchor': trace_anchor,
        'custody_fee': 0, 'relayer_fee': tx.fee,
    })


def capture(chain, trace_rpc, tx_hash, log_index, *, nonce, fee_model):
    """Retain read-only evidence, including failed captures, before accounting."""
    calls = []
    def recorded(source, rpc):
        def invoke(method, params):
            value = rpc(method, params)
            calls.append({'source': source, 'method': method, 'params': deepcopy(params), 'result': deepcopy(value)})
            return value
        return invoke
    data = {'version': 1, 'network': asdict(chain.network), 'tx_hash': tx_hash,
            'log_index': log_index, 'nonce': nonce, 'fee_model': fee_model, 'calls': calls, 'error': None}
    try:
        verify(Chain(chain.network, recorded('chain', chain.rpc)),
               recorded('trace', trace_rpc) if trace_rpc else None,
               tx_hash, log_index, nonce=nonce, fee_model=fee_model)
    except Exception as failure:
        data['error'] = type(failure).__name__
        data['message'] = str(failure) if isinstance(failure, FundsError) else 'capture failed'
    return data


def verify_capture(data):
    if data.get('version') != 1 or data.get('error'):
        raise FundsError('sponsored recovery capture is incomplete or unsupported')
    entries = iter(data['calls'])
    def source(name):
        def replay(method, params):
            entry = next(entries, None)
            if entry is None or (entry['source'], entry['method'], entry['params']) != (name, method, params):
                raise FundsError('sponsored recovery archive has missing or reordered evidence')
            return deepcopy(entry['result'])
        return replay
    recovery = verify(Chain(Network(**data['network']), source('chain')), source('trace'),
        data['tx_hash'], data['log_index'], nonce=data['nonce'], fee_model=data['fee_model'])
    if next(entries, None) is not None:
        raise FundsError('sponsored recovery archive has unconsumed evidence')
    return recovery
