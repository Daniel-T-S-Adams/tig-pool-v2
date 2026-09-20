"""Collateral finalization and funded, exact round reward allocation."""

from psycopg2.extras import Json

from . import benchmarks,deposits,ledger,members,qualifiers,reports
from .block_observer import BlockStore
from .database import lock
from .money import Conflict,FundsError,allocate_rewards,units


RULE='round-credit-five-percent-v1'


def pot(round_number):
    return 'round:'+str(round_number)


def finalize_collateral(database,reservation_id,seal_id):
    """Finalize one known outcome independently of reward-credit coverage."""
    with database.transaction() as cursor:
        lock(cursor,'reports-store')
        cursor.execute('SELECT creation_round FROM reservations WHERE id=%s',(reservation_id,))
        initial=cursor.fetchone()
        if not initial:raise FundsError('unknown collateral reservation')
        round_number=initial['creation_round']
        lock(cursor,'round:'+str(round_number));lock(cursor,'observation-stream')
        row=benchmarks._locked(cursor,reservation_id)
        cursor.execute('SELECT * FROM collateral_finalizations WHERE reservation_id=%s',(reservation_id,))
        previous=cursor.fetchone()
        if previous:return dict(previous)
        if row['collateral_outcome'] is not None:
            return {'reservation_id':row['id'],'outcome':row['collateral_outcome'],'amount':row['amount'],'previously_finalized':True}
        seal=reports.require_seal(cursor,seal_id,round_number)
        if row['state'] not in ('active','verification_failed','expired') or row['slot_held']:
            raise Conflict('a definitive benchmark outcome is still missing')
        related=[value for value in seal['reports'].values() if value['benchmark_id']==row['benchmark_id']]
        for report in related:
            if report['result'] is None:raise Conflict('this benchmark still has unresolved arbitration')
            try:
                if report['benchmarker']!=row['assignment']['settings']['player_id'] or report['nonce']>=row['assignment']['num_nonces']:
                    raise Conflict('report ownership or nonce differs from the stored assignment')
            except (KeyError,TypeError) as error:
                raise Conflict('complete benchmark metadata is needed to reconcile this report') from error
        if row['handed_over_at'] is None:
            if row['state']!='expired':raise Conflict('unhanded work requires reconciled definitive expiry')
            outcome='returned'
        else:
            outcome='forfeited' if row['active_at_height'] is None or any(value['result']=='nonreproducible' for value in related) else 'returned'
        amount=int(row['amount'])
        destination=members.available(row['member_id']) if outcome=='returned' else ledger.account(cursor,pot(round_number),'round')
        journal_id=None
        if amount:
            journal_id=ledger.post(cursor,f'collateral:{reservation_id}:finalize','collateral_'+outcome,
                [(benchmarks.held(reservation_id),-amount),(destination,amount)],
                {'reservation_id':str(reservation_id),'creation_round':round_number,'report_seal_id':str(seal_id),'outcome':outcome})
        cursor.execute('UPDATE reservations SET collateral_outcome=%s WHERE id=%s',(outcome,reservation_id))
        benchmarks.event(cursor,reservation_id,'collateral_finalized',{'outcome':outcome,'amount':amount,'report_seal_id':str(seal_id)})
        cursor.execute('''INSERT INTO collateral_finalizations(reservation_id,outcome,amount,report_seal_id,journal_id)
            VALUES (%s,%s,%s,%s,%s) RETURNING *''',(reservation_id,outcome,amount,seal_id,journal_id))
        return dict(cursor.fetchone())


def declare_earnings(database,round_number,*,expected_received_net,withheld_operating_cost,block_id,evidence):
    """Trusted final-emissions adapter only. Estimates never create funds."""
    units(round_number,positive=True);units(expected_received_net);units(withheld_operating_cost)
    if not evidence:raise FundsError('final round earnings require protocol attribution evidence')
    reports.deadline(database,round_number,block_id)
    with database.transaction() as cursor:
        lock(cursor,'round:'+str(round_number));lock(cursor,'observation-stream')
        reports.require_block(cursor,block_id)
        cursor.execute('SELECT * FROM round_earnings WHERE round=%s',(round_number,))
        previous=cursor.fetchone()
        if previous:
            if (int(previous['expected_received_net']),int(previous['withheld_operating_cost']),previous['evidence'])!=(expected_received_net,withheld_operating_cost,evidence):
                raise Conflict('final round earnings are already frozen')
            return dict(previous)
        ledger.account(cursor,pot(round_number),'round')
        cursor.execute('''INSERT INTO round_earnings(round,expected_received_net,withheld_operating_cost,block_id,evidence)
            VALUES (%s,%s,%s,%s,%s) RETURNING *''',(round_number,expected_received_net,withheld_operating_cost,block_id,Json(evidence)))
        return dict(cursor.fetchone())


