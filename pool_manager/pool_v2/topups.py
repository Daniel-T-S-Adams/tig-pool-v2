"""Operator-funded manual TIG fee top-ups, with separate chain and TIG receipts."""

from dataclasses import asdict
from datetime import datetime,timedelta,timezone
import uuid

from psycopg2.extras import Json

from . import benchmarks,chain_observer,custody,deposits,funding,ledger
from .chain import ConfirmedTransaction,ConfirmedTransfer,CustodyPreflight,FEE_MODELS,Network,hex_bytes
from .database import lock
from .money import Conflict,FundsError,units


def held(identity,asset):return f'operator:topup:{identity}:{asset}'


def begin(database,request_key,preflight,policy_capture,*,amount,fee_limit,fee_model,actor):
    units(amount,positive=True);units(fee_limit,positive=True)
    if (not isinstance(preflight,CustodyPreflight) or fee_model not in FEE_MODELS or amount>=2**256
            or not isinstance(actor,str) or not 1<=len(actor)<=128
            or not isinstance(request_key,str) or not 1<=len(request_key)<=128):
        raise FundsError('top-up requires a verified preflight, fee rule, operator and bounded request key')
    policy=funding.read(database,policy_capture)
    with database.transaction() as cursor:
        custody.bind(cursor,preflight.network)
        lock(cursor,'operator:protocol-budget')
        cursor.execute('SELECT * FROM protocol_topups WHERE request_key=%s',(request_key,))
        old=cursor.fetchone()
        if old:
            if (int(old['amount']),int(old['fee_limit']),old['actor'])!=(amount,fee_limit,actor):
                raise Conflict('top-up request key was reused')
            return dict(old)
        from . import pilot
        pilot.prohibit_topup(cursor)
        now=datetime.now(timezone.utc)
        if not -5<=(now-preflight.checked_at).total_seconds()<=20 or not -5<=(now-policy['checked_at']).total_seconds()<=120:
            raise Conflict('top-up preflight or protocol policy is stale')
        if not preflight.network.require_finalized or preflight.network.custody!=policy['player_id']:
            raise Conflict('top-up must fund the same finalized custody and benchmarker account')
        if amount<policy['minimum']:raise FundsError('top-up amount is below the captured protocol minimum')
        if (ledger.backing(cursor),ledger.backing(cursor,'NATIVE'))!=(preflight.token_balance,preflight.native_balance):
            raise Conflict('wallet balances do not reconcile before top-up')
        wallet=chain_observer.status(database,cursor=cursor)
        if wallet['initialized'] and not wallet['ready']:raise Conflict('custody observer requires reconciliation before top-up')
        protocol=funding.status(database,cursor=cursor)
        if not protocol['ready']:raise Conflict('protocol fee balance requires reconciliation before another top-up')
        if protocol['observation']['id']!=policy_capture:
            raise Conflict('top-up must use the most recent protocol policy observation')
        identity=uuid.uuid4()
        custody.reserve_nonce(cursor,identity,preflight.network,preflight.nonce,'protocol_topup')
        for asset in ('TIG','NATIVE'):ledger.account(cursor,held(identity,asset),'operator_commitment',asset)
        ledger.post(cursor,f'topup:{identity}:reserve','operator_topup_reservation',
            [('operator:custody:TIG',-amount),(held(identity,'TIG'),amount),
             ('operator:custody:NATIVE',-fee_limit),(held(identity,'NATIVE'),fee_limit)],{'actor':actor,'policy_capture':policy_capture})
        proof={'block_number':preflight.block_number,'checked_at':preflight.checked_at.isoformat(),
            'token_balance':preflight.token_balance,'native_balance':preflight.native_balance,'evidence':preflight.evidence}
        cursor.execute('''INSERT INTO protocol_topups(id,request_key,amount,fee_limit,network,recipient,fee_model,
            policy_capture,preflight,actor,state) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'uncertain') RETURNING *''',
            (identity,request_key,amount,fee_limit,Json(asdict(preflight.network)),policy['recipient'],fee_model,
             policy_capture,Json(proof),actor))
        return dict(cursor.fetchone())


