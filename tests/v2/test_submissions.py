from copy import deepcopy
import gzip
import io
import json
from pathlib import Path
import tarfile
import time
from unittest.mock import Mock
from urllib.request import Request

from fastapi.testclient import TestClient

from pool_manager.pool_v2 import benchmarks, controls, member_protocol, submissions, work_requests
from pool_manager.pool_v2.block_observer import BlockStore
from pool_manager.pool_v2.api import Settings,create_app
from pool_manager.pool_v2.artifacts import ArtifactRedirect,DEFAULT_HOSTS
from pool_manager.pool_v2.coordinator import Coordinator,verify_archive
from pool_manager.pool_v2.reconciliation import confirmed,reconcile_block,reconcile_pending
from pool_manager.pool_v2.money import Conflict, FundsError, TIG
from pool_manager.pool_v2.observation import capture_snapshot
from pool_manager.pool_v2.protocol import ProtocolDataError
from pool_manager.pool_v2.tig_transport import TigSubmissionClient
from funds_helpers import DatabaseCase,OTHER
from observer_helpers import observation


class SubmissionTests(DatabaseCase):
    def setUp(self):
        super().setUp()
        self.player = "0x"+"6"*40
        self.fund(amount=200*TIG)
        self.fees()
        self.obs = observation(8)
        # Keep one compatible CPU challenge so submission-recovery tests don't
        # depend on the selector's intentional random tie-breaking.
        next(value for value in self.obs["challenges"]["challenges"] if value["id"]=="c2")["config"]["type"]="gpu"
        self.obs["pool_player_id"]=self.player
        self.obs["pool_pending"]={"precommits":[],"benchmarks":[],"proofs":[],"frauds":[]}
        store = BlockStore(self.db)
        store.initialize(8); store.record(self.obs, collector="fixture")

    def queued(self, key="first", member=None):
        work_requests.create(self.db, member or self.member, key, resource="CPU", compute_type="aws_c7a", capacity={"workers":1})
        row = work_requests.reserve_next(self.db, self.player, now=self.obs["start"]["block"]["details"]["timestamp"])
        submissions.prepare_archive(self.db, row["id"], row["selection"]["binary"]["details"]["download_url"], b"recorded-archive-fixture")
        intent = self.row("SELECT * FROM protocol_outbox WHERE reservation_id=%s", (row["id"],))
        return row, intent

    def preflight(self, seen=None):
        return {"block_id":"block-8","height":8,"seen_benchmarks":seen or [],"observed_at":int(time.time())}

    def precommit(self, row, identity="b"*32):
        track = sorted(row["payload"]["track_settings"])[0]
        chosen = row["payload"]["track_settings"][track]
        return {"benchmark_id":identity, "settings":{**row["payload"]["settings"],"track_id":track},
            "state":{"block_confirmed":9}, "details":{**chosen,"num_nonces":chosen["num_bundles"]*2,
                "rand_hash":"a"*32,"block_started":8,"fee_paid":str(row["fee_limit"]),"compute_type":"aws_c7a"}}

    def test_two_submitters_cannot_send_same_durable_intent(self):
        row,intent=self.queued()
        results=self.concurrent([lambda:submissions.begin(self.db,intent["id"],preflight=self.preflight())]*2)
        self.assertEqual(sum(isinstance(value,dict) for value in results),1,results)
        self.assertEqual(sum(isinstance(value,Conflict) for value in results),1,results)
        saved=self.row("SELECT * FROM protocol_outbox WHERE id=%s",(intent["id"],))
        self.assertEqual(saved["state"],"uncertain")
        self.assertIsNotNone(saved["sent_at"])
        self.assertEqual(self.row("SELECT state FROM reservations WHERE id=%s",(row["id"],))["state"],"uncertain")

    def test_pause_fences_new_reservations_and_first_precommit_sends(self):
        row,intent=self.queued()
        controls.set_pause(self.db,True,actor='operator',reason='maintenance',event_key='pause')
        with self.assertRaisesRegex(Conflict,'paused'):
            submissions.begin(self.db,intent['id'],preflight=self.preflight())
        with self.assertRaisesRegex(Conflict,'paused'):
            self.reserve('direct-reservation')
        work_requests.create(self.db,self.member,'waiting',resource='CPU',compute_type='aws_c7a',capacity={'workers':1})
        self.assertIsNone(work_requests.reserve_next(self.db,self.player,now=self.obs['start']['block']['details']['timestamp']))
        self.assertEqual(self.balance()['slots'],1)
        submissions.cancel_unsent(self.db,intent['id'],evidence={'operator_pause':True})
        self.assertEqual(self.balance()['collateral'],0)
        controls.set_pause(self.db,False,actor='operator',reason='ready',event_key='resume')
        self.assertIsNotNone(work_requests.reserve_next(self.db,self.player,now=self.obs['start']['block']['details']['timestamp']))

    def test_pause_keeps_uncertain_recovery_handover_and_results_available(self):
        row,intent=self.queued()
        submissions.begin(self.db,intent['id'],preflight=self.preflight())
        controls.set_pause(self.db,True,actor='operator',reason='maintenance',event_key='pause')
        confirmed=self.precommit(row)
        self.assertEqual(submissions.recover_precommit(self.db,intent['id'],[confirmed],evidence={'block':9}),confirmed['benchmark_id'])
        assigned=submissions.publish_assignment(self.db,row['id'],confirmed,evidence={'block':9})
        member_protocol.acknowledge(self.db,assigned['benchmark_id'],self.member,assigned['assignment_digest'])
        member_protocol.results(self.db,assigned['benchmark_id'],self.member,
            {'merkle_root':'a'*64,'solution_quality':[1]*assigned['assignment']['num_nonces']})
        payload=self.row("SELECT id FROM protocol_outbox WHERE reservation_id=%s AND kind='results'",(row['id'],))
        self.assertIsNotNone(payload)
        self.assertEqual(submissions.begin(self.db,payload['id'])['state'],'uncertain')

    def test_lost_response_reconciles_one_new_match_without_resending(self):
        row,intent=self.queued()
        old=self.precommit(row,"c"*32)
        submissions.begin(self.db,intent["id"],preflight=self.preflight([old["benchmark_id"]]))
        self.assertIsNone(submissions.recover_precommit(self.db,intent["id"],[old],evidence={"block":9}))
        with self.assertRaises(Conflict): submissions.begin(self.db,intent["id"],preflight=self.preflight())
        with self.assertRaises(Conflict): submissions.cancel_unsent(self.db,intent["id"],evidence={"timeout":True})
        new=self.precommit(row)
        self.assertEqual(submissions.recover_precommit(self.db,intent["id"],[old,new],evidence={"block":9}),new["benchmark_id"])
        published=submissions.publish_assignment(self.db,row["id"],new,evidence={"confirmed_block":9})
        self.assertEqual(published["state"],"accepted")
        self.assertIsNone(published["handed_over_at"])
        self.assertEqual(published["assignment"]["num_nonces"],10)
        self.assertEqual(self.balance()["collateral"],50*TIG)

    def test_positive_identity_survives_until_complete_confirmed_metadata_arrives(self):
        row,intent=self.queued()
        submissions.begin(self.db,intent["id"],preflight=self.preflight())
        self.assertTrue(submissions.record_response(self.db,intent["id"],{"status":200,"body":{"benchmark_id":"b"*32}}))
        receipt=self.row("SELECT * FROM precommit_receipts WHERE reservation_id=%s",(row["id"],))
        self.assertEqual(receipt["benchmark_id"],"b"*32)
        pending=self.precommit(row);pending["state"]=None
        with self.assertRaises(ProtocolDataError):submissions.publish_assignment(self.db,row["id"],pending,evidence={"pending":True})
        self.assertEqual(self.row("SELECT state FROM reservations WHERE id=%s",(row["id"],))["state"],"uncertain")
        confirmed=self.precommit(row)
        published=submissions.publish_assignment(self.db,row["id"],confirmed,evidence={"block":9})
        repeated=submissions.publish_assignment(self.db,row["id"],confirmed,evidence={"block":9})
        self.assertEqual(published["assignment_digest"],repeated["assignment_digest"])
        changed=deepcopy(confirmed);changed["details"]["num_nonces"]+=1
        with self.assertRaises(ProtocolDataError):submissions.publish_assignment(self.db,row["id"],changed,evidence={"block":9})

    def test_multiple_matches_and_http_errors_retain_collateral(self):
        row,intent=self.queued()
        submissions.begin(self.db,intent["id"],preflight=self.preflight())
        self.assertFalse(submissions.record_response(self.db,intent["id"],{"status":400,"body":"unknown server failure"}))
        self.assertIsNone(submissions.recover_precommit(self.db,intent["id"],[self.precommit(row),self.precommit(row,"c"*32)],evidence={"block":9}))
        with self.assertRaises(FundsError):submissions.definitive_rejection(self.db,intent["id"],evidence={"http_status":400})
        self.assertEqual(self.balance()["collateral"],50*TIG)
        self.assertEqual(self.row("SELECT count(*) AS n FROM submission_responses")["n"],1)

    def test_indistinguishable_pending_precommit_blocks_another_member(self):
        first,intent=self.queued()
        submissions.begin(self.db,intent["id"],preflight=self.preflight())
        self.fund(member_wallet=OTHER,amount=100*TIG)
        second,other=self.queued("second",member=self.other)
        with self.assertRaises(Conflict):submissions.begin(self.db,other["id"],preflight=self.preflight())
        self.assertEqual(self.row("SELECT state FROM reservations WHERE id=%s",(second["id"],))["state"],"reserved")
        submissions.cancel_unsent(self.db,other["id"],evidence={"offer_expired_while_waiting":True})
        self.assertEqual(self.balance()["collateral"],50*TIG)
        submissions.definitive_rejection(self.db,intent["id"],evidence={"definitive_no_precommit":True,"fixture":"TIG validation rejection"})
        self.assertEqual(self.balance()["collateral"],0)
        self.assertEqual(self.balance()["slots"],0)

    def test_stale_unsent_choice_cancels_without_repricing(self):
        row,intent=self.queued()
        changed={**self.preflight(),"block_id":"block-9"}
        with self.assertRaises(Conflict):submissions.begin(self.db,intent["id"],preflight=changed)
        submissions.cancel_unsent(self.db,intent["id"],evidence={"new_block_id":"block-9"})
        self.assertEqual(self.balance()["collateral"],0)
        self.assertEqual(self.row("SELECT payload FROM reservations WHERE id=%s",(row["id"],))["payload"]["settings"]["block_id"],"block-8")

    def test_submission_transport_requires_enabled_durable_intent_and_preserves_exact_bytes(self):
        row,intent=self.queued()
        client=TigSubmissionClient("https://tig.example","fixture-key")
        client.opener=Mock()
        with self.assertRaises(FundsError):client.post(intent)
        client.enabled=True
        with self.assertRaises(FundsError):client.post(intent)
        client.opener.open.assert_not_called()
        begun=submissions.begin(self.db,intent["id"],preflight=self.preflight())
        response=Mock();response.code=200;response.read.return_value=json.dumps({"benchmark_id":"b"*32}).encode()
        client.opener.open.return_value.__enter__=Mock(return_value=response)
        client.opener.open.return_value.__exit__=Mock(return_value=False)
        # The transport keeps the original response object inside its context.
        client.opener.open.return_value.code=200
        client.opener.open.return_value.read=response.read
        result=client.post(begun)
        request=client.opener.open.call_args.args[0]
        self.assertEqual(request.data,intent["payload_text"].encode())
        self.assertEqual(request.full_url,"https://tig.example/submit-precommit")
        self.assertNotIn("fixture-key",json.dumps(result))
        self.assertTrue(submissions.record_response(self.db,intent["id"],result))

    def test_pool_pending_feed_is_archived_before_it_has_any_qualifiers(self):
        base=self.obs
        class Client:
            def get(_,path,params=None):
                if path=="/get-block":return base["end"]
                if path=="/get-benchmarks":
                    return base["players"].get(params["player_id"],{"precommits":[],"benchmarks":[],"proofs":[],"frauds":[]})
                return base[{"/get-algorithms":"algorithms","/get-challenges":"challenges","/get-opow":"opow"}[path]]
        captured=capture_snapshot(Client(),start=base["start"],pool_player_id=self.player)
        self.assertEqual(captured["pool_player_id"],self.player)
        self.assertEqual(captured["pool_pending"]["precommits"],[])
        self.assertNotIn(self.player,captured["players"])
        outcome=BlockStore(self.db).record(captured,collector="with-own-feed")
        self.assertTrue(outcome["complete"])

    def test_conflicting_pool_pending_evidence_cannot_overwrite_a_saved_block(self):
        changed=deepcopy(self.obs)
        changed['pool_pending']['frauds']=[{'benchmark_id':'b'*32,'state':{'block_confirmed':8},'allegation':None}]
        result=BlockStore(self.db).record(changed,collector='conflicting-replica')
        self.assertFalse(result['complete'])
        with self.assertRaises(ProtocolDataError):BlockStore(self.db).read('block-8')

    def test_ineligible_queue_members_cannot_starve_a_later_funded_offer(self):
        for number in range(100):
            work_requests.create(self.db,self.other,str(number),resource="CPU",compute_type="aws_c7a",capacity={"workers":1})
        work_requests.create(self.db,self.member,"eligible",resource="CPU",compute_type="aws_c7a",capacity={"workers":1})
        now=self.obs["start"]["block"]["details"]["timestamp"]
        self.assertIsNone(work_requests.reserve_next(self.db,self.player,now=now))
        self.assertEqual(work_requests.reserve_next(self.db,self.player,now=now)["member_id"],self.member)

    def test_dispatcher_commits_before_network_call_and_does_not_retry_timeout(self):
        row,intent=self.queued()
        public=Mock()
        public.get.side_effect=lambda path,params=None: ({"block":{"id":"block-8","details":{"height":8,"timestamp":int(time.time())}}}
                                                       if path=="/get-block" else {"precommits":[]})
        writer=Mock();writer.enabled=True
        def post(begun):
            persisted=self.row("SELECT state,sent_at FROM protocol_outbox WHERE id=%s",(intent["id"],))
            self.assertEqual(persisted["state"],"uncertain")
            self.assertIsNotNone(persisted["sent_at"])
            raise TimeoutError("lost HTTP response")
        writer.post.side_effect=post
        coordinator=Coordinator(self.db,self.player,public,writer,new_work=True)
        with self.assertRaises(TimeoutError):coordinator.dispatch_one()
        self.assertIsNone(coordinator.dispatch_one())
        self.assertEqual(writer.post.call_count,1)
        self.assertEqual(self.balance()["collateral"],50*TIG)

    def test_pause_cancels_only_unsent_precommit_without_calling_tig(self):
        row,intent=self.queued()
        public,writer=Mock(),Mock();writer.enabled=False
        coordinator=Coordinator(self.db,self.player,public,writer,new_work=False)
        self.assertIsNone(coordinator.dispatch_one())
        self.assertEqual(self.balance()["collateral"],0)
        writer.post.assert_not_called();public.get.assert_not_called()

    def test_replay_rotates_held_blocks_so_later_complete_blocks_progress(self):
        missing=observation(9)
        BlockStore(self.db).record(missing,collector="missing-pool-feed")
        later=observation(10)
        later["pool_player_id"]=self.player;later["pool_pending"]=self.obs["pool_pending"]
        BlockStore(self.db).record(later,collector="complete-pool-feed")
        first=reconcile_pending(self.db,self.player,limit=2)
        self.assertEqual(first["completed"],["block-8"])
        self.assertEqual([v["block_id"] for v in first["held"]],["block-9"])
        second=reconcile_pending(self.db,self.player,limit=1)
        self.assertEqual(second["completed"],["block-10"])

    def test_recorded_public_verification_failure_is_confirmed_not_an_arbitration(self):
        with gzip.open(Path(__file__).parent/'fixtures/confirmed-verification-failure.json.gz','rt') as source:
            recorded=json.load(source)
        self.assertTrue(confirmed(recorded["fraud"],recorded["block_height"]))
        self.assertEqual(recorded["proof"]["state"]["block_confirmed"],recorded["fraud"]["state"]["block_confirmed"])
        self.assertGreater(recorded["proof"]["details"]["block_active"],recorded["fraud"]["state"]["block_confirmed"])

    def test_archive_preflight_requires_actual_architecture_library_and_gpu_ptx(self):
        buffer=io.BytesIO()
        with tarfile.open(fileobj=buffer,mode='w:gz') as target:
            item=tarfile.TarInfo('amd64/c001_a001.so');item.size=3
            target.addfile(item,io.BytesIO(b'bin'))
        verify_archive(buffer.getvalue(),'c001_a001','aws_c7a')
        with self.assertRaises(ProtocolDataError):verify_archive(buffer.getvalue(),'c001_a001','aws_g4dn')
        with self.assertRaises(ProtocolDataError):verify_archive(buffer.getvalue(),'c001_a001','aws_c7g')

    def test_only_public_tig_artifact_redirects_are_followed_without_credentials(self):
        handler=ArtifactRedirect(DEFAULT_HOSTS)
        request=Request('https://mainnet-api.tig.foundation/get-binary-blob',headers={'Authorization':'secret-fixture'})
        target='https://media.githubusercontent.com/media/tig-foundation/tig-monorepo/challenge/algorithm/library.tar.gz'
        redirected=handler.redirect_request(request,None,307,'Temporary Redirect',{},target)
        self.assertEqual(redirected.full_url,target)
        self.assertNotIn('Authorization',dict(redirected.header_items()))
        for url in ('http://media.githubusercontent.com/file','https://other.example/file',
                    'https://media.githubusercontent.com/media/another-owner/project/file'):
            with self.assertRaises(FundsError):handler.redirect_request(request,None,307,'redirect',{},url)

    def test_pool_serves_exact_saved_archive_and_assignment_uses_its_checksum_url(self):
        row,intent=self.queued()
        submissions.begin(self.db,intent["id"],preflight=self.preflight())
        submissions.record_response(self.db,intent["id"],{"status":200,"body":{"benchmark_id":"b"*32}})
        published=submissions.publish_assignment(self.db,row["id"],self.precommit(row),evidence={"block":9},artifact_origin='https://pool.example')
        assignment=published['assignment'];digest=assignment['binary_sha256']
        self.assertEqual(assignment['binary_url'],'https://pool.example/api/v2/artifacts/'+digest)
        api=TestClient(create_app(Settings(self.db.dsn,'https://pool.example',8453,'a'*64)))
        reply=api.get('/api/v2/artifacts/'+digest)
        self.assertEqual(reply.status_code,200)
        self.assertEqual(reply.content,b'recorded-archive-fixture')
        self.assertEqual(reply.headers['etag'],'"'+digest+'"')
        self.assertEqual(api.get('/api/v2/artifacts/not-a-checksum').status_code,404)

    def accepted_with_pending_proof(self):
        row,intent=self.queued()
        submissions.begin(self.db,intent["id"],preflight=self.preflight())
        submissions.record_response(self.db,intent["id"],{"status":200,"body":{"benchmark_id":"b"*32}})
        precommit=self.precommit(row)
        accepted=submissions.publish_assignment(self.db,row["id"],precommit,evidence={"block":9})
        benchmarks.acknowledge(self.db,row["id"],self.member,accepted["assignment_digest"])
        member_protocol.results(self.db,accepted["benchmark_id"],self.member,{"merkle_root":"a"*64,"solution_quality":[9]*10})
        result_intent=self.row("SELECT id FROM protocol_outbox WHERE reservation_id=%s AND kind='results'",(row["id"],))
        submissions.begin(self.db,result_intent["id"])
        submissions.record_response(self.db,result_intent["id"],{"status":200,"body":{"ok":True}})
        # This adapter fixture supplies an existing member upload; proof
        # construction/validation is covered by the paired member API suite.
        with self.db.transaction() as cursor:
            work_requests.enqueue(cursor,row["id"],"proofs",{"benchmark_id":accepted["benchmark_id"],"merkle_proofs":[]})
        proof_intent=self.row("SELECT id FROM protocol_outbox WHERE reservation_id=%s AND kind='proofs'",(row["id"],))
        submissions.begin(self.db,proof_intent["id"])
        return row,precommit

    def outcome_observation(self,row,precommit,height,*,active=False,failure=False,missing=False):
        value=observation(height)
        identity=precommit["benchmark_id"]
        benchmark={"id":identity,"state":{"block_confirmed":10},"details":{"stopped":False,
            "merkle_root":"a"*64,"sampled_nonces":[0],"num_active_bundles":2,"average_quality_by_bundle":[9,7]}}
        proof={"benchmark_id":identity,"state":{"block_confirmed":11},"details":{"block_active":13,"submission_delay":3}}
        feed={"precommits":[precommit],"benchmarks":[benchmark],"proofs":[proof],"frauds":[]}
        if failure:feed["frauds"]=[{"benchmark_id":identity,"state":{"block_confirmed":11},"allegation":None}]
        if missing:feed={"precommits":[],"benchmarks":[],"proofs":[],"frauds":[]}
        value["pool_player_id"]=self.player
        if active:
            block=value["start"]["block"]
            block["data"]["active_ids"]["benchmark"].append(identity)
            block["data"]["active_ids"]["opow"].append(self.player)
            block["details"]["num_active"]["benchmark"]+=1
            block["details"]["num_active"]["opow"]+=1
            value["end"]=deepcopy(value["start"])
            value["players"][self.player]=feed
            cid,aid,track=(precommit["settings"][key] for key in ("challenge_id","algorithm_id","track_id"))
            value["opow"]["opow"].append({"player_id":self.player,"block_data":{"num_qualifiers_by_challenge_by_track":{cid:{track:1}}}})
            algorithm=next(item for item in value["algorithms"]["codes"] if item["id"]==aid)
            algorithm["block_data"]["num_qualifiers_by_track_by_player"].setdefault(track,{})[self.player]=1
            challenge=next(item for item in value["challenges"]["challenges"] if item["id"]==cid)
            challenge["block_data"]["num_qualifiers_by_track"][track]+=1
        else:value["pool_pending"]=feed
        outcome=BlockStore(self.db).record(value,collector="outcome-fixture")
        self.assertTrue(outcome["complete"],outcome)
        return value["start"]["block"]["id"]

    def test_confirmed_proof_waits_for_actual_activation_before_freeing_slot(self):
        row,precommit=self.accepted_with_pending_proof()
        before=self.outcome_observation(row,precommit,12)
        reconcile_block(self.db,before,self.player)
        self.assertEqual(self.balance()["slots"],1)
        self.assertEqual(member_protocol.get(self.db,precommit["benchmark_id"],self.member)["sampled_nonces"],[0])
        after=self.outcome_observation(row,precommit,13,active=True)
        reconcile_block(self.db,after,self.player)
        self.assertEqual(self.balance()["slots"],0)
        self.assertEqual(self.balance()["collateral"],50*TIG)
        self.assertEqual(self.row("SELECT active_at_height FROM reservations WHERE id=%s",(row["id"],))["active_at_height"],13)

    def test_confirmed_verification_failure_frees_slot_and_keeps_collateral(self):
        row,precommit=self.accepted_with_pending_proof()
        block_id=self.outcome_observation(row,precommit,12,failure=True)
        reconcile_block(self.db,block_id,self.player)
        self.assertEqual(self.balance()["slots"],0)
        self.assertEqual(self.balance()["collateral"],50*TIG)
        self.assertEqual(self.row("SELECT state FROM reservations WHERE id=%s",(row["id"],))["state"],"verification_failed")

    def test_disappeared_protocol_records_are_not_inferred_to_be_expired(self):
        row,precommit=self.accepted_with_pending_proof()
        block_id=self.outcome_observation(row,precommit,200,missing=True)
        reconcile_block(self.db,block_id,self.player)
        self.assertEqual(self.balance()["slots"],1)
        self.assertEqual(self.balance()["collateral"],50*TIG)
