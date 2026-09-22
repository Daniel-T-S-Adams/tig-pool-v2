"""Explicit opening of a fresh account's observed, nonwithdrawable testnet credit.

This is a setup operation, never automatic balancing of unexplained money.
It accepts only TIG's documented 10 TIG testnet grant, before any pool work or
protocol funding. No API credential, custody transaction or submission is used.
"""

from . import benchmarks, funding, ledger, members
from .database import lock
from .money import Conflict, FundsError, TIG


API_ORIGIN = 'https://testnet-api.tig.foundation'
CHAIN_ID = 84532
TOKEN = '0x3366feee9bbe5b830df9e1fa743828732b13959a'
AMOUNT = 10 * TIG
SOURCE = 'tig-testnet-starter'


def initialize(database, capture_id, *, player_id, actor):
    """Post the observed opening credit once; caller has setup/operator authority."""
    player_id = members.address(player_id)
    if not isinstance(actor, str) or not actor.strip() or len(actor) > 200:
        raise FundsError('starter-credit setup requires a named operator')
    with database.transaction() as cursor:
        # Match custody/top-up lock order; reservations share the budget lock.
        lock(cursor, 'custody-identity')
        lock(cursor, 'operator:protocol-budget')
        lock(cursor, 'protocol-funding')
        cursor.execute("SELECT * FROM protocol_opening_credits WHERE source=%s", (SOURCE,))
        previous = cursor.fetchone()
        if previous:
            if previous['player_id'] != player_id:
                raise Conflict('starter credit already belongs to a different player')
            # Lost-response recovery returns the original record, even after
            # fees have been consumed or its original observation has aged.
            return dict(previous)

        data = funding.read_capture(database, capture_id)
        snapshot = funding.verify(data)
        if snapshot['player_id'] != player_id:
            raise Conflict('starter-credit evidence belongs to another player')
        if data.get('api_origin') != API_ORIGIN:
            raise FundsError('starter credit requires an explicit official testnet capture')
        for end in ('start', 'end'):
            token = data[end]['block']['config'].get('erc20', {})
            if (token.get('chain_id'), token.get('token_address')) != (hex(CHAIN_ID), TOKEN):
                raise FundsError('starter-credit capture has a different chain or token')
        if snapshot['available'] != AMOUNT or data['player_data']['topups']:
            raise FundsError('starter credit requires exactly 10 TIG and no top-up history')

        cursor.execute("SELECT * FROM custody_identity WHERE name='custody'")
        custody = cursor.fetchone()
        if not custody or (custody['chain_id'], custody['token'], custody['wallet'], custody['decimals']) != (
                CHAIN_ID, TOKEN, player_id, 18):
            raise Conflict('initialize the matching testnet custody identity before starter credit')
        current = funding.status(database, cursor=cursor)
        latest = current.get('observation')
        if (not current.get('initialized') or current['player_id'] != player_id or current['conflicts']
                or not latest or latest['id'] != capture_id or not latest['complete']
                or not -5 <= latest['age'] <= 120):
            raise Conflict('starter credit requires the latest fresh, complete funding observation')
        cursor.execute("""SELECT 1 FROM reservations
            UNION ALL SELECT 1 FROM protocol_topups
            UNION ALL SELECT 1 FROM protocol_topup_facts
            UNION ALL SELECT 1 FROM entries e JOIN accounts a ON a.id=e.account_id
                WHERE a.location='protocol'
            LIMIT 1""")
        if cursor.fetchone() or funding.balance(cursor) != 0:
            raise Conflict('starter credit must be initialized before any pool protocol activity')

        journal = ledger.post(cursor, 'protocol-opening:' + SOURCE, 'operator_testnet_starter_credit',
            [('external:protocol:TIG', -AMOUNT), (benchmarks.OPERATOR_FEES, AMOUNT)],
            {'source': SOURCE, 'capture_id': capture_id, 'player_id': player_id,
             'api_origin': API_ORIGIN, 'chain_id': CHAIN_ID, 'token': TOKEN, 'actor': actor})
        cursor.execute("""INSERT INTO protocol_opening_credits
            (source,player_id,capture_id,amount,journal_id,actor)
            VALUES (%s,%s,%s,%s,%s,%s) RETURNING *""",
            (SOURCE, player_id, capture_id, AMOUNT, journal, actor))
        return dict(cursor.fetchone())