def instructions(database,identity):
    with database.transaction() as cursor:
        cursor.execute('''SELECT t.*,s.chain_id,s.sender,s.nonce,p.tx_hash,p.transfer_event,p.fee FROM protocol_topups t
            JOIN custody_sends s ON s.id=t.id LEFT JOIN custody_payments p ON p.send_id=t.id WHERE t.id=%s''',(identity,))
        row=cursor.fetchone()
        if not row:raise FundsError('unknown operator top-up')
        value=dict(row)
        value['token']=value['network']['token']
        value['transaction']={'from':value['sender'],'to':value['token'],'chainId':hex(value['chain_id']),
            'nonce':hex(int(value['nonce'])),'value':'0x0',
            'data':'0xa9059cbb'+'0'*24+value['recipient'][2:]+f"{int(value['amount']):064x}"}
        return value


def claim(database,identity,tx_hash,*,actor):
    tx_hash=hex_bytes(tx_hash,32)
    if not actor:raise FundsError('operator identity required')
    with database.transaction() as cursor:
        cursor.execute('SELECT 1 FROM protocol_topups WHERE id=%s',(identity,))
        if not cursor.fetchone():raise FundsError('unknown operator top-up')
        cursor.execute('INSERT INTO topup_transaction_claims(send_id,tx_hash,actor) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING',
            (identity,tx_hash,actor))


def _transaction(row,transaction,transfer):
    if not isinstance(transaction,ConfirmedTransaction):raise FundsError('verified finalized chain transaction required')
    network=Network(**row['network'])
    if (transaction.network.chain_id,transaction.network.token,transaction.network.custody,transaction.sender,transaction.nonce,transaction.fee_model)!=(
        network.chain_id,network.token,network.custody,row['sender'],int(row['nonce']),row['fee_model']):
        raise Conflict('top-up transaction differs from the frozen route, nonce or fee rule')
    if not transaction.network.require_finalized or transaction.network.confirmations<network.confirmations:
        raise Conflict('top-up confirmation policy cannot be weakened')
    if transaction.block_number<=row['preflight']['block_number'] or transaction.block_timestamp+timedelta(seconds=5)<row['sent_at']:
        raise Conflict('top-up transaction predates the recorded intent')
    if transfer is not None:
        if not isinstance(transfer,ConfirmedTransfer):raise FundsError('verified token event required')
        if not transfer.network.require_finalized or transfer.network.confirmations<network.confirmations:
            raise Conflict('top-up confirmation policy cannot be weakened')
        if (transfer.network.chain_id,transfer.network.token,transfer.network.custody,transfer.sender,transfer.recipient,
            transfer.amount,transfer.tx_hash,transfer.block_hash)!=(network.chain_id,network.token,network.custody,
                row['sender'],row['recipient'],int(row['amount']),transaction.tx_hash,transaction.block_hash):
            raise Conflict('top-up token event does not match the full frozen amount and destination')