def attribute_receipt(database,round_number,transfer,*,evidence):
    """Move a verified, unmatched incoming reward event into its frozen round."""
    units(round_number,positive=True);units(transfer.amount,positive=True)
    if not evidence:raise FundsError('reward receipt requires verified protocol-round attribution')
    if transfer.recipient!=transfer.network.custody or transfer.sender==transfer.network.custody:
        raise FundsError('reward receipt must be incoming to configured custody')
    with database.transaction() as cursor:
        lock(cursor,'round:'+str(round_number));lock(cursor,'transfer:'+transfer.event_id)
        cursor.execute('SELECT * FROM round_receipts WHERE event_id=%s',(transfer.event_id,))
        previous=cursor.fetchone()
        if previous:
            if previous['round']!=round_number or int(previous['amount'])!=transfer.amount:
                raise Conflict('reward receipt already belongs to a different round')
            deposits.save_transfer(cursor,transfer)
            return dict(previous)
        cursor.execute('SELECT * FROM round_earnings WHERE round=%s',(round_number,))
        earnings=cursor.fetchone()
        if not earnings:raise Conflict('final earnings must be attributed before their receipts')
        cursor.execute('SELECT 1 FROM round_settlements WHERE round=%s',(round_number,))
        if cursor.fetchone():raise Conflict('round has already settled')
        cursor.execute('SELECT 1 FROM transfers WHERE event_id=%s',(transfer.event_id,))
        if not cursor.fetchone():raise Conflict('receipt must first be observed by the confirmed transfer indexer')
        deposits.save_transfer(cursor,transfer)
        cursor.execute('SELECT 1 FROM transfer_attributions WHERE event_id=%s',(transfer.event_id,))
        if cursor.fetchone():raise Conflict('receipt is already assigned; deposits cannot also become round rewards')
        cursor.execute('SELECT coalesce(sum(amount),0) AS amount FROM round_receipts WHERE round=%s',(round_number,))
        total=int(cursor.fetchone()['amount'])+transfer.amount
        if total>int(earnings['expected_received_net']):raise Conflict('receipts exceed the declared final net round earnings')
        deposits._attribute(cursor,transfer,pot(round_number),'protocol-reward-adapter',evidence)
        cursor.execute('INSERT INTO round_receipts(event_id,round,amount,evidence) VALUES (%s,%s,%s,%s) RETURNING *',
                       (transfer.event_id,round_number,transfer.amount,Json(evidence)))
        return dict(cursor.fetchone())


def reimburse_operating_cost(database,round_number):
    """Use actual available operator funds, never a promised reimbursement."""
    with database.transaction() as cursor:
        lock(cursor,'round:'+str(round_number))
        cursor.execute('SELECT * FROM round_reimbursements WHERE round=%s',(round_number,))
        previous=cursor.fetchone()
        if previous:return dict(previous)
        cursor.execute('SELECT withheld_operating_cost FROM round_earnings WHERE round=%s',(round_number,))
        earnings=cursor.fetchone()
        if not earnings:raise Conflict('final round operating-charge classification is missing')
        cursor.execute('SELECT 1 FROM round_settlements WHERE round=%s',(round_number,))
        if cursor.fetchone():raise Conflict('round has already settled')
        amount=int(earnings['withheld_operating_cost'])
        journal_id=None
        if amount:
            journal_id=ledger.post(cursor,f'round:{round_number}:reimburse','operator_cost_reimbursement',
                [('operator:custody:TIG',-amount),(pot(round_number),amount)],{'round':round_number,'withheld_operating_cost':amount})
        cursor.execute('INSERT INTO round_reimbursements(round,amount,journal_id) VALUES (%s,%s,%s) RETURNING *',(round_number,amount,journal_id))
        return dict(cursor.fetchone())


