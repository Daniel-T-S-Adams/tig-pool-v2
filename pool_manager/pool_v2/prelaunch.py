"""Operator-reviewed opening credit for a confirmed prelaunch TIG top-up."""

from dataclasses import asdict

from psycopg2.extras import Json

from . import benchmarks,custody,funding,ledger
from .chain import ConfirmedTransaction,ConfirmedTransfer,Network
from .database import lock
from .money import Conflict,FundsError


MAINNET_API='https://mainnet-api.tig.foundation'
MAINNET_CHAIN=8453
MAINNET_TOKEN='0x0c03ce270b4826ec62e7dd007f0b716068639f7b'
TOPUP_ADDRESS='0x0000000000000000000000000000000000000001'


def credit_mainnet_topup(database,capture_id,transaction,transfer,*,actor,reason):
    """Credit a prelaunch top-up only after TIG and finalized Base evidence agree.

    This is a one-time setup operation, not a general correction route. The
    source token transfer happened before the pool began custody accounting;
    this records the resulting TIG fee balance without moving any token.
    """
    if not isinstance(actor,str) or not actor.strip() or len(actor)>200 or not isinstance(reason,str) or not reason.strip():
        raise FundsError('prelaunch protocol credit requires a named operator and reason')
    if not isinstance(transaction,ConfirmedTransaction) or not isinstance(transfer,ConfirmedTransfer):
        raise FundsError('verified Base transaction and token receipt are required')
    network=transaction.network
    if (network.chain_id,network.token,network.require_finalized,network.confirmations)!=(
            MAINNET_CHAIN,MAINNET_TOKEN,True,12):
        raise FundsError('prelaunch credit requires the configured finalized Base network')
    if transfer.network!=network or not transaction.successful or transaction.value!=0 or (
            transaction.sender,transaction.recipient,transaction.tx_hash,transaction.block_number,transaction.block_hash)!=(
            network.custody,network.token,transfer.tx_hash,transfer.block_number,transfer.block_hash) or (
            transfer.sender,transfer.recipient)!=(network.custody,TOPUP_ADDRESS):
        raise Conflict('Base transaction does not prove the pool wallet top-up transfer')

    data=funding.read_capture(database,capture_id)
    snapshot=funding.verify(data)
    if data.get('api_origin')!=MAINNET_API or snapshot['player_id']!=network.custody:
        raise FundsError('TIG evidence is not a mainnet capture for this pool wallet')
    matches=[row for row in snapshot['topups'].values() if
        (row['tx_hash'],row['log_index'],row['amount'])==(transfer.tx_hash,transfer.log_index,transfer.amount)]
    raw_topups=data['player_data']['topups']
    if (snapshot['recipient']!=TOPUP_ADDRESS or len(raw_topups)!=1 or raw_topups[0]['state'] is None
            or len(matches)!=1 or len(snapshot['topups'])!=1 or snapshot['available']!=transfer.amount):
        raise Conflict('TIG must show this exact, unused top-up as the full available fee balance')
    topup=matches[0]
    evidence={
        'network':asdict(network),
        'transaction':{'tx_hash':transaction.tx_hash,'sender':transaction.sender,'recipient':transaction.recipient,
            'nonce':transaction.nonce,'successful':transaction.successful,'value':transaction.value,'fee':transaction.fee,
            'fee_model':transaction.fee_model,'block_number':transaction.block_number,'block_hash':transaction.block_hash,
            'block_timestamp':transaction.block_timestamp.isoformat(),'rpc':transaction.evidence},
        'transfer':{'event_id':transfer.event_id,'tx_hash':transfer.tx_hash,'log_index':transfer.log_index,
            'block_number':transfer.block_number,'block_hash':transfer.block_hash,'sender':transfer.sender,
            'recipient':transfer.recipient,'amount':transfer.amount,'rpc':transfer.evidence}}
    with database.transaction() as cursor:
        custody.bind(cursor,network)
        lock(cursor,'operator:protocol-budget')
        lock(cursor,'protocol-funding')
        cursor.execute('''SELECT id,complete,available,
            extract(epoch FROM clock_timestamp()-checked_at) AS age FROM funding_captures
            WHERE player_id=%s ORDER BY checked_at DESC,created_at DESC,id DESC LIMIT 1''',(network.custody,))
        latest=cursor.fetchone()
        cursor.execute('SELECT 1 FROM funding_alerts LIMIT 1')
        if (not latest or latest['id']!=capture_id or not latest['complete']
                or not -5<=latest['age']<=120 or int(latest['available'])!=transfer.amount
                or cursor.fetchone()):
            raise Conflict('opening credit requires the latest fresh, complete, conflict-free TIG capture')
        cursor.execute('SELECT * FROM mainnet_protocol_opening_credits WHERE topup_id=%s',(topup['id'],))
        previous=cursor.fetchone()
        if previous:
            if (previous['capture_id'],previous['tx_hash'],previous['log_index'],int(previous['amount']))!=(
                    capture_id,transfer.tx_hash,transfer.log_index,transfer.amount):
                raise Conflict('mainnet top-up already has a different opening credit')
            return dict(previous)
        cursor.execute('''SELECT 1 FROM reservations UNION ALL SELECT 1 FROM protocol_topups
            UNION ALL SELECT 1 FROM protocol_topup_credits UNION ALL SELECT 1 FROM protocol_opening_credits LIMIT 1''')
        if cursor.fetchone() or funding.balance(cursor):
            raise Conflict('prelaunch credit is only available before pool protocol spending or other credits')
        cursor.execute('SELECT player_id FROM protocol_identity WHERE name=\'fees\'')
        identity=cursor.fetchone()
        if not identity or identity['player_id']!=network.custody:
            raise Conflict('pool protocol identity must be bound to the verified custody wallet')
        journal=ledger.post(cursor,'mainnet-prelaunch-topup:'+topup['id'],'operator_mainnet_prelaunch_protocol_funding',
            [('external:protocol:TIG',-transfer.amount),(benchmarks.OPERATOR_FEES,transfer.amount)],
            {'topup_id':topup['id'],'capture_id':capture_id,'player_id':network.custody,
             'tx_hash':transfer.tx_hash,'log_index':transfer.log_index,'base_block':transfer.block_number,
             'base_hash':transfer.block_hash,'actor':actor,'reason':reason})
        cursor.execute('''INSERT INTO mainnet_protocol_opening_credits
            (topup_id,player_id,capture_id,tx_hash,log_index,amount,base_block,base_hash,actor,reason,evidence,journal_id)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *''',
            (topup['id'],network.custody,capture_id,transfer.tx_hash,transfer.log_index,transfer.amount,
             transfer.block_number,transfer.block_hash,actor,reason,Json(evidence),journal))
        return dict(cursor.fetchone())
