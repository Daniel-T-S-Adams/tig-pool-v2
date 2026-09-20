"""Immutable report observations and complete, post-X+2 arbitration seals.

The deployment adapter supplies a proven reporting-round scope. It must not
guess that a protocol reporting round equals the benchmark's creation round.
"""

import gzip
import hashlib
import json
import re
import uuid

from psycopg2.extras import Json

from .block_observer import BlockStore
from .database import lock
from .ledger import canonical,fingerprint
from .money import Conflict,FundsError,units
from .protocol import ProtocolDataError,_index,_integer,validate_reports


def scope(database,creation_round,reporting_rounds,*,adapter_version,evidence):
    """Trusted protocol adapter only; record its verified boundary mapping."""
    units(creation_round,positive=True)
    if not reporting_rounds or any(type(value) is not int or value<=0 for value in reporting_rounds) or len(set(reporting_rounds))!=len(reporting_rounds):
        raise FundsError('explicit, unique protocol reporting rounds required')
    if not adapter_version or not evidence:
        raise FundsError('reporting-round mapping requires adapter version and evidence')
    values=sorted(reporting_rounds)
    with database.transaction() as cursor:
        lock(cursor,'reports-store')
        _check_scope(cursor,creation_round,values)
        cursor.execute('SELECT * FROM round_report_scopes WHERE creation_round=%s',(creation_round,))
        previous=cursor.fetchone()
        if previous:
            if (previous['reporting_rounds'],previous['adapter_version'],previous['evidence'])!=(values,adapter_version,evidence):
                raise Conflict('reporting-round scope is already frozen')
            return
        cursor.execute('INSERT INTO round_report_scopes(creation_round,reporting_rounds,adapter_version,evidence) VALUES (%s,%s,%s,%s)',
                       (creation_round,values,adapter_version,Json(evidence)))


def deadline(database,creation_round,block_id):
    _,snapshot=BlockStore(database).read(block_id)
    if snapshot.round<creation_round+3:
        raise Conflict('the end of creation round X+2 has not been observed')
    return snapshot


def require_block(cursor,block_id):
    """Caller holds observation-stream while performing financial mutations."""
    cursor.execute("""SELECT 1 FROM observed_blocks b JOIN observation_alerts a ON a.height=b.height
        WHERE b.id=%s AND a.kind='conflicting-block'""",(block_id,))
    if cursor.fetchone():raise Conflict('settlement evidence has a conflicting observed block')


def _facts(payload,reporting_round,height):
    validated=validate_reports(payload)
    report_rows=_index(payload['reports'],'id','reports')
    decisions=_index(payload['arbitrations'],'report_id','arbitrations')
    facts,arbitrations={},{}
    for identity,report in report_rows.items():
        details=report['details']
        if details['round']!=reporting_round:
            raise ProtocolDataError('report response differs from its requested reporting round')
        if report.get('state') is not None:
            confirmed=_integer(report['state']['block_confirmed'],'report confirmation')
            if confirmed>height:raise ProtocolDataError('report confirmation follows the captured block')
            facts[identity]=(reporting_round,details['benchmark_id'],details['benchmarker'],details['nonce'],confirmed)
        arbitration=decisions.get(identity)
        if arbitration and arbitration.get('state') is not None:
            confirmed=_integer(arbitration['state']['block_confirmed'],'arbitration confirmation')
            if confirmed>height:raise ProtocolDataError('arbitration confirmation follows the captured block')
            arbitrations[identity]=(arbitration['details']['result'],confirmed)
    return validated,facts,arbitrations


def record(database,reporting_round,block_id,payload,*,metadata=None,error=None):
    """Preserve malformed or incomplete responses without treating them as empty."""
    units(reporting_round,positive=True)
    _,snapshot=BlockStore(database).read(block_id)
    encoded=canonical(payload).encode();digest=hashlib.sha256(encoded).hexdigest()
    input_digest=fingerprint({'sha256':digest,'metadata':metadata or {},'error':error})
    identity=uuid.uuid4()
    try:
        _,facts,arbitrations=_facts(payload,reporting_round,snapshot.height)
    except (ProtocolDataError,KeyError,TypeError) as failure:
        facts,arbitrations={},{}
        error=str(failure)
    with database.transaction() as cursor:
        lock(cursor,'reports-store')
        cursor.execute('SELECT id,complete,error FROM report_captures WHERE reporting_round=%s AND block_id=%s AND input_digest=%s',
                       (reporting_round,block_id,input_digest))
        previous=cursor.fetchone()
        if previous:return dict(previous)
        cursor.execute('''SELECT * FROM confirmed_reports WHERE (reporting_round=%s AND confirmed_height<=%s)
            OR report_id=ANY(%s)''',(reporting_round,snapshot.height,list(facts)))
        known=cursor.fetchall()
        for report in known:
            expected=(report['reporting_round'],report['benchmark_id'],report['benchmarker'],int(report['nonce']),report['confirmed_height'])
            if facts.get(report['report_id'])!=expected:
                error='capture omits or contradicts a previously confirmed report'
        cursor.execute('''SELECT a.* FROM confirmed_arbitrations a JOIN confirmed_reports r ON r.report_id=a.report_id
            WHERE (r.reporting_round=%s AND a.confirmed_height<=%s) OR a.report_id=ANY(%s)''',
            (reporting_round,snapshot.height,list(arbitrations)))
        for arbitration in cursor.fetchall():
            if arbitrations.get(arbitration['report_id'])!=(arbitration['result'],arbitration['confirmed_height']):
                error='capture omits or contradicts a previously confirmed arbitration'
        cursor.execute('''INSERT INTO report_captures(id,reporting_round,block_id,payload_sha256,input_digest,compressed_payload,metadata,complete,error)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)''',(identity,reporting_round,block_id,digest,input_digest,gzip.compress(encoded,mtime=0),Json(metadata or {}),error is None,error))
        if error is None:
            for report_id,values in facts.items():
                cursor.execute('''INSERT INTO confirmed_reports(report_id,reporting_round,benchmark_id,benchmarker,nonce,confirmed_height,capture_id)
                    VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING''',(report_id,*values,identity))
            for report_id,values in arbitrations.items():
                cursor.execute('INSERT INTO confirmed_arbitrations(report_id,result,confirmed_height,capture_id) VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING',
                               (report_id,*values,identity))
    return {'id':identity,'complete':error is None,'error':error}


