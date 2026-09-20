"""Real HTTPS browser/API/PostgreSQL flows, using generated wallets and chain fixtures."""

from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
import unittest

from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi.testclient import TestClient
import uvicorn

from pool_manager.pool_v2 import custody, deposits, funding, ledger, members
from pool_manager.pool_v2.api import Settings, create_app
from pool_manager.pool_v2.chain import CustodyPreflight
from pool_manager.pool_v2.money import TIG
from funds_helpers import DatabaseCase, NETWORK, CUSTODY, chain_fixture, transfer
from test_withdrawals import payment_fixture
from test_funding import TOPUP,funding_capture,protocol_topup


@unittest.skipUnless(os.environ.get('POOL_V2_BROWSER_TESTS') == '1',
                     'set POOL_V2_BROWSER_TESTS=1 and install the pinned Playwright browser')
class DashboardBrowserTests(DatabaseCase):
    def setUp(self):
        super().setUp()
        self.signer=Account.create()
        self.wallet=self.signer.address.lower()
        self.operator='browser-fixture-operator'
        with self.db.transaction() as cursor:
            self.browser_member=members.register_verified(cursor,self.wallet)['id']
        deposits.receive(self.db,transfer(self.wallet,amount=100*TIG+1))
        self.reservation=self.reserve('browser-work',member=self.browser_member)
        _,funding,tx_hash=payment_fixture(sender='0x'+'7'*40,to=CUSTODY,value=1000)
        custody.receive_native(self.db,funding.transaction(tx_hash,fee_model='op-jovian'))
        _,self.external_deposit,self.external_hash=chain_fixture(sender='0x'+'8'*40,amount=9*TIG)
        deposits.receive(self.db,self.external_deposit.transfer(self.external_hash,2))
        _,self.extra_native,self.native_hash=payment_fixture(sender='0x'+'7'*40,to=CUSTODY,value=100,nonce=2)
        self.certificate=tempfile.TemporaryDirectory(prefix='innopool-v2-browser-')
        self.addCleanup(self.certificate.cleanup)
        cert,key=(str(Path(self.certificate.name)/name) for name in ('cert.pem','key.pem'))
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1',
            '-keyout',key,'-out',cert,'-subj','/CN=127.0.0.1'],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        listener=socket.socket()
        listener.bind(('127.0.0.1',0));listener.listen(128)
        self.addCleanup(listener.close)
        self.origin='https://127.0.0.1:'+str(listener.getsockname()[1])
        settings=Settings(self.db.dsn,self.origin,8453,hashlib.sha256(self.operator.encode()).hexdigest(),
            funds_enabled=True,work_enabled=True,pool_player_id=CUSTODY,custody_network=NETWORK,
            custody_rpc_url='https://rpc.example',withdrawal_fee_model='op-jovian')
        self.app=create_app(settings)
        test=self
        class SimulatedChain:
            network=NETWORK
            def preflight(self):
                with test.db.transaction() as cursor:
                    cursor.execute('SELECT coalesce(max(s.nonce),0)+1 AS next FROM custody_payments p JOIN custody_sends s ON s.id=p.send_id')
                    nonce=int(cursor.fetchone()['next'])
                    return CustodyPreflight(NETWORK,nonce,ledger.backing(cursor),ledger.backing(cursor,'NATIVE'),90,
                        datetime.now(timezone.utc),{'fixture':True})
            def find_nonce(self,nonce,*,after_height):
                test.assertEqual(after_height,90)
                test.assertIn(nonce,(1,2))
                _,self.chain,self.tx_hash=payment_fixture(amount=40*TIG+1 if nonce==1 else 5*TIG+1,
                    recipient=test.wallet if nonce==1 else TOPUP,nonce=nonce)
                return self.tx_hash
            def transaction(self,tx_hash,**kwargs):
                return (test.extra_native if tx_hash==test.native_hash else self.chain).transaction(tx_hash,**kwargs)
            def transfer(self,tx_hash,index):
                return (test.external_deposit if tx_hash==test.external_hash else self.chain).transfer(tx_hash,index)
        self.app.state.payment_chain=SimulatedChain()
        self.server=uvicorn.Server(uvicorn.Config(self.app,log_level='critical',access_log=False,
            ssl_certfile=cert,ssl_keyfile=key))
        self.thread=threading.Thread(target=self.server.run,kwargs={'sockets':[listener]},daemon=True)
        def stop():
            self.server.should_exit=True
            self.thread.join(timeout=10)
            if self.thread.is_alive():raise RuntimeError('isolated browser server did not stop')
        self.addCleanup(stop)
        self.thread.start()
        deadline=time.monotonic()+10
        while not self.server.started and self.thread.is_alive() and time.monotonic()<deadline:time.sleep(.02)
        self.assertTrue(self.server.started,'isolated HTTPS server failed to start')

    def test_member_and_operator_complete_reviewed_payment_without_float_rounding(self):
        from playwright.sync_api import sync_playwright, expect
        playwright=sync_playwright().start()
        self.addCleanup(playwright.stop)
        browser=playwright.chromium.launch()
        context=browser.new_context(ignore_https_errors=True,viewport={'width':1440,'height':1000})
        self.addCleanup(browser.close)
        wallet_methods=[]
        def wallet_request(request):
            wallet_methods.append(request['method'])
            if request['method']=='eth_requestAccounts':return [self.wallet]
            if request['method']=='eth_chainId':return '0x2105'
            if request['method']=='personal_sign':
                self.assertEqual(request['params'][1].lower(),self.wallet)
                return '0x'+Account.sign_message(encode_defunct(hexstr=request['params'][0]),self.signer.key).signature.hex()
            raise AssertionError('The browser must never ask to send a transaction: '+request['method'])
        context.expose_function('fixtureWallet',wallet_request)
        context.add_init_script('window.ethereum={request: request => window.fixtureWallet(request)};')
        page=context.new_page()
        errors=[]
        page.on('pageerror',lambda error:errors.append(str(error)))
        page.goto(self.origin)
        expect(page.locator('#work-status')).to_have_text('Accepting work')
        page.get_by_role('button',name='Connect wallet',exact=True).click()
        expect(page.locator('#available')).to_have_text('50.000000000000000001')
        expect(page.locator('#collateral')).to_have_text('50')
        page.get_by_label('Withdraw TIG',exact=True).fill('40.000000000000000001')
        page.get_by_role('button',name='Request withdrawal').click()
        expect(page.locator('#pending')).to_have_text('40.000000000000000001')
        expect(page.locator('#available')).to_have_text('10')
        page.get_by_role('button',name='Create execution token').click()
        expect(page.locator('#worker-token-box')).to_be_visible()
        execution=page.locator('#worker-token').input_value()
        self.assertGreater(len(execution),30)
        with TestClient(self.app) as client:
            result=client.post('/api/v2/withdrawals',json={'amount':'1','request_key':'worker-must-not-withdraw'},
                headers={'Authorization':'Bearer '+execution})
            self.assertEqual(result.status_code,401)

        operator=context.new_page()
        operator.on('pageerror',lambda error:errors.append(str(error)))
        operator.goto(self.origin+'/operator')
        operator.get_by_label('Operator token',exact=True).fill(self.operator)
        operator.get_by_role('button',name='Open operator dashboard').click()
        expect(operator.locator('#operator-content')).to_be_visible()
        expect(operator.locator('#operator-token')).to_have_value('')
        operator.locator('#unattributed-deposits').get_by_role('button',name='Operator funding',exact=True).click()
        operator.get_by_label('Ownership check').fill('Fixture operator source checked independently')
        operator.locator('#action-submit').click()
        expect(operator.locator('#unattributed-deposits')).to_contain_text('No deposits await attribution.')
        operator.get_by_role('button',name='Record native funding',exact=True).click()
        operator.get_by_label('Transaction hash',exact=True).fill(self.native_hash)
        operator.locator('#action-submit').click()
        expect(operator.locator('#action-dialog')).not_to_be_visible()
        self.assertEqual(self.row("SELECT balance FROM accounts WHERE id='operator:custody:TIG'")['balance'],9*TIG)
        row=operator.locator('#operator-members tr').filter(has=operator.locator('[title="'+self.wallet+'"]'))
        row.get_by_role('button',name='Edit multiplier').click()
        operator.get_by_label('Multiplier · 0 to 1').fill('0.2')
        # User-controlled text is rendered as text, including HTML-looking audit notes.
        reason='<img src=x onerror=alert(1)> reviewed trust'
        operator.get_by_label('Reason',exact=True).fill(reason)
        operator.get_by_role('button',name='Save multiplier').click()
        expect(operator.locator('#action-dialog')).not_to_be_visible()
        expect(operator.locator('#multiplier-history')).to_contain_text(reason)
        self.assertEqual(operator.locator('#multiplier-history img').count(),0)
        self.assertEqual(int(self.row('SELECT amount FROM reservations WHERE id=%s',(self.reservation['id'],))['amount']),50*TIG)
        operator.get_by_role('button',name='Pause new work',exact=True).click()
        expect(operator.locator('#work-status')).to_have_text('New work paused')
        operator.get_by_role('button',name='Resume new work',exact=True).click()
        expect(operator.locator('#work-status')).to_have_text('Accepting work')
        operator.locator('#operator-withdrawals').get_by_role('button',name='Approve',exact=True).click()
        operator.get_by_label('Review notes').fill('Fixture destination and full amount checked')
        operator.locator('#action-submit').click()
        expect(operator.locator('#action-dialog')).not_to_be_visible()
        operator.get_by_role('button',name='Prepare payment',exact=True).click()
        operator.get_by_label('Maximum reserved operator fee · native token').fill('0.0000000000000001')
        operator.locator('#action-submit').click()
        expect(operator.locator('#action-title')).to_have_text('Manual payment details')
        expect(operator.locator('#action-description')).to_contain_text('potentially sent')
        expect(operator.locator('#action-fields')).to_contain_text('40.000000000000000001 TIG')
        operator.locator('#close-dialog').click()
        operator.locator('#operator-withdrawals').get_by_role('button',name='Check transfer',exact=True).click()
        expect(operator.locator('#action-fields')).to_contain_text('40.000000000000000001 TIG')
        operator.locator('#action-submit').click()
        expect(operator.locator('#action-dialog')).not_to_be_visible()
        expect(operator.locator('#operator-withdrawals')).to_contain_text('Paid')
        page.locator('#refresh-member').click()
        expect(page.locator('#pending')).to_have_text('0')
        expect(page.locator('#available')).to_have_text('10')
        expect(page.locator('#collateral')).to_have_text('50')
        expect(page.locator('#multiplier')).to_have_text('0.2×')
        page.get_by_label('Withdraw TIG',exact=True).fill('1')
        page.get_by_role('button',name='Request withdrawal').click()
        expect(page.locator('#message')).to_contain_text('seven days')
        self.assertNotIn('eth_sendTransaction',wallet_methods)
        self.assertEqual(self.row('SELECT count(*) AS n FROM withdrawal_attempt_outcomes')['n'],1)
        self.assertEqual(self.row("SELECT balance FROM accounts WHERE id='operator:custody:NATIVE'")['balance'],1040)
        page.locator('#active-tokens').get_by_role('button',name='Revoke',exact=True).click()
        expect(page.locator('#worker-token-box')).not_to_be_visible()
        with TestClient(self.app) as client:
            self.assertEqual(client.get('/api/v2/member/balance',headers={'Authorization':'Bearer '+execution}).status_code,401)

        funding.record(self.db,funding_capture())
        operator.locator('#refresh-operator').click()
        expect(operator.locator('#funding-health')).to_have_text('Reconciled')
        operator.get_by_role('button',name='Prepare fee top-up',exact=True).click()
        operator.get_by_label('Amount · TIG',exact=True).fill('5.000000000000000001')
        operator.get_by_label('Maximum reserved operator fee · native token').fill('0.0000000000000001')
        operator.locator('#action-submit').click()
        expect(operator.locator('#action-title')).to_have_text('Manual fee top-up details')
        expect(operator.locator('#action-fields')).to_contain_text('5.000000000000000001 TIG')
        operator.locator('#close-dialog').click()
        operator.locator('#operator-topups').get_by_role('button',name='Check transfer',exact=True).click()
        operator.locator('#action-submit').click()
        expect(operator.locator('#action-dialog')).not_to_be_visible()
        expect(operator.locator('#operator-topups')).to_contain_text('Awaiting TIG credit')
        self.assertEqual(self.row("SELECT balance FROM accounts WHERE id='operator:protocol:TIG'")['balance'],0)
        funding.record(self.db,funding_capture(5*TIG+1,protocol_topup(self.app.state.payment_chain.tx_hash,amount=5*TIG+1)))
        operator.locator('#operator-topups').get_by_role('button',name='Check TIG credit',exact=True).click()
        expect(operator.locator('#operator-topups')).to_contain_text('Credited')
        expect(operator.locator('#funding-health')).to_have_text('Reconciled')
        self.assertEqual(self.row("SELECT balance FROM accounts WHERE id='operator:protocol:TIG'")['balance'],5*TIG+1)
        self.assertEqual(self.row("SELECT balance FROM accounts WHERE id='operator:custody:NATIVE'")['balance'],980)
        self.assertEqual(members.balances(self.db,self.browser_member)['available'],10*TIG)
        self.assertNotIn('eth_sendTransaction',wallet_methods)

        output=os.environ.get('POOL_V2_BROWSER_ARTIFACTS')
        if output:
            Path(output).mkdir(parents=True,exist_ok=True)
            page.screenshot(path=str(Path(output)/'member-desktop.png'),full_page=True)
            operator.screenshot(path=str(Path(output)/'operator-desktop.png'),full_page=True)
        for current,name in ((page,'member'),(operator,'operator')):
            current.set_viewport_size({'width':390,'height':844})
            self.assertTrue(current.evaluate('document.documentElement.scrollWidth <= window.innerWidth'),name+' overflows mobile viewport')
            if output:current.screenshot(path=str(Path(output)/(name+'-mobile.png')),full_page=True)
            self.assertEqual(current.evaluate('Object.keys(localStorage).length'),0)
            self.assertEqual(current.evaluate('Object.keys(sessionStorage).length'),0)
        self.assertEqual(errors,[])
        page.get_by_role('button',name='Sign out',exact=True).click()
        expect(page.locator('#member-content')).not_to_be_visible()
