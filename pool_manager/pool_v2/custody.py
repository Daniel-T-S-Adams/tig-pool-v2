"""Verified custody identity and operator-native funding; no transfer sender."""

from psycopg2.extras import Json

from . import ledger
from .chain import ConfirmedTransaction
from .database import lock
from .money import Conflict, FundsError


def bind(cursor, network):
    lock(cursor, 'custody-identity')
    cursor.execute("SELECT player_id FROM protocol_identity WHERE name='fees'")
    protocol=cursor.fetchone()
    if protocol and protocol['player_id']!=network.custody:
        raise Conflict('custody wallet differs from the bound protocol funding account')
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


def reserve_nonce(cursor,identity,network,nonce,kind):
    """Caller holds custody identity; all send purposes share this fence."""
    from .money import units
    units(nonce)
    if kind not in ('withdrawal','protocol_topup'):raise FundsError('unknown custody send purpose')
    bind(cursor,network)
    cursor.execute('SELECT id FROM custody_sends WHERE chain_id=%s AND sender=%s AND nonce=%s',
        (network.chain_id,network.custody,nonce))
    if cursor.fetchone():raise Conflict('custody nonce is already reserved by another send attempt')
    cursor.execute('INSERT INTO custody_sends(id,chain_id,sender,nonce,kind) VALUES (%s,%s,%s,%s,%s)',
        (identity,network.chain_id,network.custody,nonce,kind))


def record_payment(cursor,identity,transaction,transfer_event,journal_id,*,authorization=None,actor=None,reason=None):
    """One verified finalized transaction can explain only one custody send."""
    cursor.execute('SELECT chain_id,sender,nonce FROM custody_sends WHERE id=%s',(identity,))
    route=cursor.fetchone()
    sender, nonce, fee = transaction.sender, transaction.nonce, transaction.fee
    if authorization is not None:
        from .sponsored_withdrawals import SponsoredWithdrawal
        if (not isinstance(authorization, SponsoredWithdrawal) or authorization.transaction != transaction
                or authorization.transfer.event_id != transfer_event or not actor or not reason):
            raise FundsError('verified sponsored payment and explicit operator review required')
        sender, nonce, fee = transaction.network.custody, authorization.custody_nonce, 0
    if not route or (route['chain_id'],route['sender'],int(route['nonce']))!=(
        transaction.network.chain_id,sender,nonce):
        raise Conflict('payment does not match the shared custody nonce reservation')
    cursor.execute('SELECT * FROM custody_payments WHERE send_id=%s OR (chain_id=%s AND tx_hash=%s)',
        (identity,transaction.network.chain_id,transaction.tx_hash))
    old=cursor.fetchone()
    if old:
        if (str(old['send_id']),old['tx_hash'],old['transfer_event'],int(old['fee']),str(old['journal_id']))!=(
            str(identity),transaction.tx_hash,transfer_event,fee,str(journal_id)):
            raise Conflict('custody transaction already has a different financial attribution')
        return
    if authorization is not None:
        cursor.execute('''INSERT INTO custody_authorization_payments
            (send_id,chain_id,tx_hash,authority,nonce,delegate,actor,reason,evidence)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
            (identity,transaction.network.chain_id,transaction.tx_hash,sender,nonce,authorization.delegate,
             actor,reason,Json(authorization.evidence)))
    cursor.execute('INSERT INTO custody_payments(send_id,chain_id,tx_hash,transfer_event,fee,journal_id) VALUES (%s,%s,%s,%s,%s,%s)',
        (identity,transaction.network.chain_id,transaction.tx_hash,transfer_event,fee,journal_id))
