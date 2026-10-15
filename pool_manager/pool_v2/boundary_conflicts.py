"""Resolve a same-block capture that only changed capture format, retaining every alert.

When collectors switch from the compact full-network projection to the
pool-scoped capture, a restarted collector can record the current head a
second time in the new format. Both captures describe the same TIG block,
but their stored identities differ by construction: the scoped capture holds
every player's qualifier counts and no selection references, the compact one
holds the pool's counts and the best network bundles. The proof below shows
that every credit-bearing input agrees, so the later capture is an
alternative projection of the recorded block rather than new evidence.
"""

from dataclasses import asdict
import hashlib

from .block_observer import _semantic, canonical_observation
from .compact import FORMAT
from .observation import canonical_json
from .protocol import ProtocolDataError, SCOPED_FORMAT, equal_bundle_credit, validate_snapshot


POLICY = 'format-boundary-projection-v1'
FEED_KEYS = ('precommits', 'benchmarks', 'proofs', 'frauds')


def _records(rows):
    return sorted(canonical_json(row).decode() for row in rows)


def projection_proof(original, conflicting, source_digest):
    """Compare the compact original with the scoped recapture of the same block.

    Nothing is inferred or recomputed from the network: the block identity,
    configuration, the pool's feed, bundles, qualifier counts, credit, the
    pool's own algorithm counts, algorithm and challenge records, binaries,
    the pool's OPoW row and the active benchmark list must all agree.
    """
    if original.get('format') != FORMAT or conflicting.get('format') != SCOPED_FORMAT:
        raise ProtocolDataError('boundary resolution needs a compact original and a pool-scoped conflicting capture')
    first, second = validate_snapshot(original), validate_snapshot(conflicting)
    if _semantic(first, original) != source_digest:
        raise ProtocolDataError('original capture does not match the immutable block digest')
    player = original['pool_player_id']
    if conflicting.get('pool_player_id') != player:
        raise ProtocolDataError('boundary captures belong to different pools')
    identity = lambda snapshot: (snapshot.block_id, snapshot.previous_block_id, snapshot.height, snapshot.round, snapshot.timestamp)
    if identity(first) != identity(second) or original['end']['block']['id'] != conflicting['end']['block']['id']:
        raise ProtocolDataError('boundary captures have different block identities')
    compared = ['block']

    def same(name, left, right):
        if canonical_json(left) != canonical_json(right):
            raise ProtocolDataError('boundary captures differ in ' + name)
        compared.append(name)

    def feed(observation):
        records = observation['players'].get(player, observation.get('pool_pending'))
        if not isinstance(records, dict):
            raise ProtocolDataError('boundary captures need the complete pool feed')
        return {key: _records(records.get(key, [])) for key in FEED_KEYS}

    def codes(observation):
        rows = {}
        for row in observation['algorithms']['codes']:
            counts = row['block_data']['num_qualifiers_by_track_by_player']
            rows[row['id']] = {'details': row['details'], 'state': row['state'], 'adoption': row['block_data']['adoption'],
                               'pool_counts': {track: players[player] for track, players in counts.items() if player in players}}
        return rows

    def challenges(observation):
        return {row['id']: {'config': row['config'], 'state': row['state']} for row in observation['challenges']['challenges']}

    def opow(observation):
        return _records(row for row in observation['opow']['opow'] if row['player_id'] == player)

    def bundles(snapshot):
        return sorted((asdict(bundle) for bundle in snapshot.bundles), key=lambda row: (row['benchmark_id'], row['active_index']))

    def credit(snapshot):
        return {benchmark + ':' + str(index): str(value) for (benchmark, index), value in sorted(equal_bundle_credit(snapshot).items())}

    same('block_config', canonical_observation(original['start']['block']['config']),
         canonical_observation(conflicting['start']['block']['config']))
    same('block_details', original['start']['block']['details'], conflicting['start']['block']['details'])
    same('pool_feed', feed(original), feed(conflicting))
    same('pool_precommits', first.precommits, second.precommits)
    same('pool_bundles', bundles(first), bundles(second))
    same('pool_qualifiers', sorted((*key, value) for key, value in first.qualifiers.items()),
         sorted((*key, value) for key, value in second.qualifiers.items()))
    same('pool_credit', credit(first), credit(second))
    same('algorithms', codes(original), codes(conflicting))
    same('binaries', _records(original['algorithms'].get('binarys', [])), _records(conflicting['algorithms'].get('binarys', [])))
    same('challenges', challenges(original), challenges(conflicting))
    same('pool_opow', opow(original), opow(conflicting))
    same('pool_active_benchmark_ids', sorted(original['pool_active_benchmark_ids']),
         sorted(conflicting['start']['block']['data']['active_ids']['benchmark']))
    if conflicting['scope']['active_benchmark_count'] != original['start']['block']['details']['num_active']['benchmark']:
        raise ProtocolDataError('boundary captures differ in scope_count')
    compared.append('scope_count')
    sha = lambda value: hashlib.sha256(canonical_json(value)).hexdigest()
    return {'policy': POLICY, 'block_id': first.block_id, 'height': first.height,
            'original_format': FORMAT, 'conflicting_format': SCOPED_FORMAT,
            'original_semantic_digest': source_digest, 'conflicting_semantic_digest': _semantic(second, conflicting),
            'original_payload_sha256': sha(original), 'conflicting_payload_sha256': sha(conflicting),
            'conflicting_scope': dict(conflicting['scope']), 'compared': compared, 'pool_credit_matches': True}
