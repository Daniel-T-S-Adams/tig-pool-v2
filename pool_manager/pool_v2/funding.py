"""Public TIG fee-balance observation and positive top-up confirmation."""

from datetime import datetime,timezone
import gzip
import hashlib
import json
import re

from psycopg2.extras import Json

from . import ledger,members
from .database import lock
from .money import Conflict,FundsError
from .protocol import _integer


def amount(value):
    if not isinstance(value,str) or not re.fullmatch(r'[0-9]{1,78}',value):
        raise FundsError('TIG funding amounts must be exact integer-unit strings')
    return int(value)


def verify(data):
    if data.get('version')!=1 or data.get('error'):raise FundsError('protocol funding capture is incomplete')
    player_id=members.address(data['player_id'])
    start,end=data['start']['block'],data['end']['block']
    if start['id']!=end['id'] or start['details']['height']!=end['details']['height'] or start['config']['topups']!=end['config']['topups']:
        raise Conflict('protocol block changed during funding capture')
    checked=datetime.fromisoformat(data['checked_at'])
    if checked.tzinfo is None:raise FundsError('protocol funding capture time needs a timezone')
    timestamp=_integer(start['details']['timestamp'],'block timestamp')
    height=_integer(start['details']['height'],'block height',1)
    if not -5<=checked.timestamp()-timestamp<=120:raise FundsError('protocol funding block is stale')
    policy=start['config']['topups']
    recipient=members.address(policy['topup_address'])
    minimum=amount(policy['min_topup_amount'])
    if minimum<=0 or recipient==player_id:raise FundsError('invalid protocol top-up route or minimum')
    feed=data['player_data']
    player=feed['player']
    if player is not None and (not isinstance(player,dict) or members.address(player['id'])!=player_id):
        raise FundsError('protocol player response has a different identity')
    if player is None or player['state'] is None:
        if feed['topups']:raise FundsError('uninitialized protocol player has inconsistent top-ups')
        available=0
    else:available=amount(player['state']['available_fee_balance'])
    confirmed={}
    if not isinstance(feed['topups'],list):raise FundsError('complete top-up list required')
    seen=set()
    for row in feed['topups']:
        identity=row['id']
        if not isinstance(identity,str) or not re.fullmatch(r'[0-9a-f]{32}',identity) or identity in seen:
            raise FundsError('invalid or repeated protocol top-up identity')
        seen.add(identity)
        details=row['details']
        if members.address(details['player_id'])!=player_id:raise FundsError('top-up belongs to a different player')
        from .chain import hex_bytes
        tx_hash=hex_bytes(details['tx_hash'],32)
        index=_integer(details['log_idx'],'top-up log index')
        value=amount(details['amount'])
        if value<=0:raise FundsError('protocol top-up amount must be positive')
        if row['state'] is None:continue
        confirmed_at=_integer(row['state']['block_confirmed'],'top-up confirmation height',1)
        if confirmed_at>height:raise FundsError('top-up confirmation is after its observation block')
        confirmed[identity]={'id':identity,'tx_hash':tx_hash,'log_index':index,'amount':value,'height':confirmed_at}
    return {'player_id':player_id,'block_id':start['id'],'height':height,'checked_at':checked,
        'recipient':recipient,'minimum':minimum,'available':available,'topups':confirmed}


def capture(client,player_id):
    data={'version':1,'player_id':members.address(player_id),'api_origin':getattr(client,'base_url',None),
        'checked_at':datetime.now(timezone.utc).isoformat(),'error':None}
    try:
        data['start']=client.get('/get-block',{'include_data':'true'})
        data['player_data']=client.get('/get-player-data',{'block_id':data['start']['block']['id'],'player_id':data['player_id']})
        data['end']=client.get('/get-block',{'include_data':'true'})
        verify(data)
    except Exception as failure:data['error']=type(failure).__name__
    return data


