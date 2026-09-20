"""Explicitly enabled TIG benchmark writes; no transfers, wallet or key creation."""

from datetime import datetime, timezone
import hashlib
import json
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .money import FundsError


PATHS = {"precommit": "/submit-precommit", "results": "/submit-benchmark", "proofs": "/submit-proof"}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise FundsError("TIG write endpoint redirected; retain uncertainty and check configuration")


class TigSubmissionClient:
    def __init__(self, base_url, api_key, *, enabled=False, timeout=20):
        parsed = urlsplit(base_url)
        if (parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password
                or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
            raise FundsError("submission service requires an explicit HTTPS TIG origin")
        self.origin, self._key, self.enabled, self.timeout = base_url.rstrip("/"), api_key, enabled, timeout
        self.opener = build_opener(NoRedirect())

    def post(self, intent):
        if not self.enabled or not self._key:
            raise FundsError("TIG submissions are disabled or not configured")
        if intent.get("state") != "uncertain" or not intent.get("sent_at"):
            raise FundsError("persist the potentially-sent marker before calling TIG")
        kind = intent.get("kind")
        if kind not in PATHS:
            raise FundsError("only benchmark precommit, result and proof submissions are supported")
        encoded = intent["payload_text"].encode()
        if hashlib.sha256(encoded).hexdigest() != intent["digest"]:
            raise FundsError("serialized submission differs from its durable digest")
        request = Request(self.origin+PATHS[kind], data=encoded, method="POST", headers={
            "Content-Type": "application/json", "Accept": "application/json",
            "X-Api-Key": self._key, "User-Agent": "innopool-v2-submitter/2.0"})
        try:
            response = self.opener.open(request, timeout=self.timeout)
        except HTTPError as error:
            response = error
        with response:
            raw = response.read(2*1024*1024+1)
            if len(raw) > 2*1024*1024:
                raise FundsError("TIG response exceeds configured limit; reconcile the submission")
            status = response.code
        body = raw.decode("utf-8", errors="replace")
        try:
            body = json.loads(body)
        except ValueError:
            pass
        # Authentication headers are never part of durable/logged evidence.
        return {"status": status, "body": body, "path": PATHS[kind], "origin": self.origin,
                "raw_sha256": hashlib.sha256(raw).hexdigest(), "received_at": datetime.now(timezone.utc).isoformat()}