def reconcile(database,identity,transaction,transfer=None):
    row=instructions(database,identity)
    _transaction(row,transaction,None)
    # Keep valid raw chain evidence even if additional operator fee funding is needed.
    with database.transaction() as cursor:custody.save_transaction(cursor,transaction)
    _transaction(row,transaction,transfer)
    with database.transaction() as cursor:
        custody.bind(cursor,transaction.network)
        lock(cursor,'operator:protocol-budget')
        cursor.execute('SELECT state FROM protocol_topups WHERE id=%s FOR UPDATE',(identity,))
        state=cursor.fetchone()['state']
        cursor.execute('SELECT * FROM custody_payments WHERE send_id=%s',(identity,))
        previous=cursor.fetchone()
        if previous:
            if previous['tx_hash']!=transaction.tx_hash or previous['transfer_event']!=(transfer.event_id if transfer else None):
                raise Conflict('top-up already has a different final transaction or token event')
            return {'id':str(identity),'state':state,'fee':int(previous['fee'])}
        if state!='uncertain':raise Conflict('top-up no longer has an unresolved send')
        if not transaction.successful:
            if transfer is not None:raise Conflict('failed top-up cannot contain a successful token event')
            outcome='failed'
        elif transfer is not None:
            if transaction.recipient!=row['token'] or transaction.value!=0:
                raise Conflict('top-up requires the frozen direct token transaction; reconcile other wallet costs explicitly')
            deposits.save_transfer(cursor,transfer)
            outcome='awaiting_protocol'
        else:
            tx=transaction.evidence['transaction']
            if transaction.recipient!=transaction.sender or transaction.value!=0 or tx.get('input') not in ('0x','') or transaction.evidence['receipt']['logs']:
                raise Conflict('unmatched successful top-up needs explicit wallet reconciliation')
            outcome='cancelled'
        amount,fee_limit=int(row['amount']),int(row['fee_limit'])
        destination='external:custody:TIG' if outcome=='awaiting_protocol' else 'operator:custody:TIG'
        journal=ledger.post(cursor,f'topup:{identity}:cash','operator_topup_'+outcome,
            [(held(identity,'TIG'),-amount),(destination,amount),(held(identity,'NATIVE'),-fee_limit),
             ('operator:custody:NATIVE',fee_limit-transaction.fee),('external:custody:NATIVE',transaction.fee)],
            {'tx_hash':transaction.tx_hash,'transfer_event':transfer.event_id if transfer else None,
             'fee':transaction.fee,'fee_model':transaction.fee_model})
        custody.record_payment(cursor,identity,transaction,transfer.event_id if transfer else None,journal)
        cursor.execute('UPDATE protocol_topups SET state=%s WHERE id=%s',(outcome,identity))
        return {'id':str(identity),'state':outcome,'fee':transaction.fee}


def credit(database,identity,capture_id):
    snapshot=funding.read(database,capture_id)
    row=instructions(database,identity)
    if snapshot['player_id']!=row['sender']:raise Conflict('protocol confirmation belongs to another account')
    with database.transaction() as cursor:
        custody.bind(cursor,Network(**row['network']))
        lock(cursor,'operator:protocol-budget')
        cursor.execute('SELECT 1 FROM funding_alerts LIMIT 1')
        if cursor.fetchone():raise Conflict('confirmed protocol top-up history requires reconciliation before credit')
        cursor.execute('SELECT state FROM protocol_topups WHERE id=%s FOR UPDATE',(identity,))
        state=cursor.fetchone()['state']
        cursor.execute('SELECT * FROM protocol_topup_credits WHERE send_id=%s',(identity,))
        old=cursor.fetchone()
        if old:return dict(old)
        if state!='awaiting_protocol':raise Conflict('verified outgoing token transfer is required before protocol credit')
        cursor.execute('SELECT * FROM transfers WHERE event_id=%s',(row['transfer_event'],))
        event=cursor.fetchone()
        matches=[value for value in snapshot['topups'].values() if (value['tx_hash'],value['log_index'],value['amount'])==(
            event['tx_hash'],event['log_index'],int(row['amount']))]
        if len(matches)!=1:raise Conflict('TIG has not uniquely confirmed this exact top-up event')
        match=matches[0]
        cursor.execute('SELECT send_id FROM protocol_topup_credits WHERE topup_id=%s OR transfer_event=%s',(match['id'],row['transfer_event']))
        if cursor.fetchone():raise Conflict('protocol top-up credit has already been consumed')
        journal=ledger.post(cursor,f'topup:{identity}:protocol','operator_protocol_funding',
            [('external:protocol:TIG',-int(row['amount'])),(benchmarks.OPERATOR_FEES,int(row['amount']))],
            {'topup_id':match['id'],'capture_id':capture_id,'transfer_event':row['transfer_event']})
        cursor.execute('INSERT INTO protocol_topup_credits(send_id,topup_id,capture_id,transfer_event,journal_id) VALUES (%s,%s,%s,%s,%s) RETURNING *',
            (identity,match['id'],capture_id,row['transfer_event'],journal))
        result=dict(cursor.fetchone())
        cursor.execute("UPDATE protocol_topups SET state='credited' WHERE id=%s",(identity,))
        return result
