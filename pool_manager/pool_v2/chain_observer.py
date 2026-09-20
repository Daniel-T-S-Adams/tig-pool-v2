"""Finalized custody transfer capture, offline verification and ledger reconciliation.

Only the read-only RPC adapter is used. Raw responses are retained before any
funds are credited. A missing response never advances the collection cursor.
"""

from dataclasses import asdict
from copy import deepcopy
from datetime import datetime,timezone
import gzip
import hashlib
import json

from psycopg2.extras import Json

from . import custody,deposits,ledger
from .chain import Chain,Network,TRANSFER_TOPIC,hex_bytes,quantity
from .database import lock
from .money import Conflict,FundsError,units


def _header(value,height=None):
    if not isinstance(value,dict):raise FundsError('chain header is unavailable')
    number=quantity(value['number'])
    if height is not None and number!=height:raise FundsError('chain header height differs from requested height')
    hex_bytes(value['hash'],32);quantity(value['timestamp'])
    return number


def _balances(chain,height):
    tag=hex(height)
    token=chain.rpc('eth_call',[{'to':chain.network.token,
        'data':'0x70a08231'+'0'*24+chain.network.custody[2:]},tag])
    native=chain.rpc('eth_getBalance',[chain.network.custody,tag])
    nonce=chain.rpc('eth_getTransactionCount',[chain.network.custody,tag])
    return {'tig':int(hex_bytes(token,32),16),'native':quantity(native),'nonce':quantity(nonce)}


def _read(network,rpc,first,count,checked_at):
    units(first,positive=True)
    if type(count) is not int or not 1<=count<=1000:raise FundsError('chain batch size must be 1 to 1000')
    if not network.require_finalized:raise FundsError('custody indexing requires finalized evidence')
    chain=Chain(network,rpc);chain.verify_network()
    latest=rpc('eth_getBlockByNumber',['latest',False])
    finalized=rpc('eth_getBlockByNumber',['finalized',False])
    head,final=_header(latest),_header(finalized)
    if final>head or not -5<=checked_at.timestamp()-quantity(latest['timestamp'])<=120:
        raise FundsError('chain head is stale or inconsistent')
    target=min(final,head-network.confirmations+1)
    last=min(target,first+count-1)
    if last<first-1:raise Conflict('chain source is behind the recorded custody cursor')
    before=rpc('eth_getBlockByNumber',[hex(first-1),False]);_header(before,first-1)
    opening=_balances(chain,first-1)
    end=before if last==first-1 else rpc('eth_getBlockByNumber',[hex(last),False])
    _header(end,last)
    topic='0x'+'0'*24+network.custody[2:]
    transfers={}
    if last>=first:
        for direction,topics in (('incoming',[TRANSFER_TOPIC,None,topic]),('outgoing',[TRANSFER_TOPIC,topic])):
            logs=rpc('eth_getLogs',[{'address':network.token,'fromBlock':hex(first),'toBlock':hex(last),'topics':topics}])
            if not isinstance(logs,list) or len(logs)>10000:raise FundsError('invalid or oversized custody log response')
            seen=set()
            for item in logs:
                identity=(hex_bytes(item['transactionHash'],32),quantity(item['logIndex']))
                if identity in seen:raise FundsError('duplicate transfer in a log response')
                seen.add(identity)
                value=chain.transfer(*identity)
                if not first<=value.block_number<=last:raise FundsError('transfer falls outside requested finalized range')
                if (value.recipient if direction=='incoming' else value.sender)!=network.custody:
                    raise FundsError('transfer does not match its custody filter')
                receipt_log=next(log for log in value.evidence['receipt']['logs'] if quantity(log['logIndex'])==value.log_index)
                for key in ('address','transactionHash','logIndex','blockNumber','blockHash','topics','data'):
                    if item.get(key)!=receipt_log.get(key):raise FundsError('log response differs from its verified receipt')
                if item.get('removed'):raise FundsError('removed transfer cannot be credited')
                previous=transfers.get(value.event_id)
                if previous and (previous.sender,previous.recipient,previous.amount,previous.block_hash)!=(value.sender,value.recipient,value.amount,value.block_hash):
                    raise Conflict('conflicting custody transfer facts')
                transfers[value.event_id]=value
    closing=_balances(chain,last)
    change=sum((value.amount if value.recipient==network.custody else 0)
        -(value.amount if value.sender==network.custody else 0) for value in transfers.values())
    if closing['tig']-opening['tig']!=change:
        raise Conflict('TIG balance change does not match the complete captured transfer range')
    if rpc('eth_getCode',[network.custody,hex(last)])!='0x':
        raise FundsError('custody indexing currently requires an undelegated EOA wallet')
    repeated=rpc('eth_getBlockByNumber',[hex(last),False]);_header(repeated,last)
    if hex_bytes(repeated['hash'],32)!=hex_bytes(end['hash'],32):raise Conflict('finalized custody anchor changed during capture')
    return {'network':network,'first':first,'last':last,'target':target,'before_hash':hex_bytes(before['hash'],32),
        'block_hash':hex_bytes(end['hash'],32),'opening':opening,'closing':closing,
        'transfers':sorted(transfers.values(),key=lambda value:(value.block_number,value.tx_hash,value.log_index)),
        'checked_at':checked_at}


