"""Opt-in, cumulative limits for a two-member CPU testnet pilot.

The fixed two-attempt limit is derived from immutable potentially-sent events,
so restarts, rejected requests, refunds and operator resume cannot reset it.
Later operator-reviewed phases extend cumulative limits without resetting events.
All authorization checks run under the normal protocol-budget transaction lock.
No result/proof or monetary recovery path consumes another attempt.
"""

from psycopg2.extras import Json

from . import funding, members, starter_credit
from .database import lock
from .money import Conflict, FundsError, TIG


FIELDS = {'version', 'api_origin', 'chain_id', 'token', 'pool_wallet',
          'maximum_total_tig_units', 'max_fee_per_attempt_units', 'members'}


def validate(config):
    if not isinstance(config, dict) or set(config) != FIELDS:
        raise FundsError('pilot configuration has missing or unknown fields')
    if (type(config['version']) is not int or config['version'] != 1
            or type(config['chain_id']) is not int
            or (config['api_origin'], config['chain_id'], config['token']) != (
                starter_credit.API_ORIGIN, starter_credit.CHAIN_ID, starter_credit.TOKEN)):
        raise FundsError('CPU pilot requires the explicit TIG testnet chain, token and API')
    wallet = members.address(config['pool_wallet'])
    total = funding.amount(config['maximum_total_tig_units'])
    fee = funding.amount(config['max_fee_per_attempt_units'])
    if not 0 < total <= 5*TIG or not 0 < fee <= total:
        raise FundsError('pilot maximum must be at most 5 TIG with a positive fee allowance')
    group = config['members']
    if not isinstance(group, list) or len(group) != 2:
        raise FundsError('pilot requires exactly two members, in execution order')
    normalized = []
    for member in group:
        if not isinstance(member, dict) or set(member) != {'wallet', 'funding_units'}:
            raise FundsError('each pilot member requires a wallet and exact funding allowance')
        amount = funding.amount(member['funding_units'])
        if amount <= 0: raise FundsError('pilot members require positive funding')
        normalized.append({'wallet': members.address(member['wallet']), 'funding_units': str(amount)})
    if len({wallet, *(member['wallet'] for member in normalized)}) != 3:
        raise FundsError('pilot pool and member wallets must be distinct')
    if sum(int(member['funding_units']) for member in normalized) + 2*fee > total:
        raise FundsError('combined member funding and both fee allowances exceed the pilot budget')
    return {**config, 'pool_wallet': wallet, 'maximum_total_tig_units': str(total),
            'max_fee_per_attempt_units': str(fee), 'members': normalized}


def policy(cursor):
    cursor.execute("SELECT config FROM pilot_limits WHERE name='testnet-cpu'")
    row = cursor.fetchone()
    return row['config'] if row else None


def phase(cursor, config):
    cursor.execute('SELECT * FROM pilot_phases ORDER BY number DESC LIMIT 1')
    row = cursor.fetchone()
    if row:
        return dict(row)
    return {'number': 0, 'config': {'version': 2, 'phase_key': 'initial-cpu-pilot', 'attempt_limit': 2,
        'members': [{**member, 'attempt_limit': 1} for member in config['members']]}}


