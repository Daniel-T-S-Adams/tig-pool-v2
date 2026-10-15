"""Resolve only proven legacy/compact list permutations, retaining every alert."""

from copy import deepcopy
import hashlib

from psycopg2.extras import Json

from .block_observer import BlockStore, _semantic, _unpack
from .boundary_conflicts import POLICY as BOUNDARY_POLICY, projection_proof
from .compact import FORMAT, compact_observation
from .database import lock
from .observation import canonical_json
from .protocol import ProtocolDataError, validate_snapshot


POLICY = 'legacy-compact-record-order-v1'
PATHS = (('start', 'block', 'config', 'rewards', 'schedule'),
         ('end', 'block', 'config', 'rewards', 'schedule'),
         ('algorithms', 'binarys'))


def ordering_proof(legacy, compact, source_digest):
    """Recreate the entire v1 compact capture from the retained full network.

    Only the three transport record lists may be permuted. No missing network
    records are inferred, and no values or list multiplicities may change.
    """
    if legacy.get('format') or compact.get('format') != FORMAT or compact.get('source_identity_version', 1) != 1:
        raise ProtocolDataError('ordering resolution requires legacy and v1 compact captures')
    original = validate_snapshot(legacy)
    other = validate_snapshot(compact)
    if (original.block_id, original.height) != (other.block_id, other.height):
        raise ProtocolDataError('ordering resolution captures have different block identities')
    if _semantic(original, legacy) != source_digest:
        raise ProtocolDataError('legacy replay does not match the immutable block digest')
    restored = deepcopy(legacy)
    changed = []
    for path in PATHS:
        left, right = restored, compact
        for key in path[:-1]:
            if key not in left or key not in right:
                raise ProtocolDataError('ordering resolution is missing a record list')
            left, right = left[key], right[key]
        before, after = left.get(path[-1]), right.get(path[-1])
        if (not isinstance(before, list) or not isinstance(after, list)
                or not all(isinstance(row, dict) for row in before + after)
                or sorted(map(canonical_json, before)) != sorted(map(canonical_json, after))):
            raise ProtocolDataError('ordering resolution changes record contents or multiplicities')
        if before != after:
            changed.append('/'.join(path))
        left[path[-1]] = deepcopy(after)
    replay = validate_snapshot(restored)
    replay_digest = _semantic(replay, restored)
    candidate = compact_observation(restored, identity_version=1)
    expected = deepcopy(compact)
    # Legacy packing discarded the transport order of pool feed records.
    # Reproduce the canonical source exactly; extra raw-order aliases in the
    # captured metadata need not be regenerated to prove accounting equality.
    aliases = candidate.pop('source_semantic_digests')
    captured_aliases = expected.pop('source_semantic_digests')
    if (replay_digest not in captured_aliases or not set(aliases) <= set(captured_aliases)
            or candidate != expected):
        raise ProtocolDataError('complete network replay does not reproduce the conflicting capture')
    if not changed:
        raise ProtocolDataError('captures have no ordering difference to resolve')
    sha = lambda value: hashlib.sha256(canonical_json(value)).hexdigest()
    return {'policy': POLICY, 'block_id': original.block_id, 'height': original.height,
            'legacy_semantic_digest': source_digest, 'replayed_source_semantic_digest': replay_digest,
            'legacy_payload_sha256': sha(legacy), 'compact_payload_sha256': sha(compact),
            'replayed_network_sha256': sha(restored), 'changed_paths': changed,
            'complete_network_replay_matches': True}


PROOFS = {POLICY: ordering_proof, BOUNDARY_POLICY: projection_proof}


def resolve(database, *, alert_id, conflicting_attempt_id, actor, reason, policy=POLICY):
    """Append one immutable resolution for a conflicting capture under the named policy."""
    if policy not in PROOFS:
        raise ValueError('unknown conflict resolution policy')
    if type(alert_id) is not int or alert_id <= 0:
        raise ValueError('a positive conflict alert ID is required')
    if not isinstance(actor, str) or not actor.strip() or not isinstance(reason, str) or not reason.strip():
        raise ValueError('ordering resolution requires an actor and reason')
    with database.transaction() as cursor:
        lock(cursor, 'observation-stream')
        cursor.execute('SELECT * FROM observation_conflict_resolutions WHERE alert_id=%s', (alert_id,))
        previous = cursor.fetchone()
        if previous:
            if (str(previous['conflicting_attempt_id']), previous['actor'], previous['reason'], previous['policy']) != (str(conflicting_attempt_id), actor.strip(), reason.strip(), policy):
                raise ProtocolDataError('conflict already has a different immutable resolution')
            return dict(previous)
        cursor.execute('SELECT * FROM observation_alerts WHERE id=%s', (alert_id,))
        alert = cursor.fetchone()
        cursor.execute('''SELECT b.*,a.manifest FROM observed_blocks b
            JOIN capture_attempts a ON a.id=b.attempt_id WHERE b.height=%s''', (alert['height'] if alert else None,))
        block = cursor.fetchone()
        cursor.execute('SELECT * FROM capture_attempts WHERE id=%s', (conflicting_attempt_id,))
        attempt = cursor.fetchone()
        if (not alert or not block or not attempt or alert['kind'] != 'conflicting-block'
                or alert['details'] != {'block_id': block['id'], 'collector': attempt['collector']}
                or attempt['block_id'] != block['id'] or attempt['height'] != block['height']
                or attempt['error'] != 'conflicting block identity or accounting evidence at a captured height'):
            raise ProtocolDataError('alert is not the matching same-block capture conflict')
        cursor.execute('''SELECT 1 FROM observation_alerts WHERE kind='incomplete-capture' AND height=%s
            AND details->>'attempt_id'=%s AND details->>'error'=%s''',
            (block['height'], str(conflicting_attempt_id), attempt['error']))
        if not cursor.fetchone():
            raise ProtocolDataError('conflicting capture provenance is missing')
        proof = PROOFS[policy](_unpack(cursor, block['manifest']), _unpack(cursor, attempt['manifest']), block['semantic_digest'])
        cursor.execute('''INSERT INTO observation_conflict_resolutions
            (alert_id,block_id,original_attempt_id,conflicting_attempt_id,policy,actor,reason,proof)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *''',
            (alert_id, block['id'], block['attempt_id'], conflicting_attempt_id, policy, actor.strip(), reason.strip(), Json(proof)))
        result = dict(cursor.fetchone())
        cursor.execute("SELECT contiguous_height FROM observation_stream WHERE name='tig'")
        BlockStore(database)._advance(cursor, cursor.fetchone()['contiguous_height'])
    return result
