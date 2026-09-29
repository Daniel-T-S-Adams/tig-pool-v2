"""Finalized internal CALL receipts for operator network fees, with retained traces."""

from dataclasses import dataclass

from psycopg2.extras import Json

from . import custody, ledger
from .chain import ConfirmedTransaction, hex_bytes, quantity
from .members import address
from .money import Conflict, FundsError, units


def path(value, *, root=False):
    if (not isinstance(value, (list, tuple)) or not (0 if root else 1) <= len(value) <= 64
            or any(type(index) is not int or not 0 <= index < 2**31 for index in value)):
        raise FundsError('internal transfer path must contain nonnegative integer call indexes')
    return tuple(value)


@dataclass(frozen=True)
class InternalNativeReceipt:
    transaction: ConfirmedTransaction
    trace_address: tuple
    sender: str
    recipient: str
    amount: int
    evidence: dict


def verify(chain, trace_rpc, tx_hash, trace_address, *, fee_model):
    """Require the configured RPCs to agree on chain, transaction and final block.

    Parity-format trace_transaction is an explicit read-only RPC capability.
    A value on DELEGATECALL/CALLCODE is not a transfer. A child of a reverted
    frame is not a transfer even when that child's own frame reports success.
    """
    selected = path(trace_address)
    if trace_rpc is None:
        raise FundsError('internal native funding requires a configured trace RPC')
    if not chain.network.require_finalized:
        raise FundsError('internal native funding requires finalized chain evidence')
    transaction = chain.transaction(tx_hash, fee_model=fee_model)
    if not transaction.successful or transaction.sender == chain.network.custody:
        raise FundsError('internal funding requires a successful transaction from another wallet')
    if quantity(trace_rpc('eth_chainId', [])) != chain.network.chain_id:
        raise FundsError('trace RPC chain differs from the configured custody network')
    tag = hex(transaction.block_number)
    anchor = trace_rpc('eth_getBlockByNumber', [tag, False])
    if (quantity(anchor['number']) != transaction.block_number
            or hex_bytes(anchor['hash'], 32) != transaction.block_hash):
        raise FundsError('trace RPC canonical block differs from the confirmed receipt')
    trace = trace_rpc('trace_transaction', [transaction.tx_hash])
    if not isinstance(trace, list) or not 1 <= len(trace) <= 10000:
        raise FundsError('transaction trace is missing or oversized')
    frames = {}
    receipt = transaction.evidence['receipt']
    for frame in trace:
        try:
            location = path(frame['traceAddress'], root=True)
            if (location in frames or type(frame['blockNumber']) is not int
                    or frame['blockNumber'] != transaction.block_number
                    or hex_bytes(frame['blockHash'], 32) != transaction.block_hash
                    or hex_bytes(frame['transactionHash'], 32) != transaction.tx_hash
                    or type(frame['transactionPosition']) is not int
                    or frame['transactionPosition'] != quantity(receipt['transactionIndex'])
                    or type(frame['subtraces']) is not int or not 0 <= frame['subtraces'] <= 10000):
                raise FundsError('trace contains duplicate, malformed or mismatched call identities')
            frames[location] = frame
        except (KeyError, TypeError) as failure:
            raise FundsError('transaction trace evidence is incomplete') from failure
    # Check tree completeness so a missing failed parent cannot be ignored.
    children = {location: set() for location in frames}
    for location in frames:
        if location:
            if location[:-1] not in frames:
                raise FundsError('transaction trace is missing an ancestor')
            children[location[:-1]].add(location[-1])
    if (() not in frames or selected not in frames
            or sum(frame['subtraces'] for frame in frames.values()) != len(frames) - 1 or any(
            children[location] != set(range(frame['subtraces'])) for location, frame in frames.items())):
        raise FundsError('transaction trace is incomplete or the requested transfer is absent')
    try:
        root = frames[()]
        root_action = root['action']
        if (root['type'] != 'call' or root_action['callType'] != 'call'
                or address(root_action['from']) != transaction.sender
                or address(root_action['to']) != transaction.recipient
                or quantity(root_action['value']) != transaction.value
                or root_action['input'] != transaction.evidence['transaction']['input']):
            raise FundsError('root trace differs from the confirmed transaction')
        for depth in range(len(selected) + 1):
            ancestor = frames[selected[:depth]]
            if ancestor.get('error') or not isinstance(ancestor.get('result'), dict):
                raise FundsError('internal transfer or its ancestor failed or reverted')
            if (ancestor.get('type') != 'call'
                    or ancestor['action']['callType'] not in ('call', 'delegatecall', 'callcode')):
                raise FundsError('internal funding requires a successful CALL ancestry')
        frame = frames[selected]
        action = frame['action']
        sender, recipient = address(action['from']), address(action['to'])
        amount = quantity(action['value'])
        if (frame['type'] != 'call' or action['callType'] != 'call' or amount <= 0
                or sender == chain.network.custody or recipient != chain.network.custody):
            raise FundsError('selected call is not a positive incoming native transfer')
    except (KeyError, TypeError) as failure:
        raise FundsError('transaction trace evidence is incomplete') from failure
    code = chain.rpc('eth_getCode', [chain.network.custody, tag])
    if code != '0x':
        raise FundsError('internal native funding currently requires undelegated EOA custody')
    repeated = chain.rpc('eth_getBlockByNumber', [tag, False])
    trace_repeated = trace_rpc('eth_getBlockByNumber', [tag, False])
    if any(hex_bytes(value['hash'], 32) != transaction.block_hash
           or quantity(value['number']) != transaction.block_number for value in (repeated, trace_repeated)):
        raise Conflict('internal funding block changed during verification')
    return InternalNativeReceipt(transaction, selected, sender, recipient, amount,
        {'version': 1, 'trace': trace, 'trace_anchor': anchor, 'custody_code': code})


