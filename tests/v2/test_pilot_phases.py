from copy import deepcopy
from dataclasses import replace

import psycopg2

from pool_manager.pool_v2 import controls, deposits, members, pilot, withdrawals
from pool_manager.pool_v2.money import Conflict, FundsError, TIG
from funds_helpers import OTHER, WALLET, transfer
import test_pilot
from test_starter_credit import TESTNET


class PilotPhaseTests(test_pilot.PilotCase):
    def finish_initial(self):
        first=self.reserve(); self.send(first); self.active(first)
        second=self.reserve('second',member=self.other); self.send(second); self.active(second,'b'*32)
        controls.set_pause(self.db,True,actor='operator',reason='review phase',event_key='pause')
        return [first,second]

    def configuration(self,*,attempts=1,funding=TIG+TIG//10,key='funds-test'):
        return {'version':2,'phase_key':key,'attempt_limit':attempts*2,
            'members':[{'wallet':wallet,'funding_units':str(funding),'attempt_limit':attempts}
                       for wallet in (WALLET,OTHER)]}

    def extend(self,value=None,*,expected=0):
        return pilot.extend(self.db,value or self.configuration(),expected_phase=expected,
                            actor='operator',reason='bounded financial validation')

    def test_withdrawal_phase_preserves_original_holds_attempts_and_budget(self):
        original=self.finish_initial()
        value=self.configuration(); first=self.extend(value)
        self.assertEqual(self.extend(value)['number'],first['number'])
        self.assertEqual(pilot.status(self.db)['config'],self.config)
        deposits.receive(self.db,replace(transfer(WALLET,amount=TIG//10),network=TESTNET))
        requested=withdrawals.request(self.db,self.member,'early-withdrawal',TIG//10)
        self.assertEqual(int(requested['amount']),TIG//10)
        self.assertEqual(self.balance()['collateral'],TIG)
        self.assertEqual(self.balance()['available'],0)
        self.assertEqual(self.balance()['pending_withdrawals'],TIG//10)
        state=pilot.status(self.db)
        self.assertEqual((state['attempts_used'],state['attempts_limit'],state['phase_number']),(2,2,1))
        self.assertEqual(state['committed_fee_units'],str(2*TIG//1000))
        for row in original:
            saved=self.row('SELECT amount,multiplier,slot_held FROM reservations WHERE id=%s',(row['id'],))
            self.assertEqual((int(saved['amount']),str(saved['multiplier']),saved['slot_held']),(TIG,'0.02',False))

    def test_later_work_uses_cumulative_member_and_global_limits_after_restart(self):
        self.finish_initial(); self.extend(self.configuration(attempts=2))
        for member,wallet in ((self.member,WALLET),(self.other,OTHER)):
            deposits.receive(self.db,replace(transfer(wallet,amount=TIG//10),network=TESTNET))
            members.set_multiplier(self.db,member,'0.002',actor='operator',reason='new work only',event_key='later-'+wallet)
        controls.set_pause(self.db,False,actor='operator',reason='test',event_key='resume')
        first=self.reserve('new-a'); self.send(first); self.active(first,'c'*32)
        self.assertEqual(int(first['amount']),TIG//10)
        with self.assertRaisesRegex(Conflict,'member has used'):
            self.reserve('too-many-a')
        second=self.reserve('new-b',member=self.other); self.send(second); self.active(second,'d'*32)
        self.db=type(self.db)(self.db.dsn)
        pilot.initialize(self.db,self.config,actor='operator')
        with self.assertRaisesRegex(Conflict,'cumulative precommit'):
            self.reserve('after-restart')
        self.assertEqual(pilot.status(self.db)['attempts_used'],4)
        self.assertEqual(self.balance()['collateral'],TIG+TIG//10)

    def test_phase_needs_pause_and_reconciled_work(self):
        with self.assertRaisesRegex(Conflict,'pause new work'):
            self.extend()
        self.reserve()
        controls.set_pause(self.db,True,actor='operator',reason='review',event_key='pause')
        with self.assertRaisesRegex(Conflict,'in-flight'):
            self.extend()

    def test_phase_cannot_change_identity_reduce_limits_or_exceed_original_budget(self):
        self.finish_initial()
        invalid=[]
        item=self.configuration(); item['members'].reverse(); invalid.append(item)
        item=self.configuration(funding=3*TIG); invalid.append(item)
        item=self.configuration(attempts=151); invalid.append(item)
        item=self.configuration(funding=TIG-1); invalid.append(item)
        item=self.configuration(); item['members'][0]['attempt_limit']=True; invalid.append(item)
        item=self.configuration(); item['maximum_total_tig_units']=str(10*TIG); invalid.append(item)
        item=self.configuration(funding=TIG); invalid.append(item)
        for value in invalid:
            with self.subTest(value=value), self.assertRaises((FundsError,Conflict)):
                self.extend(value)
        self.assertEqual(pilot.status(self.db)['phase_number'],0)
        self.extend(self.configuration(attempts=2))
        with self.assertRaisesRegex(Conflict,'decrease or reset'):
            self.extend(self.configuration(key='reset'),expected=1)

    def test_phase_is_immutable_idempotent_and_fenced_against_concurrent_reviews(self):
        self.finish_initial()
        values=[self.configuration(key='review-a'),self.configuration(key='review-b')]
        results=self.concurrent([lambda:self.extend(values[0]),lambda:self.extend(values[1])])
        self.assertEqual(sum(isinstance(value,dict) for value in results),1,results)
        accepted=next(value for value in results if isinstance(value,dict))
        self.assertEqual(self.extend(accepted['config'])['number'],1)
        changed=deepcopy(accepted['config']); changed['members'][0]['funding_units']=str(TIG+1)
        with self.assertRaisesRegex(Conflict,'key was reused'):
            self.extend(changed)
        for sql in ('DELETE FROM pilot_phases','TRUNCATE pilot_phases',"UPDATE pilot_phases SET reason='changed'"):
            with self.assertRaises(psycopg2.Error):
                with self.db.transaction() as cursor:cursor.execute(sql)
        self.assertEqual(pilot.status(self.db)['attempts_used'],2)

    def test_extra_attributed_grant_cannot_be_laundered_through_a_new_phase(self):
        self.finish_initial()
        grant=replace(transfer('0x'+'5'*40,amount=2000*TIG),network=TESTNET)
        deposits.receive(self.db,grant)
        deposits.attribute_reviewed(self.db,grant,actor='operator',evidence={'fixture':True},operator=True)
        with self.assertRaisesRegex(Conflict,'existing attributed receipts'):
            self.extend()
