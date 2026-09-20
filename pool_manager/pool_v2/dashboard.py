"""Read-only member/operator projections from the enforcement ledger."""

from collections import defaultdict
from fractions import Fraction

from . import members
from .block_observer import BlockStore
from .money import FundsError


def page(limit,offset):
    if type(limit) is not int or not 1<=limit<=200 or type(offset) is not int or offset<0:
        raise FundsError('invalid page size or offset')


def member(database,member_id,*,limit=50,offset=0):
    page(limit,offset)
    balance=members.balances(database,member_id)
    for field in ('available','collateral','pending_withdrawals'):balance[field]=str(balance[field])
    with database.transaction() as cursor:
        cursor.execute('''SELECT id,benchmark_id,creation_round,resource,state,slot_held,base_amount,multiplier,
            multiplier_revision,amount,collateral_outcome,handed_over_at,created_at,
            CASE WHEN collateral_outcome IS NULL THEN amount ELSE 0 END AS held
            FROM reservations WHERE member_id=%s ORDER BY created_at DESC,id DESC LIMIT %s OFFSET %s''',
            (member_id,limit,offset))
        assignments=cursor.fetchall()
        cursor.execute('''SELECT b.round,c.denominator,sum(c.numerator) AS numerator FROM benchmark_credits c
            JOIN observed_blocks b ON b.id=c.block_id WHERE c.member_id=%s
            GROUP BY b.round,c.denominator ORDER BY b.round DESC''',(member_id,))
        credit=defaultdict(Fraction)
        for row in cursor.fetchall():credit[row['round']]+=Fraction(int(row['numerator']),int(row['denominator']))
        cursor.execute('''SELECT round,created_at,member_allocations->>%s AS allocation FROM round_settlements
            WHERE member_allocations ? %s ORDER BY round DESC''',(str(member_id),str(member_id)))
        settled={row['round']:dict(row) for row in cursor.fetchall()}
        rounds=[]
        for number in sorted(set(credit)|set(settled),reverse=True)[:100]:
            value=credit[number]
            rounds.append({'round':number,'credit_numerator':str(value.numerator),'credit_denominator':str(value.denominator),
                'allocation':settled.get(number,{}).get('allocation'),'settled_at':settled.get(number,{}).get('created_at')})
        cursor.execute('SELECT * FROM withdrawals WHERE member_id=%s ORDER BY created_at DESC,id DESC LIMIT %s OFFSET %s',
                       (member_id,limit,offset))
        return {'balance':balance,'assignments':assignments,'rounds':rounds,'withdrawals':cursor.fetchall(),
                'limit':limit,'offset':offset}


def operator(database,*,limit=50,offset=0):
    page(limit,offset)
    status=BlockStore(database).status()
    with database.transaction() as cursor:
        cursor.execute('''SELECT m.id,m.wallet,m.withdrawal_wallet,m.multiplier,m.multiplier_revision,a.balance AS available,
            (SELECT coalesce(sum(amount),0) FROM reservations r WHERE r.member_id=m.id AND r.collateral_outcome IS NULL) AS collateral,
            (SELECT count(*) FROM reservations r WHERE r.member_id=m.id AND r.slot_held) AS slots
            FROM members m JOIN accounts a ON a.id='member:'||m.id||':available'
            ORDER BY m.created_at DESC,m.id DESC LIMIT %s OFFSET %s''',(limit,offset))
        people=cursor.fetchall()
        cursor.execute('''SELECT asset,location,kind,sum(balance) AS balance FROM accounts
            WHERE kind<>'external' GROUP BY asset,location,kind ORDER BY asset,location,kind''')
        balances=cursor.fetchall()
        cursor.execute('''SELECT o.id,o.kind,o.sent_at,r.member_id,r.benchmark_id FROM protocol_outbox o
            JOIN reservations r ON r.id=o.reservation_id WHERE o.state='uncertain' ORDER BY o.created_at LIMIT 100''')
        pending=cursor.fetchall()
        cursor.execute('SELECT height,kind,details,created_at FROM observation_alerts ORDER BY created_at DESC LIMIT 50')
        alerts=cursor.fetchall()
        cursor.execute('''SELECT numbers.round,e.expected_received_net,e.withheld_operating_cost,s.pot,s.operator_allocation,s.created_at AS settled_at,
            (SELECT count(*) FROM reservations r WHERE r.creation_round=numbers.round AND r.collateral_outcome IS NULL) AS pending_collateral,
            (SELECT count(*) FROM observed_blocks b WHERE b.round=numbers.round) AS observed_blocks,
            (SELECT count(*) FROM observed_blocks b JOIN credited_blocks c ON c.block_id=b.id WHERE b.round=numbers.round) AS credited_blocks
            FROM (SELECT round FROM observed_blocks UNION SELECT creation_round FROM reservations UNION SELECT round FROM round_earnings) numbers
            LEFT JOIN round_earnings e ON e.round=numbers.round LEFT JOIN round_settlements s ON s.round=numbers.round
            ORDER BY numbers.round DESC LIMIT 100''')
        rounds=cursor.fetchall()
        cursor.execute('''SELECT id,member_id,benchmark_id,creation_round,state,amount,base_amount,multiplier,handed_over_at
            FROM reservations WHERE collateral_outcome IS NULL ORDER BY creation_round,created_at LIMIT %s OFFSET %s''',(limit,offset))
        holds=cursor.fetchall()
        cursor.execute('SELECT * FROM multiplier_changes ORDER BY created_at DESC LIMIT 50')
        changes=cursor.fetchall()
        cursor.execute('SELECT * FROM custody_identity WHERE name=\'custody\'')
        return {'members':people,'balances':balances,'observation':status,'uncertain_submissions':pending,
                'alerts':alerts,'rounds':rounds,'collateral':holds,'multiplier_changes':changes,
                'custody':cursor.fetchone(),'limit':limit,'offset':offset}
