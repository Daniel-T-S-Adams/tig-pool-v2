"""Isolated v2 API factory. Importing it does not start legacy pool processes."""

from dataclasses import dataclass
from decimal import Decimal
import hashlib
import re
import secrets
import uuid
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse,JSONResponse,Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StrictBool,StrictInt, StrictStr

from .auth import Auth, AuthenticationError
from .database import Database
from .chain import Chain, Network, Rpc, FEE_MODELS, UnsupportedCustodyTransaction
from . import members, withdrawals, work_requests, member_protocol
from . import artifacts, native_funding, sponsored_withdrawals
from . import controls,custody,dashboard,deposits,settlement,chain_observer,funding,topups,releases
from .protocol import ProtocolDataError
from .money import Conflict, FundsError, InsufficientFunds


API_VERSION = "2.0"


@dataclass(frozen=True)
class Settings:
    database_dsn: str
    origin: str
    chain_id: int
    operator_token_sha256: str
    # Keep monetary API actions closed until custody/observation integration is
    # configured and verified. No runtime is deployed by this module.
    funds_enabled: bool = False
    work_enabled: bool = False
    pool_player_id: str | None = None
    offer_ttl_seconds: int = 60
    custody_network: Network | None = None
    custody_rpc_url: str | None = None
    withdrawal_fee_model: str | None = None
    settlement_enabled: bool = False
    release_manifest: dict | None = None
    build_commit: str | None = None
    worker_installer: bytes | None = None
    custody_trace_rpc_url: str | None = None


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WalletChallenge(Input):
    wallet: StrictStr = Field(max_length=42)


class WalletSignature(Input):
    challenge_id: uuid.UUID
    signature: StrictStr = Field(max_length=132)


class MultiplierChange(Input):
    multiplier: StrictStr = Field(max_length=82)
    reason: StrictStr = Field(min_length=1, max_length=1000)
    event_key: StrictStr = Field(min_length=1, max_length=128)


class WithdrawalRequest(Input):
    amount: StrictStr = Field(pattern=r"^[1-9][0-9]{0,77}$")
    request_key: StrictStr = Field(min_length=1, max_length=128)


class WithdrawalReview(Input):
    reason: StrictStr = Field(min_length=1, max_length=1000)


class WithdrawalRelease(WithdrawalReview):
    event_key: StrictStr = Field(min_length=1, max_length=128)


class WithdrawalSend(Input):
    request_key: StrictStr = Field(min_length=1, max_length=128)
    fee_limit: StrictStr = Field(pattern=r"^[1-9][0-9]{0,77}$")


class WithdrawalReconcile(Input):
    tx_hash: StrictStr | None = Field(default=None, pattern=r"^0x[0-9a-fA-F]{64}$")
    log_index: StrictInt | None = Field(default=None, ge=0)


class SponsoredWithdrawalRecovery(WithdrawalReview):
    tx_hash: StrictStr = Field(pattern=r"^0x[0-9a-fA-F]{64}$")
    log_index: StrictInt = Field(ge=0)


class TopupRequest(WithdrawalSend):
    amount: StrictStr = Field(pattern=r'^[1-9][0-9]{0,77}$')


class PauseChange(WithdrawalRelease):
    paused: StrictBool


class SettlementApproval(Input):
    input_digest: StrictStr = Field(pattern=r'^[0-9a-f]{64}$')


class CustodyReceipt(Input):
    tx_hash: StrictStr = Field(pattern=r'^0x[0-9a-fA-F]{64}$')
    log_index: StrictInt | None = Field(default=None,ge=0)


class DepositAttribution(CustodyReceipt):
    member_wallet: StrictStr | None = Field(default=None,max_length=42)
    operator_funding: StrictBool = False
    reason: StrictStr = Field(min_length=1,max_length=1000)


class NativeCustodyReceipt(CustodyReceipt):
    trace_address: list[StrictInt] | None = Field(default=None, min_length=1, max_length=64)


class RevokeToken(Input):
    token: StrictStr = Field(min_length=32, max_length=128)


class Capacity(Input):
    workers: StrictInt = Field(ge=1, le=4096)