def extend(database, value, *, expected_phase, actor, reason):
    """Append a reviewed phase while paused; original network/budget stay fixed."""
    from . import controls
    if (type(expected_phase) is not int or expected_phase < 0
            or not isinstance(actor, str) or not 1 <= len(actor.strip()) <= 200
            or not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 1000):
        raise FundsError('phase extension requires an expected revision, operator and reason')
    if (not isinstance(value, dict) or set(value) != {'version', 'phase_key', 'attempt_limit', 'members'}
            or type(value['version']) is not int or value['version'] != 2
            or not isinstance(value['phase_key'], str) or not 1 <= len(value['phase_key'].strip()) <= 128
            or type(value['attempt_limit']) is not int or value['attempt_limit'] < 2
            or not isinstance(value['members'], list) or len(value['members']) != 2):
        raise FundsError('invalid cumulative pilot phase')
    normalized = []
    for item in value['members']:
        if (not isinstance(item, dict) or set(item) != {'wallet', 'funding_units', 'attempt_limit'}
                or type(item['attempt_limit']) is not int or item['attempt_limit'] < 1):
            raise FundsError('each phase member needs a cumulative funding and attempt limit')
        amount = funding.amount(item['funding_units'])
        if amount <= 0: raise FundsError('member funding allowance must be positive')
        normalized.append({**item, 'wallet': members.address(item['wallet']), 'funding_units': str(amount)})
    value = {**value, 'members': normalized}
    if sum(item['attempt_limit'] for item in normalized) != value['attempt_limit']:
        raise FundsError('member attempt allowances must sum to the total attempt limit')
    with database.transaction() as cursor:
        lock(cursor, 'custody-identity')
        lock(cursor, 'operator:protocol-budget')
        config = policy(cursor)
        if not config: raise Conflict('initial pilot policy must exist before extending it')
        cursor.execute('SELECT * FROM pilot_phases WHERE phase_key=%s', (value['phase_key'],))
        old = cursor.fetchone()
        if old:
            if (old['config'], old['previous_number'], old['actor'], old['reason']) != (value, expected_phase, actor, reason):
                raise Conflict('phase key was reused with different inputs')
            return dict(old)
        current = phase(cursor, config)
        if current['number'] != expected_phase:
            raise Conflict('pilot phase changed; review the current cumulative allowances')
        if not controls.paused(database, cursor=cursor):
            raise Conflict('pause new work before reviewing a later pilot phase')
        cursor.execute("SELECT 1 FROM reservations WHERE state IN ('reserved','uncertain','accepted') LIMIT 1")
        if cursor.fetchone(): raise Conflict('finish or reconcile in-flight pilot work before extending')
        for before, after in zip(current['config']['members'], normalized):
            if before['wallet'] != after['wallet']:
                raise Conflict('later phases must retain both original member identities and order')
            if (int(after['funding_units']) < int(before['funding_units'])
                    or after['attempt_limit'] < before['attempt_limit']):
                raise Conflict('cumulative phase allowances cannot decrease or reset')
        if value['members'] == current['config']['members']:
            raise Conflict('phase does not change the existing allowances')
        planned = sum(int(item['funding_units']) for item in normalized)
        if planned + value['attempt_limit'] * int(config['max_fee_per_attempt_units']) > int(config['maximum_total_tig_units']):
            raise FundsError('cumulative funding and all fee ceilings exceed the original pilot budget')
        if attributed_receipts(cursor, config) > planned:
            raise Conflict('existing attributed receipts exceed the proposed phase allocation')
        cursor.execute("SELECT count(*) AS count FROM reservation_events WHERE kind='potentially_sent'")
        used = cursor.fetchone()['count']
        cursor.execute('''INSERT INTO pilot_phases(number,phase_key,config,previous_number,attempts_before,actor,reason)
            VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *''',
            (expected_phase+1, value['phase_key'], Json(value), expected_phase, used, actor, reason))
        return dict(cursor.fetchone())


def initialize(database, config, *, actor):
    config = validate(config)
    if not isinstance(actor, str) or not actor.strip() or len(actor) > 200:
        raise FundsError('pilot limits require a named setup operator')
    with database.transaction() as cursor:
        lock(cursor, 'custody-identity')
        lock(cursor, 'operator:protocol-budget')
        previous = policy(cursor)
        if previous:
            if previous != config: raise Conflict('recorded pilot limits cannot be changed or reset')
            return previous
        cursor.execute('SELECT 1 FROM reservations UNION ALL SELECT 1 FROM protocol_topups LIMIT 1')
        if cursor.fetchone(): raise Conflict('pilot limits must be installed before any work or top-up')
        cursor.execute("SELECT * FROM custody_identity WHERE name='custody'")
        identity = cursor.fetchone()
        if not identity or (identity['chain_id'], identity['token'], identity['wallet'], identity['decimals']) != (
                config['chain_id'], config['token'], config['pool_wallet'], 18):
            raise Conflict('pilot requires the matching initialized testnet custody identity')
        cursor.execute("INSERT INTO pilot_limits(name,config,actor) VALUES ('testnet-cpu',%s,%s)", (Json(config), actor))
        return config


def require_service(database, *, api_origin, player_id, required=False):
    """Check before constructing an authenticated transport or enabling work."""
    with database.transaction() as cursor:
        config = policy(cursor)
    if required and not config:
        raise Conflict('deployment requires installed pilot limits')
    if config and (api_origin, members.address(player_id)) != (config['api_origin'], config['pool_wallet']):
        raise Conflict('pilot database cannot use a different submission network or player')
    return config


