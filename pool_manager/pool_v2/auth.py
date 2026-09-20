"""One-use wallet challenges and separately scoped, revocable opaque tokens."""

from datetime import timedelta
import hashlib
import secrets
from urllib.parse import urlsplit
import uuid

from eth_account import Account
from eth_account.messages import encode_defunct
from eth_utils import to_checksum_address

from .members import address, register_verified


class AuthenticationError(ValueError):
    pass


def token_digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


class Auth:
    def __init__(self, database, *, origin, chain_id):
        parsed = urlsplit(origin)
        if (parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password
                or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
            raise ValueError("wallet authentication requires the configured HTTPS origin")
        if type(chain_id) is not int or chain_id <= 0:
            raise ValueError("explicit verified chain ID required")
        self.database, self.origin, self.domain, self.chain_id = database, origin.rstrip("/"), parsed.netloc, chain_id

    def challenge(self, wallet):
        wallet = address(wallet)
        identity = uuid.uuid4()
        nonce = secrets.token_hex(24)
        with self.database.transaction() as cursor:
            cursor.execute("SELECT clock_timestamp() AS now")
            now = cursor.fetchone()["now"]
            expiry = now + timedelta(minutes=5)
            # ERC-4361/EIP-191 message, constructed by the server, not the client.
            message = (f"{self.domain} wants you to sign in with your Ethereum account:\n"
                       f"{to_checksum_address(wallet)}\n\n"
                       "Sign in to InnoPool v2 to manage your member funds and execution tokens.\n\n"
                       f"URI: {self.origin}\nVersion: 1\nChain ID: {self.chain_id}\nNonce: {nonce}\n"
                       f"Issued At: {now.isoformat()}\nExpiration Time: {expiry.isoformat()}\nRequest ID: {identity}")
            cursor.execute("INSERT INTO auth_challenges(id,wallet,message,expires_at) VALUES (%s,%s,%s,%s)",
                           (identity, wallet, message, expiry))
        return {"id": str(identity), "message": message, "expires_at": expiry}

    def verify(self, challenge_id, signature):
        with self.database.transaction() as cursor:
            cursor.execute("""SELECT * FROM auth_challenges WHERE id=%s AND used_at IS NULL
                AND expires_at > clock_timestamp() FOR UPDATE""", (challenge_id,))
            challenge = cursor.fetchone()
            if not challenge:
                raise AuthenticationError("challenge expired, consumed, or unknown")
            # An old deployment origin/network cannot issue sessions after a config change.
            if (not challenge["message"].startswith(self.domain + " wants you to sign in")
                    or f"\nURI: {self.origin}\nVersion: 1\nChain ID: {self.chain_id}\n" not in challenge["message"]):
                raise AuthenticationError("challenge belongs to another deployment")
            try:
                signer = Account.recover_message(encode_defunct(text=challenge["message"]), signature=signature)
            except (ValueError, TypeError) as exc:
                raise AuthenticationError("invalid wallet signature") from exc
            if signer.lower() != challenge["wallet"]:
                raise AuthenticationError("signature does not match the requested wallet")
            member = register_verified(cursor, signer)
            cursor.execute("UPDATE auth_challenges SET used_at=clock_timestamp() WHERE id=%s", (challenge_id,))
            token = self._issue(cursor, member["id"], "wallet", timedelta(minutes=15))
            return {"member_id": str(member["id"]), "token": token, "expires_in": 900}

    def _issue(self, cursor, member_id, kind, lifetime):
        token = secrets.token_urlsafe(32)
        cursor.execute("INSERT INTO tokens(digest,member_id,kind,expires_at) VALUES (%s,%s,%s,clock_timestamp()+%s)",
                       (token_digest(token), member_id, kind, lifetime))
        return token

    def authenticate(self, cursor, token, *, wallet=False):
        if not isinstance(token, str) or not 32 <= len(token) <= 128:
            raise AuthenticationError("invalid token")
        cursor.execute("""SELECT member_id,kind FROM tokens WHERE digest=%s AND revoked_at IS NULL
            AND expires_at>clock_timestamp() FOR SHARE""", (token_digest(token),))
        principal = cursor.fetchone()
        if not principal or (wallet and principal["kind"] != "wallet"):
            raise AuthenticationError("a valid wallet session is required" if wallet else "invalid or expired token")
        return principal["member_id"]

    def issue_execution_token(self, wallet_token):
        with self.database.transaction() as cursor:
            member_id = self.authenticate(cursor, wallet_token, wallet=True)
            return self._issue(cursor, member_id, "execution", timedelta(days=30))

    def revoke(self, wallet_token, token_to_revoke):
        with self.database.transaction() as cursor:
            member_id = self.authenticate(cursor, wallet_token, wallet=True)
            cursor.execute("UPDATE tokens SET revoked_at=clock_timestamp() WHERE digest=%s AND member_id=%s",
                           (token_digest(token_to_revoke), member_id))
