"""Public algorithm archives: restricted redirects and content-addressed reads."""

import hashlib
from urllib.parse import unquote,urlsplit
from urllib.request import HTTPRedirectHandler,Request

from .money import FundsError


DEFAULT_HOSTS={"mainnet-api.tig.foundation","media.githubusercontent.com"}


def check_url(url,allowed_hosts):
    try:
        parsed=urlsplit(url)
        port=parsed.port
    except ValueError as failure:
        raise FundsError('algorithm URL is malformed') from failure
    if (parsed.scheme != "https" or parsed.hostname not in allowed_hosts
            or parsed.username or parsed.password or parsed.fragment or port not in (None,443)
            or (parsed.hostname=="media.githubusercontent.com" and (
                not parsed.path.startswith('/media/tig-foundation/tig-monorepo/')
                or '\\' in unquote(parsed.path)
                or any(part in ('.','..') for part in unquote(parsed.path).split('/'))))):
        raise FundsError("algorithm URL is outside the configured public HTTPS artifact sources")


class ArtifactRedirect(HTTPRedirectHandler):
    def __init__(self,allowed_hosts):
        self.allowed_hosts=set(allowed_hosts)

    def redirect_request(self,req,fp,code,msg,headers,newurl):
        if req.get_method() != "GET":
            raise FundsError("algorithm redirect is outside the configured public artifact sources")
        check_url(newurl,self.allowed_hosts)
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