def capture(network,rpc,first,*,count=1000,source='configured-rpc'):
    """Return the complete or failed raw attempt. Persist this before recording."""
    checked=datetime.now(timezone.utc)
    calls=[]
    def recorded(method,params):
        value=rpc(method,params)
        calls.append({'method':method,'params':deepcopy(params),'result':deepcopy(value)})
        return value
    data={'version':1,'network':asdict(network),'first':first,'count':count,
        'checked_at':checked.isoformat(),'source':source,'calls':calls,'error':None}
    try:_read(network,recorded,first,count,checked)
    except Exception as failure:data['error']=type(failure).__name__
    return data


def verify(data):
    if data.get('version')!=1 or data.get('error'):raise FundsError('custody capture is incomplete or unsupported')
    entries=iter(data['calls'])
    def replay(method,params):
        entry=next(entries,None)
        if entry is None or (entry['method'],entry['params'])!=(method,params):
            raise FundsError('custody archive has missing or reordered RPC evidence')
        return entry['result']
    checked=datetime.fromisoformat(data['checked_at'])
    if checked.tzinfo is None:raise FundsError('custody capture time requires an explicit timezone')
    value=_read(Network(**data['network']),replay,data['first'],data['count'],checked)
    if next(entries,None) is not None:raise FundsError('custody archive has unconsumed RPC evidence')
    return value


def save(database,data):
    raw=ledger.canonical(data).encode()
    identity=hashlib.sha256(raw).hexdigest()
    with database.transaction() as cursor:
        cursor.execute('INSERT INTO chain_captures(id,payload_gzip) VALUES (%s,%s) ON CONFLICT DO NOTHING',
            (identity,gzip.compress(raw,mtime=0)))
    return identity


def read(database,identity):
    with database.transaction() as cursor:
        cursor.execute('SELECT payload_gzip FROM chain_captures WHERE id=%s',(identity,))
        row=cursor.fetchone()
    if not row:raise FundsError('unknown custody capture')
    raw=gzip.decompress(bytes(row['payload_gzip']))
    if hashlib.sha256(raw).hexdigest()!=identity:raise Conflict('custody archive checksum differs')
    return json.loads(raw)


def _failure(database,identity,reason,*,critical=False):
    with database.transaction() as cursor:
        cursor.execute('INSERT INTO chain_alerts(capture_id,kind,details) VALUES (%s,%s,%s)',
            (identity,'canonical-conflict' if critical else 'incomplete-capture',Json({'reason':reason})))
        cursor.execute('''INSERT INTO custody_checks(capture_id,healthy,reason,checked_at)
            VALUES (%s,false,%s,clock_timestamp())''',(identity,reason))