def seal(database,creation_round,capture_ids,block_id):
    deadline(database,creation_round,block_id)
    if not capture_ids or len(set(map(str,capture_ids)))!=len(capture_ids):raise FundsError('unique complete report captures required')
    with database.transaction() as cursor:
        lock(cursor,'reports-store');lock(cursor,'round:'+str(creation_round));lock(cursor,'observation-stream')
        require_block(cursor,block_id)
        cursor.execute('SELECT * FROM round_report_scopes WHERE creation_round=%s',(creation_round,))
        scope_row=cursor.fetchone()
        if not scope_row:raise Conflict('verified protocol reporting-round scope is missing')
        _check_scope(cursor,creation_round,scope_row['reporting_rounds'])
        cursor.execute('''SELECT c.*,b.round AS observed_round,b.height FROM report_captures c
            JOIN observed_blocks b ON b.id=c.block_id WHERE c.id=ANY(%s)''',(list(map(uuid.UUID,map(str,capture_ids))),))
        captures=cursor.fetchall()
        if len(captures)!=len(capture_ids) or sorted(row['reporting_round'] for row in captures)!=scope_row['reporting_rounds']:
            raise Conflict('captures do not cover every required reporting round exactly once')
        combined={}
        for capture in captures:
            require_block(cursor,capture['block_id'])
            if not capture['complete'] or capture['observed_round']<creation_round+3:
                raise Conflict('report capture is incomplete or predates the end of X+2')
            cursor.execute('''SELECT c.id FROM report_captures c JOIN observed_blocks b ON b.id=c.block_id
                WHERE c.reporting_round=%s ORDER BY b.height DESC,c.created_at DESC LIMIT 1''',(capture['reporting_round'],))
            if cursor.fetchone()['id']!=capture['id']:
                raise Conflict('a later report observation must be reconciled before sealing')
            encoded=gzip.decompress(bytes(capture['compressed_payload']))
            if hashlib.sha256(encoded).hexdigest()!=capture['payload_sha256']:
                raise Conflict('stored report payload checksum failed')
            payload=json.loads(encoded)
            require_known_facts(cursor,capture['reporting_round'],capture['height'],payload)
            values=validate_reports(payload)
            if set(combined)&set(values):raise Conflict('report appears in multiple reporting rounds')
            combined.update(values)
        digest=fingerprint({'round':creation_round,'scope':scope_row['reporting_rounds'],'captures':sorted(map(str,capture_ids)),'reports':combined})
        cursor.execute('SELECT * FROM round_report_seals WHERE input_digest=%s',(digest,))
        previous=cursor.fetchone()
        if previous:return dict(previous)
        cursor.execute('''INSERT INTO round_report_seals(id,creation_round,block_id,capture_ids,reports,input_digest)
            VALUES (%s,%s,%s,%s,%s,%s) RETURNING *''',
            (uuid.uuid4(),creation_round,block_id,list(map(uuid.UUID,sorted(map(str,capture_ids)))),Json(combined),digest))
        return dict(cursor.fetchone())


def require_seal(cursor,identity,creation_round):
    cursor.execute('SELECT * FROM round_report_seals WHERE id=%s AND creation_round=%s',(identity,creation_round))
    seal=cursor.fetchone()
    if not seal:raise Conflict('a matching post-X+2 report seal is required')
    cursor.execute('SELECT reporting_rounds FROM round_report_scopes WHERE creation_round=%s',(creation_round,))
    _check_scope(cursor,creation_round,cursor.fetchone()['reporting_rounds'])
    require_block(cursor,seal['block_id'])
    cursor.execute('''SELECT c.*,b.height FROM report_captures c JOIN observed_blocks b ON b.id=c.block_id
        WHERE c.id=ANY(%s)''',(seal['capture_ids'],))
    for capture in cursor.fetchall():
        require_block(cursor,capture['block_id'])
        cursor.execute('''SELECT c.id FROM report_captures c JOIN observed_blocks b ON b.id=c.block_id
            WHERE reporting_round=%s ORDER BY b.height DESC,c.created_at DESC LIMIT 1''',(capture['reporting_round'],))
        if cursor.fetchone()['id']!=capture['id']:
            raise Conflict('report observations changed after this seal; reconcile a new version')
        encoded=gzip.decompress(bytes(capture['compressed_payload']))
        if hashlib.sha256(encoded).hexdigest()!=capture['payload_sha256']:
            raise Conflict('stored report payload checksum failed')
        require_known_facts(cursor,capture['reporting_round'],capture['height'],json.loads(encoded))
    return dict(seal)