class WorkRequest(Input):
    request_key: StrictStr = Field(min_length=1, max_length=128)
    resource: Literal["CPU", "GPU"]
    compute_type: StrictStr = Field(max_length=32)
    capacity: Capacity


class Acknowledge(Input):
    assignment_digest: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")


def response(value):
    return JSONResponse(jsonable_encoder(value, custom_encoder={Decimal: str}),
                        headers={"Cache-Control": "no-store"})


def create_app(settings):
    release=releases.validate(settings.release_manifest,settings.build_commit,settings.worker_installer)
    release_digest=hashlib.sha256(releases.canonical(release).encode()).hexdigest() if release else None
    if len(settings.operator_token_sha256) != 64:
        raise ValueError("operator token SHA-256 must be configured explicitly")
    if settings.work_enabled:
        if not settings.funds_enabled or not settings.pool_player_id:
            raise ValueError("work requires explicit member-funds and pool-account configuration")
        members.address(settings.pool_player_id)
    database = Database(settings.database_dsn)
    payment_chain = None
    if any(value is not None for value in (settings.custody_network, settings.custody_rpc_url, settings.withdrawal_fee_model)):
        if (not settings.custody_network or not settings.custody_rpc_url
                or settings.withdrawal_fee_model not in FEE_MODELS or settings.chain_id != settings.custody_network.chain_id):
            raise ValueError('custody network, RPC, fee model and matching authentication chain must be configured together')
        payment_chain = Chain(settings.custody_network, Rpc(settings.custody_rpc_url))
    auth = Auth(database, origin=settings.origin, chain_id=settings.chain_id)
    app = FastAPI(title="InnoPool v2", version=API_VERSION)
    app.state.database, app.state.auth = database, auth
    app.state.payment_chain = payment_chain
    if settings.custody_trace_rpc_url and payment_chain is None:
        raise ValueError('native trace RPC requires verified custody configuration')
    app.state.custody_trace_rpc = Rpc(settings.custody_trace_rpc_url) if settings.custody_trace_rpc_url else None
    web_directory=Path(__file__).with_name('web')
    app.mount('/assets',StaticFiles(directory=web_directory),name='v2-assets')

    @app.middleware('http')
    async def browser_headers(request,call_next):
        result=await call_next(request)
        result.headers['X-Content-Type-Options']='nosniff'
        result.headers['Referrer-Policy']='no-referrer'
        result.headers['Content-Security-Policy']="default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        return result

    @app.get('/',include_in_schema=False)
    @app.get('/operator',include_in_schema=False)
    @app.get('/join',include_in_schema=False)
    def website():
        return FileResponse(web_directory/'index.html',headers={'Cache-Control':'no-store'})

    @app.exception_handler(FundsError)
    async def funds_error(request, exc):
        code = 409 if isinstance(exc, (Conflict, InsufficientFunds)) else 400
        return JSONResponse({"detail": str(exc)}, status_code=code, headers={"Cache-Control": "no-store"})

    @app.exception_handler(AuthenticationError)
    async def authentication_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=401, headers={"Cache-Control": "no-store"})

    @app.exception_handler(ProtocolDataError)
    async def protocol_error(request,exc):
        return JSONResponse({'detail':str(exc)},status_code=409,headers={'Cache-Control':'no-store'})

    def bearer(authorization: str | None = Header(default=None)):
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(401, "bearer authentication required")
        return authorization[7:]

    def principal(token=Depends(bearer)):
        with database.transaction() as cursor:
            return auth.authenticate(cursor, token)

    def wallet_principal(token=Depends(bearer)):
        with database.transaction() as cursor:
            return auth.authenticate(cursor, token, wallet=True)

    def operator(token=Depends(bearer)):
        supplied = hashlib.sha256(token.encode()).hexdigest()
        if not secrets.compare_digest(supplied, settings.operator_token_sha256):
            raise HTTPException(403, "operator authentication required")
        return "operator:" + settings.operator_token_sha256[:12]

    def compatible(x_innopool_version: str | None = Header(default=None)):
        if x_innopool_version != API_VERSION:
            raise HTTPException(426, "unsupported member protocol version")

    def custody_chain():
        if app.state.payment_chain is None:
            raise HTTPException(503, 'verified custody payment configuration is not enabled')
        return app.state.payment_chain

    @app.get("/api/v2/capabilities")
    def capabilities():
        paused=controls.paused(database)
        blocked=controls.blocked(database)
        return response({"api_version": API_VERSION, "funds_enabled": settings.funds_enabled,
                         "work_enabled": settings.work_enabled and not blocked,"new_work_paused":paused,
                         "work_block_reason":blocked,
                         "work_configured":settings.work_enabled,"settlement_enabled":settings.settlement_enabled,
                         "chain_id":settings.chain_id,"origin":settings.origin,
                         "release_digest":release_digest,"pool_commit":release['pool']['commit'] if release else None,
                         "custody":settings.custody_network.custody if settings.custody_network else None,
                         "token":settings.custody_network.token if settings.custody_network else None,
                         "assignment_unit": "whole-benchmark"})

    def recorded_release():
        if release is None:raise HTTPException(503,'a verified paired release has not been configured')
        return release

    @app.get('/api/v2/release')
    def paired_release():
        return response(recorded_release())

    @app.get('/api/v2/install-worker')
    def installer_download():
        recorded_release()
        return Response(settings.worker_installer,media_type='text/x-python',headers={
            'Content-Disposition':'attachment; filename="install_worker_v2.py"','Cache-Control':'no-store'})

    @app.get('/api/v2/worker-installation')
    def worker_installation(resource:str,compute_type:str,workers:int=1):
        return response(releases.installation(recorded_release(),settings.origin,resource,compute_type,workers))

    @app.post("/api/v2/auth/challenges")
    def challenge(body: WalletChallenge):
        return response(auth.challenge(body.wallet))

    @app.get("/api/v2/artifacts/{digest}")
    def public_algorithm_archive(digest: str):
        if not re.fullmatch(r"[0-9a-f]{64}",digest):raise HTTPException(404,"unknown algorithm archive")
        archive=artifacts.read_archive(database,digest)
        return Response(archive,media_type="application/octet-stream",headers={
            "ETag":'"'+digest+'"',"Cache-Control":"public, max-age=86400, immutable"})

    @app.post("/api/v2/auth/sessions")
    def session(body: WalletSignature):
        return response(auth.verify(body.challenge_id, body.signature))

    @app.post("/api/v2/auth/execution-tokens")
    def execution_token(token=Depends(bearer)):
        return response({"token": auth.issue_execution_token(token), "expires_in": 30 * 86400})

    @app.get('/api/v2/auth/execution-tokens')
    def execution_tokens(member_id=Depends(wallet_principal)):
        with database.transaction() as cursor:
            cursor.execute("""SELECT digest AS id,created_at,expires_at FROM tokens WHERE member_id=%s
                AND kind='execution' AND revoked_at IS NULL AND expires_at>clock_timestamp()
                ORDER BY created_at DESC LIMIT 100""",(member_id,))
            return response({'tokens':cursor.fetchall()})

    @app.post('/api/v2/auth/execution-tokens/{identity}/revoke')
    def revoke_execution_token(identity:str,member_id=Depends(wallet_principal)):
        if not re.fullmatch(r'[0-9a-f]{64}',identity):raise HTTPException(404,'unknown execution token')
        with database.transaction() as cursor:
            cursor.execute("""UPDATE tokens SET revoked_at=coalesce(revoked_at,clock_timestamp())
                WHERE digest=%s AND member_id=%s AND kind='execution' RETURNING digest""",(identity,member_id))
            if not cursor.fetchone():raise HTTPException(404,'unknown execution token')
        return response({'revoked':True})

    @app.post("/api/v2/auth/revoke")
    def revoke(body: RevokeToken, token=Depends(bearer)):
        auth.revoke(token, body.token)
        return response({"revoked": True})

    @app.post('/api/v2/member/withdrawal-wallet/challenges')
    def withdrawal_wallet_challenge(body: WalletChallenge, member_id=Depends(wallet_principal)):
        return response(auth.withdrawal_wallet_challenge(member_id,body.wallet))

    @app.post('/api/v2/member/withdrawal-wallet')
    def change_withdrawal_wallet(body: WalletSignature, member_id=Depends(wallet_principal)):
        return response(auth.verify_withdrawal_wallet(member_id,body.challenge_id,body.signature))

    @app.get("/api/v2/member/balance")
    def balance(member_id=Depends(principal)):
        result = members.balances(database, member_id)
        for key in ("available", "collateral", "pending_withdrawals"):
            result[key] = str(result[key])
        return response(result)

    @app.get('/api/v2/member/dashboard')
    def member_dashboard(limit:int=50,offset:int=0,member_id=Depends(principal)):
        return response(dashboard.member(database,member_id,limit=limit,offset=offset))

    @app.get('/api/v2/operator/dashboard')
    def operator_dashboard(limit:int=50,offset:int=0,actor=Depends(operator)):
        return response(dashboard.operator(database,limit=limit,offset=offset))

    @app.get('/api/v2/operator/custody')
    def custody_dashboard(actor=Depends(operator)):
        state=chain_observer.status(database)
        with database.transaction() as cursor:
            cursor.execute('''SELECT t.* FROM transfers t LEFT JOIN transfer_attributions a ON a.event_id=t.event_id
                LEFT JOIN round_receipts r ON r.event_id=t.event_id JOIN custody_identity c ON c.name='custody'
                WHERE t.recipient=c.wallet AND t.sender<>c.wallet AND a.event_id IS NULL AND r.event_id IS NULL
                ORDER BY t.block_number,t.log_index LIMIT 100''')
            unmatched=cursor.fetchall()
            for row in unmatched:row.pop('evidence',None)
            cursor.execute('SELECT id,kind,details,created_at FROM chain_alerts ORDER BY id DESC LIMIT 50')
            return response({'observation':state,'unattributed':unmatched,'alerts':cursor.fetchall()})

    @app.post('/api/v2/operator/custody/receive-token')
    def receive_token(body:CustodyReceipt,actor=Depends(operator)):
        if body.log_index is None:raise HTTPException(400,'an exact transfer event index is required')
        chain=custody_chain()
        return response({'destination':deposits.receive(database,chain.transfer(body.tx_hash,body.log_index))})

    @app.post('/api/v2/operator/custody/receive-native')
    def receive_native(body:NativeCustodyReceipt,actor=Depends(operator)):
        if body.log_index is not None:raise HTTPException(400,'a native funding transaction has no token event index')
        chain=custody_chain()
        if body.trace_address is None:
            custody.receive_native(database,chain.transaction(body.tx_hash,fee_model=settings.withdrawal_fee_model))
        else:
            receipt=native_funding.verify(chain,app.state.custody_trace_rpc,body.tx_hash,body.trace_address,
                                          fee_model=settings.withdrawal_fee_model)
            native_funding.receive(database,receipt,actor=actor)
        return response({'received':True})

    @app.post('/api/v2/operator/custody/attribute-deposit')
    def attribute_deposit(body:DepositAttribution,actor=Depends(operator)):
        if body.log_index is None:raise HTTPException(400,'an exact transfer event index is required')
        if bool(body.member_wallet)==body.operator_funding:raise HTTPException(400,'choose a verified member wallet or operator funding')
        member_id=None
        if body.member_wallet:
            wallet=members.address(body.member_wallet)
            with database.transaction() as cursor:
                cursor.execute('SELECT id FROM members WHERE wallet=%s',(wallet,))
                member=cursor.fetchone()
            if not member:raise HTTPException(400,'member wallet has not been verified')
            member_id=member['id']
        chain=custody_chain()
        return response({'destination':deposits.attribute_reviewed(database,chain.transfer(body.tx_hash,body.log_index),
            actor=actor,evidence={'operator_review':body.reason},member_id=member_id,operator=body.operator_funding)})

    @app.post('/api/v2/operator/controls/new-work')
    def pause_new_work(body:PauseChange,actor=Depends(operator)):
        return response(controls.set_pause(database,body.paused,actor=actor,reason=body.reason,event_key=body.event_key))

    @app.get('/api/v2/operator/funding')
    def funding_dashboard(actor=Depends(operator)):
        state=funding.status(database)
        policy=None
        if state.get('observation',{}).get('complete'):
            observed=funding.read(database,state['observation']['id'])
            policy={'recipient':observed['recipient'],'minimum':str(observed['minimum'])}
        with database.transaction() as cursor:
            cursor.execute('''SELECT t.id,t.amount,t.fee_limit,t.recipient,t.state,t.sent_at,s.nonce,p.tx_hash,p.fee
                FROM protocol_topups t JOIN custody_sends s ON s.id=t.id
                LEFT JOIN custody_payments p ON p.send_id=t.id
                ORDER BY (t.state IN ('uncertain','awaiting_protocol')) DESC,t.sent_at DESC LIMIT 100''')
            return response({'observation':state,'policy':policy,'topups':cursor.fetchall()})

    @app.get('/api/v2/operator/pilot')
    def pilot_dashboard(actor=Depends(operator)):
        from . import pilot
        return response(pilot.status(database))

    @app.post('/api/v2/operator/topups')
    def prepare_topup(body:TopupRequest,actor=Depends(operator)):
        # Recover the original recorded intent even if new sends or RPC are unavailable.
        with database.transaction() as cursor:
            cursor.execute('SELECT id,amount,fee_limit,actor FROM protocol_topups WHERE request_key=%s',(body.request_key,))
            previous=cursor.fetchone()
        if previous:
            if (int(previous['amount']),int(previous['fee_limit']),previous['actor'])!=(int(body.amount),int(body.fee_limit),actor):
                raise Conflict('top-up request key was reused')
            identity=previous['id']
        else:
            if not settings.funds_enabled:raise HTTPException(503,'new operator payments are paused')
            chain=custody_chain()
            if not settings.pool_player_id or members.address(settings.pool_player_id)!=chain.network.custody:
                raise HTTPException(503,'protocol top-ups require the configured custody wallet as benchmarker')
            state=funding.status(database)
            if not state['ready']:raise Conflict('a fresh reconciled protocol fee observation is required')
            identity=topups.begin(database,body.request_key,chain.preflight(),state['observation']['id'],
                amount=int(body.amount),fee_limit=int(body.fee_limit),fee_model=settings.withdrawal_fee_model,actor=actor)['id']
        value=topups.instructions(database,identity)
        value.pop('preflight',None)
        return response(value)

    @app.get('/api/v2/operator/topups/{identity}')
    def topup_instructions(identity:uuid.UUID,actor=Depends(operator)):
        value=topups.instructions(database,identity)
        value.pop('preflight',None)
        return response(value)

    @app.post('/api/v2/operator/topups/{identity}/reconcile')
    def reconcile_topup(identity:uuid.UUID,body:WithdrawalReconcile,actor=Depends(operator)):
        chain=custody_chain()
        attempt=topups.instructions(database,identity)
        tx_hash=body.tx_hash or chain.find_nonce(int(attempt['nonce']),after_height=attempt['preflight']['block_number'])
        if not tx_hash:return response({'status':'awaiting_final_transaction','id':str(identity)})
        topups.claim(database,identity,tx_hash,actor=actor)
        tx=chain.transaction(tx_hash,fee_model=attempt['fee_model'])
        index=matching_event(chain,tx,attempt,body.log_index)
        token=chain.transfer(tx_hash,index) if index is not None else None
        return response(exact_json(topups.reconcile(database,identity,tx,token)))

    @app.post('/api/v2/operator/topups/{identity}/confirm')
    def confirm_topup(identity:uuid.UUID,actor=Depends(operator)):
        state=funding.status(database)
        if not state.get('observation',{}).get('complete'):
            raise Conflict('complete protocol funding observation is required')
        return response(topups.credit(database,identity,state['observation']['id']))

    def latest_seal(number):
        with database.transaction() as cursor:
            cursor.execute('SELECT id FROM round_report_seals WHERE creation_round=%s ORDER BY created_at DESC LIMIT 1',(number,))
            row=cursor.fetchone()
            if not row:raise Conflict('verified final arbitration evidence is not ready for this round')
            return row['id']

    def exact_json(value):
        if type(value) is int:return str(value)
        if isinstance(value,dict):return {key:exact_json(child) for key,child in value.items()}
        if isinstance(value,(list,tuple)):return [exact_json(child) for child in value]
        return value

    @app.get('/api/v2/operator/rounds/{number}/preview')
    def preview_round(number:int,actor=Depends(operator)):
        value=settlement.preview(database,number,latest_seal(number))
        with database.transaction() as cursor:
            cursor.execute('SELECT id,wallet FROM members WHERE id=ANY(%s::uuid[])',(list(value['member_allocations']),))
            value['member_wallets']={str(row['id']):row['wallet'] for row in cursor.fetchall()}
        return response(exact_json(value))

    @app.post('/api/v2/operator/rounds/{number}/settle')
    def settle_round(number:int,body:SettlementApproval,actor=Depends(operator)):
        if not settings.settlement_enabled:raise HTTPException(503,'verified live settlement adapters are not enabled')
        return response(exact_json(settlement.allocate(database,number,latest_seal(number),expected_digest=body.input_digest)))

    @app.post('/api/v2/operator/collateral/{identity}/finalize')
    def finalize_hold(identity:uuid.UUID,actor=Depends(operator)):
        if not settings.settlement_enabled:raise HTTPException(503,'verified live settlement adapters are not enabled')
        with database.transaction() as cursor:
            cursor.execute('SELECT creation_round FROM reservations WHERE id=%s',(identity,))
            row=cursor.fetchone()
            if not row:raise FundsError('unknown collateral hold')
        return response(settlement.finalize_collateral(database,identity,latest_seal(row['creation_round'])))

    @app.get("/api/v2/member/journal")
    def journal(member_id=Depends(principal), limit: int = 100):
        if not 1 <= limit <= 200:
            raise HTTPException(400, "limit must be between 1 and 200")
        with database.transaction() as cursor:
            cursor.execute("""SELECT j.id,j.kind,j.created_at,e.account_id,e.asset,e.amount
                FROM entries e JOIN journals j ON j.id=e.journal_id
                WHERE e.account_id=%s OR e.account_id IN (
                    SELECT 'collateral:' || id FROM reservations WHERE member_id=%s
                    UNION ALL SELECT 'withdrawal:' || id FROM withdrawals WHERE member_id=%s)
                ORDER BY j.created_at DESC,j.id,e.account_id LIMIT %s""",
                (members.available(member_id), member_id, member_id, limit))
            return response({"entries": cursor.fetchall()})

    @app.post("/api/v2/operator/members/{member_id}/multiplier")
    def change_multiplier(member_id: uuid.UUID, body: MultiplierChange, actor=Depends(operator)):
        return response(members.set_multiplier(database, member_id, body.multiplier,
            actor=actor, reason=body.reason, event_key=body.event_key))

    @app.post("/api/v2/withdrawals")
    def withdraw(body: WithdrawalRequest, member_id=Depends(wallet_principal)):
        if not settings.funds_enabled:
            raise HTTPException(503, "member funds operations are not enabled")
        return response(withdrawals.request(database, member_id, body.request_key, int(body.amount)))

    @app.get('/api/v2/member/withdrawals')
    def member_withdrawals(member_id=Depends(principal)):
        with database.transaction() as cursor:
            cursor.execute('SELECT * FROM withdrawals WHERE member_id=%s ORDER BY created_at DESC LIMIT 100', (member_id,))
            return response({'withdrawals': cursor.fetchall()})

    @app.post('/api/v2/withdrawals/{identity}/cancel')
    def cancel_withdrawal(identity: uuid.UUID, body: WithdrawalRelease, member_id=Depends(wallet_principal)):
        return response(withdrawals.release(database, identity, member_id=member_id, actor='member:'+str(member_id),
            reason=body.reason, event_key=body.event_key))

    @app.get('/api/v2/operator/withdrawals')
    def operator_withdrawals(actor=Depends(operator)):
        with database.transaction() as cursor:
            cursor.execute('''SELECT w.*,m.wallet,r.chain_id,r.token,r.sender,r.fee_model FROM withdrawals w
                JOIN members m ON m.id=w.member_id LEFT JOIN withdrawal_reviews r ON r.withdrawal_id=w.id
                ORDER BY (w.state IN ('requested','approved','uncertain')) DESC,w.created_at DESC LIMIT 200''')
            requests = cursor.fetchall()
            cursor.execute('''SELECT a.id,a.withdrawal_id,a.nonce,a.fee_limit,a.sent_at,o.outcome,o.fee,o.tx_hash
                FROM withdrawal_attempts a LEFT JOIN withdrawal_attempt_outcomes o ON o.attempt_id=a.id
                WHERE a.withdrawal_id=ANY(%s) ORDER BY a.sent_at''', ([row['id'] for row in requests],))
            return response({'withdrawals': requests, 'attempts': cursor.fetchall()})

    @app.post('/api/v2/operator/withdrawals/{identity}/approve')
    def approve_withdrawal(identity: uuid.UUID, body: WithdrawalReview, actor=Depends(operator)):
        if not settings.funds_enabled: raise HTTPException(503, 'new payments are paused')
        chain = custody_chain()
        return response(withdrawals.approve(database, identity, chain.network, fee_model=settings.withdrawal_fee_model,
            actor=actor, evidence={'operator_review': body.reason}))

    @app.post('/api/v2/operator/withdrawals/{identity}/reject')
    def reject_withdrawal(identity: uuid.UUID, body: WithdrawalRelease, actor=Depends(operator)):
        return response(withdrawals.release(database, identity, actor=actor, reason=body.reason, event_key=body.event_key))

    @app.post('/api/v2/operator/withdrawals/{identity}/begin')
    def begin_withdrawal(identity: uuid.UUID, body: WithdrawalSend, actor=Depends(operator)):
        if not settings.funds_enabled: raise HTTPException(503, 'new payments are paused')
        chain = custody_chain()
        # Recovery of an already committed intent does not require an available RPC.
        with database.transaction() as cursor:
            cursor.execute('SELECT id,fee_limit FROM withdrawal_attempts WHERE withdrawal_id=%s AND request_key=%s', (identity,body.request_key))
            prior = cursor.fetchone()
        if prior:
            if int(prior['fee_limit']) != int(body.fee_limit): raise Conflict('send attempt key was reused')
            attempt_id = prior['id']
        else:
            attempt_id = withdrawals.begin(database,identity,body.request_key,chain.preflight(),fee_limit=int(body.fee_limit),actor=actor)['id']
        value = withdrawals.instructions(database,attempt_id)
        value.pop('preflight',None)
        return response(value)

    @app.post('/api/v2/operator/withdrawal-attempts/{identity}/reconcile')
    def reconcile_withdrawal(identity: uuid.UUID, body: WithdrawalReconcile, actor=Depends(operator)):
        chain = custody_chain()
        attempt = withdrawals.instructions(database,identity)
        claims = attempt['claimed_tx_hashes']
        tx_hash = body.tx_hash or (claims[0] if len(claims)==1 else None)
        tx_hash = tx_hash or chain.find_nonce(int(attempt['nonce']),after_height=attempt['preflight']['block_number'])
        if not tx_hash:
            return response({'status':'awaiting_final_transaction','attempt_id':str(identity)})
        withdrawals.claim_transaction(database,identity,tx_hash,actor=actor)
        try:
            tx = chain.transaction(tx_hash,fee_model=attempt['fee_model'])
        except UnsupportedCustodyTransaction as failure:
            if failure.transaction_type != 4:
                raise
            outer = chain.authorization_transaction(tx_hash,fee_model=attempt['fee_model'])
            index = matching_event(chain,outer,attempt,body.log_index)
            if index is None:
                raise FundsError('sponsored payment has no unique matching withdrawal event')
            recovery = sponsored_withdrawals.verify(chain, app.state.custody_trace_rpc,
                tx_hash, index, nonce=int(attempt['nonce']), fee_model=attempt['fee_model'])
            return response(withdrawals.reconcile_sponsored(database,identity,recovery,actor=actor,
                reason='Operator checked the already sent initial EIP-7702 sponsored withdrawal'))
        index = matching_event(chain,tx,attempt,body.log_index)
        token = chain.transfer(tx_hash,index) if index is not None else None
        return response(withdrawals.reconcile(database,identity,tx,token))

    @app.post('/api/v2/operator/withdrawal-attempts/{identity}/recover-sponsored')
    def recover_sponsored_withdrawal(identity: uuid.UUID, body: SponsoredWithdrawalRecovery, actor=Depends(operator)):
        chain = custody_chain()
        attempt = withdrawals.instructions(database, identity)
        recovery = sponsored_withdrawals.verify(chain, app.state.custody_trace_rpc,
            body.tx_hash, body.log_index, nonce=int(attempt['nonce']), fee_model=attempt['fee_model'])
        return response(withdrawals.reconcile_sponsored(database, identity, recovery, actor=actor, reason=body.reason))

    def matching_event(chain,tx,attempt,index):
        if tx.successful and index is None:
            # Recover a lost event index only when one exact verified event matches.
            matches = []
            for log in tx.evidence['receipt']['logs']:
                try:
                    value = chain.transfer(tx.tx_hash,int(log['logIndex'],16))
                    if (value.sender,value.recipient,value.amount)==(attempt['sender'],attempt['recipient'],int(attempt['amount'])):
                        matches.append(value.log_index)
                except FundsError:
                    continue
            if len(matches)>1: raise Conflict('several matching token events require an explicit event index')
            if matches:index=matches[0]
        return index

    @app.get('/api/v2/operator/withdrawal-attempts/{identity}')
    def payment_instructions(identity:uuid.UUID,actor=Depends(operator)):
        value=withdrawals.instructions(database,identity)
        value.pop('preflight',None)
        return response(value)

    @app.post("/api/v2/work-requests", dependencies=[Depends(compatible)])
    def work_request(body: WorkRequest, member_id=Depends(principal)):
        if not settings.work_enabled or controls.blocked(database):
            raise HTTPException(503, "new work is currently disabled")
        return response(work_requests.create(database, member_id, body.request_key,
            resource=body.resource, compute_type=body.compute_type, capacity=body.capacity.model_dump(),
            ttl_seconds=settings.offer_ttl_seconds))

    @app.get("/api/v2/work-requests/{identity}", dependencies=[Depends(compatible)])
    def work_request_status(identity: uuid.UUID, member_id=Depends(principal)):
        return response(work_requests.get(database, identity, member_id))

    @app.post("/api/v2/work-requests/{identity}/refresh", dependencies=[Depends(compatible)])
    def refresh_work_request(identity: uuid.UUID, member_id=Depends(principal)):
        return response(work_requests.refresh(database, identity, member_id, settings.offer_ttl_seconds))

    @app.get("/api/v2/benchmarks/{identity}", dependencies=[Depends(compatible)])
    def benchmark_status(identity: str, member_id=Depends(principal)):
        return response(member_protocol.get(database, identity, member_id))

    @app.post("/api/v2/benchmarks/{identity}/acknowledge", dependencies=[Depends(compatible)])
    def handover(identity: str, body: Acknowledge, member_id=Depends(principal)):
        return response(member_protocol.acknowledge(database, identity, member_id, body.assignment_digest))

    @app.post("/api/v2/benchmarks/{identity}/results", dependencies=[Depends(compatible)])
    def result_upload(identity: str, body: dict, member_id=Depends(principal)):
        return response(member_protocol.results(database, identity, member_id, body))

    @app.post("/api/v2/benchmarks/{identity}/proofs", dependencies=[Depends(compatible)])
    def proof_upload(identity: str, body: dict, member_id=Depends(principal)):
        return response(member_protocol.proofs(database, identity, member_id, body))

    return app
