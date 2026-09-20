from datetime import datetime, timezone
import hashlib

from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi.testclient import TestClient

from pool_manager.pool_v2 import custody, deposits, ledger
from pool_manager.pool_v2.api import Settings, create_app
from pool_manager.pool_v2.chain import CustodyPreflight
from pool_manager.pool_v2.money import TIG
from funds_helpers import DatabaseCase, NETWORK, CUSTODY, transfer
from test_withdrawals import payment_fixture


class WithdrawalApiTests(DatabaseCase):
    def setUp(self):
        super().setUp()
        self.secret='test-operator-only'
        settings=Settings(self.db.dsn,'https://pool.example',8453,hashlib.sha256(self.secret.encode()).hexdigest(),
            funds_enabled=True,custody_network=NETWORK,custody_rpc_url='https://rpc.example',withdrawal_fee_model='op-jovian')
        self.app=create_app(settings); self.client=TestClient(self.app)
        signer=Account.create(); self.wallet=signer.address.lower()
        challenge=self.client.post('/api/v2/auth/challenges',json={'wallet':self.wallet}).json()
        signed=Account.sign_message(encode_defunct(text=challenge['message']),signer.key).signature.hex()
        session=self.client.post('/api/v2/auth/sessions',json={'challenge_id':challenge['id'],'signature':signed}).json()
        self.member_headers={'Authorization':'Bearer '+session['token']}
        self.operator_headers={'Authorization':'Bearer '+self.secret}
        execution=self.client.post('/api/v2/auth/execution-tokens',headers=self.member_headers).json()['token']
        self.execution_headers={'Authorization':'Bearer '+execution}
        deposits.receive(self.db,transfer(self.wallet,amount=100*TIG))
        _,funding,hash1=payment_fixture(sender='0x'+'7'*40,to=CUSTODY,value=1000)
        custody.receive_native(self.db,funding.transaction(hash1,fee_model='op-jovian'))
        _,self.chain,self.tx_hash=payment_fixture(recipient=self.wallet)
        test=self
        class SimulatedRpc:
            network=NETWORK
            preflight_calls=0
            def preflight(self):
                self.preflight_calls+=1
                if self.preflight_calls>1:raise AssertionError('idempotent recovery must not depend on RPC')
                with test.db.transaction() as cursor:
                    return CustodyPreflight(NETWORK,1,ledger.backing(cursor),ledger.backing(cursor,'NATIVE'),90,
                        datetime.now(timezone.utc),{'fixture':True})
            def find_nonce(self,nonce,*,after_height):
                test.assertEqual((nonce,after_height),(1,90))
                return test.tx_hash
            def transaction(self,*args,**kwargs):return test.chain.transaction(*args,**kwargs)
            def transfer(self,*args,**kwargs):return test.chain.transfer(*args,**kwargs)
        self.app.state.payment_chain=SimulatedRpc()

    def request(self,key='withdraw'):
        result=self.client.post('/api/v2/withdrawals',json={'amount':str(40*TIG),'request_key':key},headers=self.member_headers)
        self.assertEqual(result.status_code,200,result.text)
        return result.json()['id']

    def test_only_operator_can_prepare_and_recover_full_payment_after_lost_hash(self):
        identity=self.request();base='/api/v2/operator/withdrawals/'+identity
        for headers in (self.member_headers,self.execution_headers):
            self.assertEqual(self.client.post(base+'/approve',json={'reason':'checked'},headers=headers).status_code,403)
            self.assertEqual(self.client.get('/api/v2/operator/withdrawals',headers=headers).status_code,403)
        approved=self.client.post(base+'/approve',json={'reason':'identity, destination and funds checked'},headers=self.operator_headers)
        self.assertEqual(approved.status_code,200,approved.text)
        body={'request_key':'send-once','fee_limit':'100'}
        begun=self.client.post(base+'/begin',json=body,headers=self.operator_headers)
        self.assertEqual(begun.status_code,200,begun.text)
        attempt=begun.json()
        self.assertEqual(attempt['amount'],str(40*TIG))
        self.assertEqual(attempt['state'],'uncertain')
        self.assertEqual(self.client.post(base+'/begin',json=body,headers=self.operator_headers).json()['id'],attempt['id'])
        self.assertEqual(self.client.post('/api/v2/withdrawals/'+identity+'/cancel',
            json={'reason':'connection lost','event_key':'cancel'},headers=self.member_headers).status_code,409)
        paid=self.client.post('/api/v2/operator/withdrawal-attempts/'+attempt['id']+'/reconcile',json={},headers=self.operator_headers)
        self.assertEqual(paid.status_code,200,paid.text)
        self.assertEqual(paid.json()['outcome'],'paid')
        self.assertEqual(paid.json()['fee'],'60')
        shown=self.client.get('/api/v2/member/withdrawals',headers=self.member_headers).json()['withdrawals']
        self.assertEqual(len(shown),1)
        self.assertEqual(shown[0]['state'],'paid')

    def test_wallet_cancel_releases_request_but_execution_token_cannot(self):
        identity=self.request(); path='/api/v2/withdrawals/'+identity+'/cancel'
        body={'reason':'changed my mind','event_key':'member-cancel'}
        self.assertEqual(self.client.post(path,json=body,headers=self.execution_headers).status_code,401)
        result=self.client.post(path,json=body,headers=self.member_headers)
        self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(result.json()['state'],'cancelled')
        balance=self.client.get('/api/v2/member/balance',headers=self.member_headers).json()
        self.assertEqual(balance['available'],str(100*TIG))
        self.assertIsNone(balance['last_paid_at'])

    def test_new_destination_requires_its_signature_and_does_not_change_a_pending_request(self):
        identity=self.request()
        destination=Account.create()
        path='/api/v2/member/withdrawal-wallet'
        body={'wallet':destination.address}
        self.assertEqual(self.client.post(path+'/challenges',json=body,headers=self.execution_headers).status_code,401)
        challenge=self.client.post(path+'/challenges',json=body,headers=self.member_headers).json()
        wrong=Account.create()
        signature=Account.sign_message(encode_defunct(text=challenge['message']),wrong.key).signature.hex()
        submission={'challenge_id':challenge['id'],'signature':signature}
        self.assertEqual(self.client.post(path,json=submission,headers=self.member_headers).status_code,401)
        submission['signature']=Account.sign_message(encode_defunct(text=challenge['message']),destination.key).signature.hex()
        self.assertEqual(self.client.post(path,json=submission,headers=self.execution_headers).status_code,401)
        result=self.client.post(path,json=submission,headers=self.member_headers)
        self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(result.json()['withdrawal_wallet'],destination.address.lower())
        self.assertEqual(self.client.post(path,json=submission,headers=self.member_headers).status_code,401)
        self.assertEqual(self.row('SELECT recipient FROM withdrawals WHERE id=%s',(identity,))['recipient'],self.wallet)
        self.client.post('/api/v2/withdrawals/'+identity+'/cancel',json={'reason':'change','event_key':'cancel'},headers=self.member_headers)
        future=self.request(key='future')
        self.assertEqual(self.row('SELECT recipient FROM withdrawals WHERE id=%s',(future,))['recipient'],destination.address.lower())
