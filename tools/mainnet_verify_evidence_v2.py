#!/usr/bin/env python3
"""Verify every retained evidence chunk once and every manifest reference.

Run after a successful evidence copy. A concurrent later copy can only append;
only manifests present at the start of each pass are claimed as verified.
"""
from datetime import datetime, timezone
import fcntl
import gzip
import hashlib
import json
import os
from pathlib import Path
import re

from mainnet_backup_v2 import atomic_json


def verify(root):
    result = {}
    # Select both manifest sets between complete copy attempts. Do not select
    # newly arriving manifests while their chunks are still being transferred.
    with (root / 'sync.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        selected = {kind: list((root / kind / 'recorded').glob('*.json.gz'))
                    for kind in ('blocks', 'reports')}
    for kind in ('blocks', 'reports'):
        directory = root / kind
        manifests = selected[kind]
        verified = set()
        for path in (directory / 'chunks').glob('*/*.gz'):
            expected = path.name[:-3]
            if path.is_symlink() or not re.fullmatch('[0-9a-f]{64}', expected):
                raise ValueError('unsafe evidence chunk name')
            digest = hashlib.sha256()
            with gzip.open(path, 'rb') as stream:
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
            if digest.hexdigest() != expected or path.parent.name != expected[:2]:
                raise ValueError('evidence chunk checksum mismatch')
            verified.add(expected)

        def references(node):
            if not isinstance(node, dict):
                raise ValueError('invalid manifest node')
            if 'dict' in node:
                for _, child in node['dict']:
                    references(child)
            elif 'records' in node:
                if any(value not in verified for value in node['records']):
                    raise ValueError('manifest references missing or unverified evidence')
            elif 'value' not in node:
                raise ValueError('unknown manifest node')

        for path in manifests:
            with gzip.open(path, 'rt') as stream:
                saved = json.load(stream)
            references(saved['manifest'])
        result[kind] = {'manifests': len(manifests), 'verified_chunks': len(verified)}
    record = {'completed_at_utc': datetime.now(timezone.utc).isoformat(),
              'archive': str(root), 'all_selected_manifest_references_verified': True,
              'all_selected_content_digests_verified': True, 'counts': result}
    atomic_json(root / 'verification.json', record)
    return record


if __name__ == '__main__':
    os.umask(0o077)
    print(json.dumps(verify(Path('/var/lib/innopool-v2-mainnet/primary-evidence'))), flush=True)
