"""Public website content and explicitly configured HTTPS origins."""

import hashlib
from html import escape
from pathlib import Path
import re
from urllib.parse import urlsplit

from fastapi import HTTPException
from fastapi.responses import HTMLResponse, Response


def https_origin(value):
    # Origins enter HTML, CORS and CSP headers. Accept only an authority, never
    # userinfo, paths, header delimiters or a policy supplied through a Host header.
    if not isinstance(value, str) or not re.fullmatch(
            r'https://(?:[A-Za-z0-9.-]+|\[[0-9A-Fa-f:]+\])(?::[0-9]{1,5})?/?', value):
        raise ValueError('website and API origins must be explicit HTTPS origins')
    parsed = urlsplit(value)
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        raise ValueError('invalid HTTPS origin port')
    return value.rstrip('/')


class Frontend:
    def __init__(self, api_origin, directory=None):
        directory = directory or Path(__file__).with_name('web')
        self.assets = {}
        template = (directory / 'index.html').read_text()
        template = template.replace('__POOL_V2_API_ORIGIN__', escape(api_origin, quote=True))
        for name, media_type in (('app.js', 'text/javascript'), ('style.css', 'text/css')):
            content = (directory / name).read_bytes()
            digest = hashlib.sha256(content).hexdigest()
            self.assets[name] = (digest, content, media_type)
            template = template.replace('/assets/' + name, '/assets/' + digest + '/' + name)
        self.html = template

    def page(self):
        # This public shell contains no account data. Fetch it afresh so the
        # browser always learns the current API origin and asset hashes.
        return HTMLResponse(self.html, headers={'Cache-Control': 'no-store'})

    def asset(self, name, digest=None):
        value = self.assets.get(name)
        if value is None or (digest is not None and digest != value[0]):
            raise HTTPException(404, 'unknown website asset')
        return Response(value[1], media_type=value[2], headers={
            'Cache-Control': 'public, max-age=31536000, immutable' if digest else 'no-store',
            'ETag': '"' + value[0] + '"'})