def record(database,data,*,initialize=False):
    identity=save(database,data)
    try:value=verify(data)
    except Exception as failure:
        _failure(database,identity,type(failure).__name__)
        raise
    network=value['network']
    try:
        with database.transaction() as cursor:
            # Calls to deposits.receive below use their own committing transactions.
            # Never hold the custody lock across one of those calls.
            lock(cursor,'chain-stream')
            cursor.execute("SELECT * FROM chain_stream WHERE name='custody'")
            stream=cursor.fetchone()
            if not stream:
                if not initialize:raise Conflict('custody observer requires an explicit starting height')
                if any(value['opening'].values()):raise Conflict('start custody collection before the wallet first receives funds or sends transactions')
                custody.bind(cursor,network)
                if ledger.backing(cursor) or ledger.backing(cursor,'NATIVE'):
                    raise Conflict('custody observer must initialize before accepting funds')
                cursor.execute("""INSERT INTO chain_stream(name,network,start_height,last_height,last_hash)
                    VALUES ('custody',%s,%s,%s,%s)""",(Json(asdict(network)),value['first'],value['first']-1,value['before_hash']))
                # Release the custody lock before external receipt transactions.
                # Initialization is committed on its own and replay remains safe.
        with database.transaction() as cursor:
            lock(cursor,'chain-stream')
            cursor.execute("SELECT * FROM chain_stream WHERE name='custody'")
            stream=cursor.fetchone()
            if stream['network']!=asdict(network):raise Conflict('custody observer network or finality policy changed')
            cursor.execute('SELECT 1 FROM chain_batches WHERE capture_id=%s',(identity,))
            if cursor.fetchone():return {'capture_id':identity,'replayed':True}
            if value['last']<=stream['last_height'] and value['first']<=stream['last_height']:
                if value['last']==stream['last_height'] and value['block_hash']!=stream['last_hash']:
                    raise Conflict('recorded finalized custody anchor changed')
                for transfer in value['transfers']:
                    cursor.execute('SELECT block_number,block_hash,block_timestamp,sender,recipient,amount FROM transfers WHERE event_id=%s',(transfer.event_id,))
                    previous=cursor.fetchone()
                    if not previous or (previous['block_number'],previous['block_hash'],previous['block_timestamp'],previous['sender'],previous['recipient'],int(previous['amount']))!=(
                        transfer.block_number,transfer.block_hash,transfer.block_timestamp,transfer.sender,transfer.recipient,transfer.amount):
                        raise Conflict('recorded finalized custody transfer history changed')
                return {'capture_id':identity,'superseded':True}
            if value['first']!=stream['last_height']+1:
                raise Conflict('custody capture overlaps or skips the current cursor; recapture from the next height')
            if value['before_hash']!=stream['last_hash']:
                raise Conflict('recorded finalized custody anchor changed')
            # Already credited receipts replay without posting another journal.
            for transfer in value['transfers']:
                if transfer.recipient==network.custody and transfer.sender!=network.custody:
                    deposits.receive(database,transfer)
                else:
                    with database.transaction() as receipt_cursor:deposits.save_transfer(receipt_cursor,transfer)
            custody.bind(cursor,network)
            recorded_tig,recorded_native=ledger.backing(cursor),ledger.backing(cursor,'NATIVE')
            cursor.execute('''SELECT count(*) AS count FROM withdrawal_attempt_outcomes o JOIN chain_transactions t
                ON t.chain_id=o.chain_id AND t.tx_hash=o.tx_hash
                WHERE t.chain_id=%s AND t.sender=%s AND t.block_number<=%s''',
                (network.chain_id,network.custody,value['last']))
            accounted=int(cursor.fetchone()['count'])
            cursor.execute('''SELECT 1 FROM transfers t LEFT JOIN withdrawals w ON w.paid_event=t.event_id
                WHERE t.sender=%s AND t.recipient<>%s AND t.amount>0 AND t.block_number<=%s AND w.id IS NULL LIMIT 1''',
                (network.custody,network.custody,value['last']))
            unexplained=bool(cursor.fetchone())
            cursor.execute("SELECT 1 FROM chain_alerts WHERE kind='canonical-conflict' LIMIT 1")
            conflicted=bool(cursor.fetchone())
            balances=value['closing']
            healthy=(recorded_tig,recorded_native)==(balances['tig'],balances['native']) and accounted==balances['nonce'] and not unexplained and not conflicted and value['last']==value['target']
            reason='reconciled' if healthy else 'custody collection is catching up' if value['last']<value['target'] else 'custody balances, transactions or canonical history require reconciliation'
            cursor.execute('''INSERT INTO custody_checks(capture_id,height,target_height,block_hash,actual_tig,actual_native,
                recorded_tig,recorded_native,outgoing_nonce,accounted_nonces,healthy,reason,checked_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                (identity,value['last'],value['target'],value['block_hash'],balances['tig'],balances['native'],recorded_tig,
                 recorded_native,balances['nonce'],accounted,healthy,reason,value['checked_at']))
            cursor.execute('INSERT INTO chain_batches(capture_id,first_height,last_height,block_hash) VALUES (%s,%s,%s,%s)',
                (identity,value['first'],value['last'],value['block_hash']))
            cursor.execute("UPDATE chain_stream SET last_height=%s,last_hash=%s WHERE name='custody'",
                (value['last'],value['block_hash']))
            return {'capture_id':identity,'height':value['last'],'healthy':healthy,'reason':reason}
    except Exception as failure:
        critical=isinstance(failure,Conflict) and str(failure) in (
            'recorded finalized custody anchor changed','recorded finalized custody transfer history changed')
        _failure(database,identity,str(failure) if isinstance(failure,FundsError) else type(failure).__name__,critical=critical)
        raise


def status(database,*,cursor=None):
    from contextlib import nullcontext
    with (database.transaction() if cursor is None else nullcontext(cursor)) as query:
        query.execute("SELECT * FROM chain_stream WHERE name='custody'")
        stream=query.fetchone()
        if not stream:return {'initialized':False,'ready':False}
        query.execute('SELECT *,extract(epoch FROM clock_timestamp()-checked_at) AS age FROM custody_checks ORDER BY id DESC LIMIT 1')
        latest=query.fetchone()
        ready=bool(latest and latest['healthy'] and -5<=latest['age']<=120
            and (int(latest['recorded_tig']),int(latest['recorded_native']))==(ledger.backing(query),ledger.backing(query,'NATIVE')))
        return {'initialized':True,'ready':ready,'stream':dict(stream),'check':dict(latest) if latest else None}
