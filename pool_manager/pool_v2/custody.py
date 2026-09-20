"""Verified custody identity and operator-native funding; no transfer sender."""

from psycopg2.extras import Json

from . import ledger
from .chain import ConfirmedTransaction
from .database import lock
from .money import Conflict, FundsError


def bind(cursor, network):
    lock(cursor, 'custody-identity')
    cursor.execute('SELECT * FROM custody_identity WHERE name=\'custody\'')
    existing = cursor.fetchone()
    values = (network.chain_id, network.token, network.custody, network.decimals)
    if existing:
        if (existing['chain_id'], existing['token'], existing['wallet'], existing['decimals']) != values:
            raise Conflict('configured chain, token or wallet differs from the ledger custody identity')
    else:
        cursor.execute('''SELECT 1 FROM transfers WHERE chain_id<>%s OR token<>%s
            OR (sender<>%s AND recipient<>%s) LIMIT 1''',
            (network.chain_id, network.token, network.custody, network.custody))
        if cursor.fetchone():
            raise Conflict('existing transfer history differs from this custody identity')
        cursor.execute('INSERT INTO custody_identity(name,chain_id,token,wallet,decimals) VALUES (\'custody\',%s,%s,%s,%s)', values)


def save_transaction(cursor, transaction):
    if not isinstance(transaction, ConfirmedTransaction):
        raise FundsError('only a verified chain transaction can be recorded')
    bind(cursor, transaction.network)
    lock(cursor, f'chain-transaction:{transaction.network.chain_id}:{transaction.tx_hash}')
    values = {key: getattr(transaction, key) for key in (
        'tx_hash', 'sender', 'recipient', 'nonce', 'successful', 'value', 'fee', 'fee_model',
        'block_number', 'block_hash', 'block_timestamp')}
    values['chain_id'] = transaction.network.chain_id
    cursor.execute('SELECT * FROM chain_transactions WHERE chain_id=%s AND tx_hash=%s', (values['chain_id'], values['tx_hash']))
    previous = cursor.fetchone()
    if previous:
        if any(previous[key] != value for key, value in values.items()):
            raise Conflict('confirmed transaction facts changed; reconciliation required')
        return False
    cursor.execute('SELECT tx_hash FROM chain_transactions WHERE chain_id=%s AND sender=%s AND nonce=%s',
                   (values['chain_id'], values['sender'], values['nonce']))
    if cursor.fetchone():
        raise Conflict('a different finalized transaction already consumed this sender nonce')
    cursor.execute('''INSERT INTO chain_transactions(chain_id,tx_hash,sender,recipient,nonce,successful,value,
        fee,fee_model,block_number,block_hash,block_timestamp,evidence) VALUES
        (%(chain_id)s,%(tx_hash)s,%(sender)s,%(recipient)s,%(nonce)s,%(successful)s,%(value)s,
         %(fee)s,%(fee_model)s,%(block_number)s,%(block_hash)s,%(block_timestamp)s,%(evidence)s)''',
        {**values, 'evidence': Json(transaction.evidence)})
    return True


def receive_native(database, transaction):
    if not isinstance(transaction, ConfirmedTransaction) or (not transaction.successful or transaction.value <= 0
            or transaction.recipient != transaction.network.custody or transaction.sender == transaction.network.custody):
        raise FundsError('operator native funding requires a verified incoming direct transfer')
    with database.transaction() as cursor:
        bind(cursor, transaction.network)
        save_transaction(cursor, transaction)
        ledger.post(cursor, f'native-receipt:{transaction.network.chain_id}:{transaction.tx_hash}', 'operator_native_funding',
            [('external:custody:NATIVE', -transaction.value), ('operator:custody:NATIVE', transaction.value)],
            {'chain_id': transaction.network.chain_id, 'tx_hash': transaction.tx_hash, 'sender': transaction.sender})
