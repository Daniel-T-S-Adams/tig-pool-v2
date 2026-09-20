"""One durable submission cycle; observer, API and this service stay separate."""

from datetime import datetime,timezone
import io
import json
import re
import tarfile
import time
from urllib.parse import urlsplit
from urllib.request import Request,build_opener

from . import controls,submissions,work_requests
from .artifacts import ArtifactRedirect,DEFAULT_HOSTS
from .money import Conflict,FundsError
from .protocol import ProtocolDataError,_index
from .reconciliation import reconcile_block,reconcile_pending


def download_archive(url, *, allowed_hosts):
    parsed=urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in allowed_hosts or parsed.username or parsed.password or parsed.fragment:
        raise FundsError("selected binary URL is not an allowed HTTPS artifact source")
    with build_opener(ArtifactRedirect(allowed_hosts)).open(Request(url,headers={"User-Agent":"innopool-v2-artifact/2.0"}),timeout=30) as response:
        archive=response.read(64*1024*1024+1)
    if not archive or len(archive)>64*1024*1024:
        raise ProtocolDataError("algorithm archive exceeds the configured limit")
    return archive


def verify_archive(archive, algorithm_name, compute_type):
    if not isinstance(algorithm_name,str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}",algorithm_name):
        raise ProtocolDataError("invalid algorithm library name")
    arch="arm64" if compute_type in ("aws_t4g","aws_c7g","aws_m7g") else "amd64"
    required={f"{arch}/{algorithm_name}.so"}
    if compute_type=="aws_g4dn":required.add(f"ptx/{algorithm_name}.ptx")
    found,total=set(),0
    try:
        with tarfile.open(fileobj=io.BytesIO(archive),mode="r|gz") as source:
            for item in source:
                total+=item.size
                if total>256*1024*1024:raise ProtocolDataError("algorithm archive expands beyond configured limit")
                if item.name in required:
                    if not item.isfile() or item.size<=0 or item.size>64*1024*1024 or item.name in found:
                        raise ProtocolDataError("invalid algorithm library entry")
                    found.add(item.name)
        if found != required:raise ProtocolDataError("algorithm archive lacks required architecture library/PTX")
    except (tarfile.TarError,EOFError,OSError) as exc:
        raise ProtocolDataError("invalid algorithm archive") from exc


class Coordinator:
    def __init__(self,database,player_id,public_client,submission_client,*,new_work=False,binary_hosts=None,download=None,max_age=120,artifact_origin=None):
        self.database,self.player_id=database,player_id
        self.public,self.writer=public_client,submission_client
        self.new_work,self.max_age=new_work,max_age
        self.binary_hosts=binary_hosts or DEFAULT_HOSTS
        self.artifact_origin=artifact_origin
        self.download=download or (lambda url:download_archive(url,allowed_hosts=self.binary_hosts))

    def preflight(self,row):
        start=self.public.get("/get-block")
        block=start["block"]
        if block["id"] != row["payload"]["settings"]["block_id"]:
            return None,{"reason":"new-current-block","block_id":block["id"]}
        feed=self.public.get("/get-benchmarks",{"block_id":block["id"],"player_id":self.player_id})
        seen=_index(feed["precommits"],"benchmark_id","preflight precommits")
        if any(value["settings"]["player_id"] != self.player_id for value in seen.values()):
            raise ProtocolDataError("preflight contains another player's precommit")
        end=self.public.get("/get-block")
        if end["block"]["id"] != block["id"]:
            return None,{"reason":"block-changed-during-preflight","block_id":end["block"]["id"]}
        now=int(time.time())
        if not -5 <= now-block["details"]["timestamp"] <= self.max_age:
            raise ProtocolDataError("current protocol block is stale")
        return {"block_id":block["id"],"height":block["details"]["height"],
                "observed_at":now,"seen_benchmarks":sorted(seen)},None

    def dispatch_one(self):
        # Do not mark a request potentially sent if writes are closed. Recovery
        # is still allowed independently through archived observations.
        with self.database.transaction() as cursor:
            cursor.execute("""SELECT o.*,r.offer_expires_at,r.selection FROM protocol_outbox o JOIN reservations r ON r.id=o.reservation_id
                WHERE o.state='ready'
                ORDER BY CASE o.kind WHEN 'proofs' THEN 0 WHEN 'results' THEN 1 ELSE 2 END,o.created_at,o.id LIMIT 50""")
            ready=cursor.fetchall()
        for intent in ready:
            preflight=None
            if intent["kind"]=="precommit":
                work_allowed=self.new_work and not controls.blocked(self.database)
                if not work_allowed or intent["offer_expires_at"]<=datetime.now(timezone.utc):
                    reason="new-work-paused" if not work_allowed else "offer-expired-before-send"
                    submissions.cancel_unsent(self.database,intent["id"],evidence={"reason":reason})
                    continue
                if not self.writer.enabled:continue
                with self.database.transaction() as cursor:
                    cursor.execute("SELECT 1 FROM reservation_archives WHERE reservation_id=%s",(intent["reservation_id"],))
                    archived=cursor.fetchone()
                if not archived:
                    url=intent["selection"]["binary"]["details"]["download_url"]
                    archive=self.download(url)
                    verify_archive(archive,intent["selection"]["algorithm_name"],intent["payload"]["compute_type"])
                    submissions.prepare_archive(self.database,intent["reservation_id"],url,archive)
                preflight,cancellation=self.preflight(intent)
                if cancellation:
                    submissions.cancel_unsent(self.database,intent["id"],evidence=cancellation)
                    continue
            elif submissions.retire_unsent_payload(self.database,intent["id"]):
                continue
            if not self.writer.enabled:continue
            try:
                begun=submissions.begin(self.database,intent["id"],preflight=preflight)
            except Conflict:
                continue
            try:
                response=self.writer.post(begun)
            except Exception as error:
                # Preserve that the response was unavailable. Do not include
                # exception text that a transport might fill with headers.
                submissions.record_response(self.database,intent["id"],{"status":None,"error_type":type(error).__name__})
                raise
            submissions.record_response(self.database,intent["id"],response)
            return intent["id"]
        return None

    def step(self):
        with self.database.transaction() as cursor:
            cursor.execute("SELECT id FROM observed_blocks ORDER BY height DESC LIMIT 1")
            latest=cursor.fetchone()
        if not latest:raise ProtocolDataError("no complete block is available")
        replay=reconcile_pending(self.database,self.player_id,artifact_origin=self.artifact_origin)
        # A recent receipt may have arrived after this block's first replay.
        reconcile_block(self.database,latest["id"],self.player_id,artifact_origin=self.artifact_origin)
        submitted=self.dispatch_one()
        reserved=None
        if self.new_work and self.writer.enabled and not controls.blocked(self.database):
            reserved=work_requests.reserve_next(self.database,self.player_id,now=int(time.time()),max_age=self.max_age)
        return {"submitted":str(submitted) if submitted else None,"reserved":str(reserved["id"]) if reserved else None,
                "reconciliation":replay}
