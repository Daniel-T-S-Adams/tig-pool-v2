"""Public algorithm archives: restricted redirects and content-addressed reads."""

import hashlib
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler,Request

from .money import FundsError


DEFAULT_HOSTS={"mainnet-api.tig.foundation","media.githubusercontent.com"}


class ArtifactRedirect(HTTPRedirectHandler):
    def __init__(self,allowed_hosts):
        self.allowed_hosts=set(allowed_hosts)

    def redirect_request(self,req,fp,code,msg,headers,newurl):
        parsed=urlsplit(newurl)
        if (req.get_method() != "GET" or parsed.scheme != "https" or parsed.hostname not in self.allowed_hosts
                or parsed.username or parsed.password or parsed.fragment
                or (parsed.hostname=="media.githubusercontent.com" and not parsed.path.startswith('/media/tig-foundation/tig-monorepo/'))):
            raise FundsError("algorithm redirect is outside the configured public artifact sources")
        # Only public artifact GETs use this handler. Never forward credentials
        # or other headers through redirects, including to a permitted host.
        return Request(newurl,headers={"User-Agent":"innopool-v2-artifact/2.0"},method="GET")


def read_archive(database,digest):
    with database.transaction() as cursor:
        cursor.execute("SELECT archive FROM algorithm_archives WHERE sha256=%s",(digest,))
        row=cursor.fetchone()
        if not row:raise FundsError("unknown algorithm archive")
        archive=bytes(row["archive"])
    if hashlib.sha256(archive).hexdigest() != digest:
        raise FundsError("stored algorithm archive failed its checksum")
    return archive
