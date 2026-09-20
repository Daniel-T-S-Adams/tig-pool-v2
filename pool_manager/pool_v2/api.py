"""Isolated v2 API factory. Importing it does not start legacy pool processes."""

from dataclasses import dataclass
from decimal import Decimal
import hashlib
import secrets
import uuid

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictStr

from .auth import Auth, AuthenticationError
from .database import Database
from . import members, withdrawals
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


class RevokeToken(Input):
    token: StrictStr = Field(min_length=32, max_length=128)


def response(value):
    return JSONResponse(jsonable_encoder(value, custom_encoder={Decimal: str}),
                        headers={"Cache-Control": "no-store"})


def create_app(settings):
    if len(settings.operator_token_sha256) != 64:
        raise ValueError("operator token SHA-256 must be configured explicitly")
    database = Database(settings.database_dsn)
    auth = Auth(database, origin=settings.origin, chain_id=settings.chain_id)
    app = FastAPI(title="InnoPool v2", version=API_VERSION)
    app.state.database, app.state.auth = database, auth

    @app.exception_handler(FundsError)
    async def funds_error(request, exc):
        code = 409 if isinstance(exc, (Conflict, InsufficientFunds)) else 400
        return JSONResponse({"detail": str(exc)}, status_code=code, headers={"Cache-Control": "no-store"})

    @app.exception_handler(AuthenticationError)
    async def authentication_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=401, headers={"Cache-Control": "no-store"})

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

    @app.get("/api/v2/capabilities")
    def capabilities():
        return response({"api_version": API_VERSION, "funds_enabled": settings.funds_enabled,
                         "work_enabled": False, "assignment_unit": "whole-benchmark"})

    @app.post("/api/v2/auth/challenges")
    def challenge(body: WalletChallenge):
        return response(auth.challenge(body.wallet))

    @app.post("/api/v2/auth/sessions")
    def session(body: WalletSignature):
        return response(auth.verify(body.challenge_id, body.signature))

    @app.post("/api/v2/auth/execution-tokens")
    def execution_token(token=Depends(bearer)):
        return response({"token": auth.issue_execution_token(token), "expires_in": 30 * 86400})

    @app.post("/api/v2/auth/revoke")
    def revoke(body: RevokeToken, token=Depends(bearer)):
        auth.revoke(token, body.token)
        return response({"revoked": True})

    @app.get("/api/v2/member/balance")
    def balance(member_id=Depends(principal)):
        result = members.balances(database, member_id)
        for key in ("available", "collateral", "pending_withdrawals"):
            result[key] = str(result[key])
        return response(result)

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

    return app