def record(database,data):
    raw=ledger.canonical(data).encode()
    identity=hashlib.sha256(raw).hexdigest()
    result,error=None,None
    try:result=verify(data)
    except Exception as failure:error=type(failure).__name__
    player_id=members.address(data['player_id'])
    checked=datetime.fromisoformat(data['checked_at'])
    if checked.tzinfo is None:raise FundsError('protocol capture time requires timezone')
    with database.transaction() as cursor:
        lock(cursor,'custody-identity')
        lock(cursor,'protocol-funding')
        cursor.execute('SELECT player_id FROM protocol_identity WHERE name=\'fees\'')
        bound=cursor.fetchone()
        if bound and bound['player_id']!=player_id:raise Conflict('protocol funding identity changed')
        cursor.execute('SELECT wallet FROM custody_identity WHERE name=\'custody\'')
        custody=cursor.fetchone()
        if custody and custody['wallet']!=player_id:raise Conflict('direct protocol funding requires custody and benchmarker addresses to match')
        if result and not bound:
            cursor.execute("INSERT INTO protocol_identity(name,player_id) VALUES ('fees',%s)",(player_id,))
        cursor.execute('''INSERT INTO funding_captures(id,payload_gzip,player_id,block_id,height,available,checked_at,complete,error)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING''',
            (identity,gzip.compress(raw,mtime=0),player_id,result['block_id'] if result else None,
             result['height'] if result else None,result['available'] if result else None,checked,result is not None,error))
        conflict=False
        if result:
            for item in result['topups'].values():
                cursor.execute('''SELECT topup_id,player_id,facts FROM protocol_topup_facts WHERE topup_id=%s
                    OR (player_id=%s AND tx_hash=%s AND log_index=%s)''',
                    (item['id'],player_id,item['tx_hash'],item['log_index']))
                known=cursor.fetchone()
                if known and (known['player_id']!=player_id or known['facts']!=item):
                    conflict=True
                elif not known:
                    cursor.execute('INSERT INTO protocol_topup_facts(topup_id,player_id,tx_hash,log_index,facts,capture_id) VALUES (%s,%s,%s,%s,%s,%s)',
                        (item['id'],player_id,item['tx_hash'],item['log_index'],Json(item),identity))
            for item in data['player_data']['topups']:
                if item['state'] is None:
                    cursor.execute('SELECT facts FROM protocol_topup_facts WHERE topup_id=%s',(item['id'],))
                    known=cursor.fetchone()
                    if known and known['facts']['height']<=result['height']:conflict=True
            if conflict:
                cursor.execute('INSERT INTO funding_alerts(capture_id,details) VALUES (%s,%s) ON CONFLICT DO NOTHING',
                    (identity,Json({'kind':'confirmed-topup-conflict','reason':'Previously confirmed top-up identity or facts changed'})))
    return {'capture_id':identity,'complete':result is not None and not conflict,'error':'confirmed-topup-conflict' if conflict else error}


def read_capture(database,identity):
    with database.transaction() as cursor:
        cursor.execute('SELECT payload_gzip FROM funding_captures WHERE id=%s',(identity,))
        row=cursor.fetchone()
    if not row:raise FundsError('unknown protocol funding capture')
    raw=gzip.decompress(bytes(row['payload_gzip']))
    if hashlib.sha256(raw).hexdigest()!=identity:raise Conflict('protocol funding archive checksum differs')
    return json.loads(raw)


def read(database,identity):
    return verify(read_capture(database,identity))


def balance(cursor):
    cursor.execute("SELECT coalesce(sum(balance),0) AS balance FROM accounts WHERE location='protocol' AND asset='TIG' AND kind<>'external'")
    return int(cursor.fetchone()['balance'])


def status(database,*,cursor=None):
    from contextlib import nullcontext
    with (database.transaction() if cursor is None else nullcontext(cursor)) as query:
        query.execute("SELECT player_id FROM protocol_identity WHERE name='fees'")
        identity=query.fetchone()
        if not identity:return {'initialized':False,'ready':False}
        query.execute('''SELECT id,block_id,height,available,complete,error,checked_at,
            extract(epoch FROM clock_timestamp()-checked_at) AS age FROM funding_captures
            WHERE player_id=%s ORDER BY checked_at DESC,created_at DESC,id DESC LIMIT 1''',(identity['player_id'],))
        latest=query.fetchone()
        recorded=balance(query)
        query.execute('SELECT capture_id,details,created_at FROM funding_alerts ORDER BY created_at DESC LIMIT 20')
        conflicts=[dict(row) for row in query.fetchall()]
        ready=bool(not conflicts and latest and latest['complete'] and -5<=latest['age']<=120 and int(latest['available'])==recorded)
        return {'initialized':True,'ready':ready,'player_id':identity['player_id'],'recorded':str(recorded),
            'observation':dict(latest) if latest else None,'conflicts':conflicts}