def _check_scope(cursor,creation_round,reporting_rounds):
    cursor.execute('''SELECT reporting_round FROM benchmark_reporting_rounds WHERE creation_round=%s
        UNION SELECT p.reporting_round FROM confirmed_reports p JOIN reservations r ON r.benchmark_id=p.benchmark_id
        WHERE r.creation_round=%s''',(creation_round,creation_round))
    if any(row['reporting_round'] not in reporting_rounds for row in cursor.fetchall()):
        raise Conflict('reporting scope omits a known benchmark reporting round')


def record_index(database,reporting_round,block_id,player_id,challenge_id,payload,*,metadata=None,error=None):
    """Remember positive public reporting-round membership; absence proves nothing.

    A reporting index cannot by itself establish the complete scope for failed
    or not-yet-reportable benchmarks. scope() still requires a verified adapter.
    """
    units(reporting_round,positive=True)
    BlockStore(database).read(block_id)
    encoded=canonical(payload).encode();digest=hashlib.sha256(encoded).hexdigest()
    input_digest=fingerprint({'sha256':digest,'metadata':metadata or {},'error':error})
    identity=uuid.uuid4();identities=[]
    try:
        identities=payload['benchmark_ids']
        if not isinstance(identities,list) or any(not isinstance(value,str) or not re.fullmatch('[0-9a-f]{32}',value) for value in identities) or len(set(identities))!=len(identities):
            raise ProtocolDataError('malformed reportable benchmark index')
    except (KeyError,TypeError,ProtocolDataError) as failure:
        identities=[];error=str(failure)
    with database.transaction() as cursor:
        lock(cursor,'reports-store')
        cursor.execute('''SELECT id,complete,error FROM report_index_captures WHERE reporting_round=%s
            AND block_id=%s AND player_id=%s AND challenge_id=%s AND input_digest=%s''',
            (reporting_round,block_id,player_id,challenge_id,input_digest))
        previous=cursor.fetchone()
        if previous:return dict(previous)
        cursor.execute('''SELECT r.benchmark_id,r.creation_round,r.assignment,m.reporting_round FROM reservations r
            LEFT JOIN benchmark_reporting_rounds m ON m.benchmark_id=r.benchmark_id
            WHERE r.benchmark_id=ANY(%s)''',(identities,))
        owned=cursor.fetchall()
        for row in owned:
            settings=(row['assignment'] or {}).get('settings',{})
            if settings.get('player_id')!=player_id or settings.get('challenge_id')!=challenge_id:
                error='reporting index ownership differs from the immutable assignment'
            if row['reporting_round'] is not None and row['reporting_round']!=reporting_round:
                error='benchmark appears in conflicting protocol reporting rounds'
        cursor.execute('''INSERT INTO report_index_captures(id,reporting_round,block_id,player_id,challenge_id,
            payload_sha256,input_digest,compressed_payload,metadata,complete,error) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
            (identity,reporting_round,block_id,player_id,challenge_id,digest,input_digest,gzip.compress(encoded,mtime=0),Json(metadata or {}),error is None,error))
        if error is None:
            for row in owned:
                cursor.execute('''INSERT INTO benchmark_reporting_rounds(benchmark_id,creation_round,reporting_round,capture_id)
                    VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING''',(row['benchmark_id'],row['creation_round'],reporting_round,identity))
    return {'id':identity,'complete':error is None,'error':error}


def require_known_facts(cursor,reporting_round,height,payload):
    _,facts,arbitrations=_facts(payload,reporting_round,height)
    cursor.execute('SELECT * FROM confirmed_reports WHERE reporting_round=%s AND confirmed_height<=%s',(reporting_round,height))
    for report in cursor.fetchall():
        expected=(report['reporting_round'],report['benchmark_id'],report['benchmarker'],int(report['nonce']),report['confirmed_height'])
        if facts.get(report['report_id'])!=expected:
            raise Conflict('report seal omits or contradicts confirmed evidence learned during replay')
    cursor.execute('''SELECT a.* FROM confirmed_arbitrations a JOIN confirmed_reports r ON r.report_id=a.report_id
        WHERE r.reporting_round=%s AND a.confirmed_height<=%s''',(reporting_round,height))
    for arbitration in cursor.fetchall():
        if arbitrations.get(arbitration['report_id'])!=(arbitration['result'],arbitration['confirmed_height']):
            raise Conflict('report seal omits or contradicts an already confirmed arbitration')