def receive(database, receipt, *, actor):
    if not isinstance(receipt, InternalNativeReceipt) or not actor:
        raise FundsError('verified internal native receipt and operator identity required')
    transaction = receipt.transaction
    location = path(receipt.trace_address)
    units(receipt.amount, positive=True)
    if (not transaction.successful or receipt.recipient != transaction.network.custody
            or receipt.sender == transaction.network.custody or transaction.sender == transaction.network.custody):
        raise FundsError('internal native funding must come from outside pool custody')
    with database.transaction() as cursor:
        custody.bind(cursor, transaction.network)
        custody.save_transaction(cursor, transaction)
        cursor.execute('''SELECT sender,recipient,amount FROM native_internal_receipts
            WHERE chain_id=%s AND tx_hash=%s AND trace_address=%s''',
            (transaction.network.chain_id, transaction.tx_hash, list(location)))
        previous = cursor.fetchone()
        if previous:
            if (previous['sender'], previous['recipient'], int(previous['amount'])) != (
                    receipt.sender, receipt.recipient, receipt.amount):
                raise Conflict('recorded internal native transfer facts changed')
            return
        cursor.execute('''INSERT INTO native_internal_receipts
            (chain_id,tx_hash,trace_address,sender,recipient,amount,actor,evidence)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)''',
            (transaction.network.chain_id, transaction.tx_hash, list(location), receipt.sender,
             receipt.recipient, receipt.amount, actor, Json(receipt.evidence)))
        identity = ':'.join((str(transaction.network.chain_id), transaction.tx_hash, '.'.join(map(str, location))))
        ledger.post(cursor, 'native-internal-receipt:' + identity, 'operator_native_funding',
            [('external:custody:NATIVE', -receipt.amount), ('operator:custody:NATIVE', receipt.amount)],
            {'chain_id': transaction.network.chain_id, 'tx_hash': transaction.tx_hash,
             'trace_address': list(location), 'sender': receipt.sender, 'actor': actor})
