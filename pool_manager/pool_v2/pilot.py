"""Opt-in, durable limits for the initial two-member CPU testnet pilot.

The fixed two-attempt limit is derived from immutable potentially-sent events,
so restarts, rejected requests, refunds and operator resume cannot reset it.
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
    if len(sent) >= 2: raise Conflict('pilot has used both precommit attempts')
    if any(row['state'] != 'active' for row in sent):
        raise Conflict('pilot waits for confirmed activation; failure or uncertainty stops further work')
    expected = config['members'][len(sent)]
    if member['wallet'] != expected['wallet'] or any(row['wallet'] == member['wallet'] for row in sent):
        raise Conflict('pilot permits one attempt per member in the recorded order')
    if not 0 < amount <= int(expected['funding_units']):
        raise Conflict('pilot requires positive collateral within the member funding allowance')
    cursor.execute("SELECT id FROM reservations WHERE state='reserved'")
    if any(str(row['id']) != str(reservation_id) for row in cursor.fetchall()):
        raise Conflict('pilot permits only one outstanding work reservation')
    # Charge the full frozen fee ceilings even after a rejected request or a
    # refund. A restart cannot recycle this authorization into further work.
    planned = sum(int(item['funding_units']) for item in config['members'])
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
        cursor.execute("""SELECT count(*) AS attempts,coalesce(sum(r.fee_limit),0) AS fee_ceiling
            FROM reservation_events e JOIN reservations r ON r.id=e.reservation_id
            WHERE e.kind='potentially_sent'""")
        row = cursor.fetchone()
        incoming = attributed_receipts(cursor, config)
        cursor.execute("SELECT balance FROM accounts WHERE id='unattributed:TIG'")
        unallocated = int(cursor.fetchone()['balance'])
        planned = sum(int(item['funding_units']) for item in config['members'])
        return {'configured': True, 'config': config, 'attempts_used': row['attempts'],
                'attempts_limit': 2, 'committed_fee_units': str(row['fee_ceiling']),
                'member_allocation_units': str(planned),
                'attributed_custody_receipt_units': str(incoming),
                'unattributed_custody_units': str(unallocated),
                'custody_receipts_within_allocation': incoming <= planned}