def preview(database,round_number,seal_id):
    """Calculate ready, fully funded allocations without posting or reserving funds."""
    return _settle(database,round_number,seal_id,post=False)


def allocate(database,round_number,seal_id):
    """Credit one complete reward round, even if no benchmark was created in it."""
    return _settle(database,round_number,seal_id,post=True)


def _settle(database,round_number,seal_id,*,post):
    units(round_number,positive=True)
    with database.transaction() as cursor:
        cursor.execute('SELECT * FROM round_settlements WHERE round=%s',(round_number,))
        previous=cursor.fetchone()
        if previous:return dict(previous)
    credits=qualifiers.round_credits(database,round_number)
    with database.transaction() as cursor:
        lock(cursor,'reports-store');lock(cursor,'round:'+str(round_number));lock(cursor,'observation-stream')
        cursor.execute('SELECT * FROM round_settlements WHERE round=%s',(round_number,))
        previous=cursor.fetchone()
        if previous:return dict(previous)
        reports.require_seal(cursor,seal_id,round_number)
        # Recheck coverage while the observer cannot add a conflicting block.
        if not BlockStore(database).round_coverage(round_number):
            raise Conflict('reward-round credit coverage is incomplete')
        cursor.execute('SELECT * FROM round_earnings WHERE round=%s',(round_number,))
        earnings=cursor.fetchone()
        if not earnings:raise Conflict('final net earnings are not established')
        reports.require_block(cursor,earnings['block_id'])
        cursor.execute('SELECT id FROM reservations WHERE creation_round=%s AND collateral_outcome IS NULL',(round_number,))
        if cursor.fetchone():raise Conflict('creation-round collateral outcomes are still unresolved')
        cursor.execute('SELECT event_id,amount FROM round_receipts WHERE round=%s ORDER BY event_id',(round_number,))
        receipts=cursor.fetchall()
        received=sum(int(row['amount']) for row in receipts)
        if received!=int(earnings['expected_received_net']):raise Conflict('final reward receipts have not all been received')
        cursor.execute('SELECT amount FROM round_reimbursements WHERE round=%s',(round_number,))
        reimbursement=cursor.fetchone()
        reimbursed=int(reimbursement['amount']) if reimbursement else 0
        if reimbursed!=int(earnings['withheld_operating_cost']):raise Conflict('operator funding has not replaced withheld operating charges')
        cursor.execute('''SELECT f.reservation_id,f.amount FROM collateral_finalizations f JOIN reservations r ON r.id=f.reservation_id
            WHERE r.creation_round=%s AND f.outcome='forfeited' ORDER BY f.reservation_id''',(round_number,))
        forfeitures=cursor.fetchall()
        amount=received+reimbursed+sum(int(row['amount']) for row in forfeitures)
        cursor.execute('SELECT balance FROM accounts WHERE id=%s FOR UPDATE',(pot(round_number),))
        if int(cursor.fetchone()['balance'])!=amount:raise Conflict('round account differs from its complete funding inputs')
        operator,allocations=allocate_rewards(amount,credits)
        for member_id in sorted(allocations):members.member_lock(cursor,member_id)
        inputs={'round':round_number,'rule':RULE,'report_seal_id':str(seal_id),
            'credits':{key:[value.numerator,value.denominator] for key,value in sorted(credits.items())},
            'receipts':{row['event_id']:int(row['amount']) for row in receipts},'cost_reimbursement':reimbursed,
            'forfeitures':{str(row['reservation_id']):int(row['amount']) for row in forfeitures},
            'expected_received_net':int(earnings['expected_received_net'])}
        if not post:
            return {'round':round_number,'rule':RULE,'input_digest':ledger.fingerprint(inputs),
                'inputs':inputs,'pot':amount,'operator_allocation':operator,
                'member_allocations':allocations,'preview':True}
        journal_id=None
        if amount:
            journal_id=ledger.post(cursor,f'round:{round_number}:settle','round_rewards',
                [(pot(round_number),-amount),('operator:custody:TIG',operator)]+
                [(members.available(key),value) for key,value in sorted(allocations.items())],inputs)
        cursor.execute('''INSERT INTO round_settlements(round,rule,input_digest,inputs,pot,operator_allocation,member_allocations,journal_id)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *''',
            (round_number,RULE,ledger.fingerprint(inputs),Json(inputs),amount,operator,Json(allocations),journal_id))
        return dict(cursor.fetchone())