def attributed_receipts(cursor, config):
    # Unattributed custody receipts are held in a separate, non-spendable ledger
    # account. Receiving a grant there does not allocate it to this pilot. If it
    # is later attributed to a member/operator, count the full original receipt;
    # withdrawals, collateral releases and restarts cannot recycle the allowance.
    cursor.execute('''SELECT coalesce(sum(t.amount),0) AS incoming FROM transfers t
        JOIN transfer_attributions a ON a.event_id=t.event_id
        WHERE t.recipient=%s AND t.sender<>t.recipient''', (config['pool_wallet'],))
    return int(cursor.fetchone()['incoming'])


def check(database, cursor, *, member, resource, amount, fee_limit, payload, reservation_id=None):
    """Caller holds protocol-budget; exclude only this send's own reservation."""
    config = policy(cursor)
    if not config: return
    current = phase(cursor, config)
    allowances = current['config']
    from . import chain_observer
    if not chain_observer.status(database, cursor=cursor)['ready'] or not funding.status(database, cursor=cursor)['ready']:
        raise Conflict('pilot requires fresh reconciled custody and fee observers')
    if resource != 'CPU' or payload.get('settings', {}).get('player_id') != config['pool_wallet']:
        raise Conflict('pilot permits only CPU work for its configured pool account')
    if not 0 <= fee_limit <= int(config['max_fee_per_attempt_units']):
        raise Conflict('current submission fee exceeds the pilot allowance')
    cursor.execute("""SELECT r.*,m.wallet FROM reservation_events e
        JOIN reservations r ON r.id=e.reservation_id JOIN members m ON m.id=r.member_id
        WHERE e.kind='potentially_sent' ORDER BY e.created_at,e.event_key""")
    sent = cursor.fetchall()
    if len(sent) >= allowances['attempt_limit']:
        raise Conflict('pilot has used both precommit attempts' if current['number'] == 0
                       else 'pilot has used its cumulative precommit attempt limit')
    if any(row['state'] != 'active' for row in sent):
        raise Conflict('pilot waits for confirmed activation; failure or uncertainty stops further work')
    if current['number'] == 0:
        expected = allowances['members'][len(sent)]
        if member['wallet'] != expected['wallet'] or any(row['wallet'] == member['wallet'] for row in sent):
            raise Conflict('pilot permits one attempt per member in the recorded order')
    else:
        expected = next((item for item in allowances['members'] if item['wallet'] == member['wallet']), None)
        if expected is None or sum(row['wallet'] == member['wallet'] for row in sent) >= expected['attempt_limit']:
            raise Conflict('member has used its cumulative pilot attempt limit')
    if not 0 < amount <= int(expected['funding_units']):
        raise Conflict('pilot requires positive collateral within the member funding allowance')
    cursor.execute("SELECT id FROM reservations WHERE state='reserved'")
    if any(str(row['id']) != str(reservation_id) for row in cursor.fetchall()):
        raise Conflict('pilot permits only one outstanding work reservation')
    # Charge the full frozen fee ceilings even after a rejected request or a
    # refund. A restart cannot recycle this authorization into further work.
    planned = sum(int(item['funding_units']) for item in allowances['members'])
    if planned + sum(int(row['fee_limit']) for row in sent) + fee_limit > int(config['maximum_total_tig_units']):
        raise Conflict('combined pilot funding and submission fees exceed the total budget')
    if attributed_receipts(cursor, config) > planned:
        raise Conflict('pilot attributed custody receipts exceed the recorded funding allocation')


def prohibit_topup(cursor):
    if policy(cursor):
        raise Conflict('pilot uses starter fee credit; additional token top-ups are disabled')


def status(database):
    with database.transaction() as cursor:
        config = policy(cursor)
        if not config: return {'configured': False}
        current = phase(cursor, config)
        cursor.execute("""SELECT count(*) AS attempts,coalesce(sum(r.fee_limit),0) AS fee_ceiling
            FROM reservation_events e JOIN reservations r ON r.id=e.reservation_id
            WHERE e.kind='potentially_sent'""")
        row = cursor.fetchone()
        incoming = attributed_receipts(cursor, config)
        cursor.execute("SELECT balance FROM accounts WHERE id='unattributed:TIG'")
        unallocated = int(cursor.fetchone()['balance'])
        planned = sum(int(item['funding_units']) for item in current['config']['members'])
        return {'configured': True, 'config': config, 'attempts_used': row['attempts'],
                'phase_number': current['number'], 'phase': current['config'],
                'attempts_limit': current['config']['attempt_limit'], 'committed_fee_units': str(row['fee_ceiling']),
                'member_allocation_units': str(planned),
                'attributed_custody_receipt_units': str(incoming),
                'unattributed_custody_units': str(unallocated),
                'custody_receipts_within_allocation': incoming <= planned}
