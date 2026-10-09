"""Bounded conversion of existing full network archives to compact evidence.

Conversion is additive. Pruning requires a later verified backup containing
the replacement, plus completed credit and reconciliation for that block.
"""

from datetime import datetime, timezone
import gzip
import hashlib
import json
import uuid

from psycopg2.extras import Json, execute_values

from .block_observer import BlockStore, _semantic, manifest_references
from .compact import compact_observation
from .database import lock
from .observation import canonical_json
from .protocol import ProtocolDataError, validate_snapshot
from .retention import parse_time


def backfill(database, *, player_id, limit=200, after_height=-1):
    with database.transaction() as cursor:
        cursor.execute('''SELECT b.id,b.height,b.semantic_digest FROM observed_blocks b
            JOIN capture_attempts a ON a.id=b.attempt_id
            LEFT JOIN compact_block_copies c ON c.block_id=b.id
            WHERE c.block_id IS NULL AND b.height>%s AND NOT (a.manifest ? 'expired')
              AND NOT (a.manifest ? 'compact_ref')
            ORDER BY b.height LIMIT %s''', (after_height, limit))
        rows = cursor.fetchall()
    converted, held = [], []
    for row in rows:
        try:
            observation, _ = BlockStore(database).read(row['id'])
            if observation.get('pool_player_id') != player_id:
                raise ProtocolDataError('legacy capture does not bind the configured pool player')
            projection = compact_observation(observation)
            if row['semantic_digest'] not in projection['source_semantic_digests']:
                raise ProtocolDataError('compact projection does not preserve the legacy source identity')
            snapshot = validate_snapshot(projection)
            raw = canonical_json(projection)
            with database.transaction() as cursor:
                lock(cursor, 'observation-stream')
                cursor.execute('''INSERT INTO compact_block_copies
                    (block_id,source_semantic_digest,projection_digest,payload_sha256,compressed_payload)
                    VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING''',
                    (row['id'], row['semantic_digest'], _semantic(snapshot, projection),
                     hashlib.sha256(raw).hexdigest(), gzip.compress(raw, mtime=0)))
            converted.append(row['id'])
        except ProtocolDataError as failure:
            held.append({'block_id': row['id'], 'reason': str(failure)})
    return {'converted': len(converted), 'held': held,
            'last_height': rows[-1]['height'] if rows else after_height}


def prune(database, *, proof, limit=200):
    covered = parse_time(proof['covered_until'])
    run_id = str(uuid.uuid4())
    with database.transaction() as cursor:
        lock(cursor, 'evidence-retention')
        lock(cursor, 'observation-stream')
        cursor.execute("SET LOCAL pool_v2.retention TO 'on'")
        cursor.execute("SET LOCAL pool_v2.compaction TO 'on'")
        cursor.execute("SELECT set_config('pool_v2.compaction_covered_until',%s,true)", (covered.isoformat(),))
        cursor.execute('''SELECT a.id,a.manifest FROM capture_attempts a
            JOIN observed_blocks b ON b.attempt_id=a.id
            JOIN compact_block_copies c ON c.block_id=b.id
            JOIN credited_blocks credit ON credit.block_id=b.id
            JOIN reconciled_blocks r ON r.block_id=b.id
            WHERE NOT (a.manifest ? 'expired') AND c.expired_at IS NULL
              AND a.created_at<=%s AND c.created_at<=%s
              AND NOT EXISTS (SELECT 1 FROM observation_alerts x
                  WHERE x.height=b.height AND x.kind='conflicting-block')
            ORDER BY b.height LIMIT %s''', (covered, covered, limit))
        rows = cursor.fetchall()
        tomb = {'expired': {'run': run_id, 'at': datetime.now(timezone.utc).isoformat(),
            'policy': 'compact-network-v1', 'covered_until': covered.isoformat()}}
        for row in rows:
            manifest = {'expired': {**tomb['expired'],
                'manifest_sha256': hashlib.sha256(canonical_json(row['manifest'])).hexdigest()}}
            cursor.execute('UPDATE capture_attempts SET manifest=%s,metadata=%s WHERE id=%s',
                           (Json(manifest), Json(tomb), row['id']))
        cursor.execute('''INSERT INTO retention_runs(id,policy,backup_proof,floors,counts,notes)
            VALUES (%s,%s,%s,%s,%s,%s)''', (run_id, Json({'version': 'compact-network-v1'}), Json(proof),
            Json({'covered_until': covered.isoformat()}), Json({'expired_raw_blocks': len(rows)}),
            Json(['Per-block compact replacement; global gaps and financial records preserved.'])))
    return {'run': run_id, 'expired_raw_blocks': len(rows)}


def collect_unused_chunks(database):
    """Remove only chunks no surviving capture references, under the writer lock.

    Run after backfill/pruning. Large remaining legacy captures make this
    operation deliberately defer rather than interrupt live observation.
    """
    with database.transaction() as cursor:
        lock(cursor, 'evidence-retention')
        lock(cursor, 'observation-stream')
        cursor.execute("SELECT count(*) AS n FROM capture_attempts WHERE NOT (manifest ? 'expired') AND NOT (manifest ? 'compact_ref')")
        legacy = cursor.fetchone()['n']
        if legacy > 100:
            return {'removed_chunks': 0, 'deferred': f'{legacy} legacy captures still retain full raw chunks'}
        cursor.execute("SET LOCAL pool_v2.retention TO 'on'")
        cursor.execute('CREATE TEMP TABLE live_compaction_chunks(digest text PRIMARY KEY) ON COMMIT DROP')
        cursor.execute("SELECT manifest FROM capture_attempts WHERE NOT (manifest ? 'expired')")
        manifests = [row['manifest'] for row in cursor.fetchall()]
        refs = set()
        for manifest in manifests:
            refs.update(manifest_references(cursor, manifest))
        if refs:
            execute_values(cursor, 'INSERT INTO live_compaction_chunks(digest) VALUES %s',
                           [(digest,) for digest in sorted(refs)], page_size=2000)
        cursor.execute('''DELETE FROM observation_chunks c WHERE NOT EXISTS
            (SELECT 1 FROM live_compaction_chunks live WHERE live.digest=c.digest)''')
        removed = cursor.rowcount
        cursor.execute('DELETE FROM chunk_last_ref r WHERE NOT EXISTS (SELECT 1 FROM observation_chunks c WHERE c.digest=r.digest)')
    return {'removed_chunks': removed}
