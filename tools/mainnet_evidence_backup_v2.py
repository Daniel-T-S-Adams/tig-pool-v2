#!/usr/bin/env python3
"""Append primary evidence to the independent recovery archive via restricted SSH.

Files are immutable; pending -> recorded renames do not remove retained copies.
No delete option is used. The forced SSH command permits reads only of spools.
"""
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess

from mainnet_backup_v2 import atomic_json


def main():
    if os.geteuid() != 0:
        raise SystemExit('run as root on the designated recovery host')
    os.umask(0o077)
    root = Path('/var/lib/innopool-v2-mainnet/primary-evidence')
    root.mkdir(mode=0o700, exist_ok=True)
    with (root / 'sync.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if shutil.disk_usage(root).free < 35 * 1024**3:
            raise RuntimeError('disk reserve too low; existing evidence retained')
        ssh = ('ssh -F /dev/null -i /etc/innopool-v2-mainnet/evidence-pull-ssh '
               '-o IdentitiesOnly=yes -o BatchMode=yes -o ForwardAgent=no '
               '-o StrictHostKeyChecking=yes '
               '-o UserKnownHostsFile=/etc/innopool-v2-mainnet/backup-known-hosts '
               '-o ConnectTimeout=10 -o ServerAliveInterval=15 -o ServerAliveCountMax=3')
        # Capture manifests first, then their immutable chunks. A manifest that
        # moves during listing is caught by the recorded pass or the next poll.
        # New chunks always precede manifest publication in the collector.
        counts = {}
        for kind in ('blocks', 'reports'):
            for part in ('pending', 'recorded', 'chunks'):
                destination = root / kind / ('chunks' if part == 'chunks' else 'recorded')
                destination.mkdir(parents=True, mode=0o700, exist_ok=True)
                command = ['rsync', '-rlt', '--ignore-existing', '--no-owner', '--no-group',
                    '--chmod=D700,F600', '--exclude=.writing-*', '--exclude=*.pending',
                    '--include=*/', '--include=*.gz', '--exclude=*',
                    '--timeout=120', '--bwlimit=16384', '-e', ssh,
                    f'root@46.62.249.188:{kind}/{part}/', str(destination) + '/']
                result = subprocess.run(command, timeout=7200, capture_output=True, text=True)
                if result.returncode not in ((0, 24) if part != 'chunks' else (0,)):
                    raise RuntimeError('evidence copy failed; rsync exit ' + str(result.returncode))
            counts[kind] = sum(1 for _ in (root / kind / 'recorded').glob('*.json.gz'))
        record = {'completed_at_utc': datetime.now(timezone.utc).isoformat(),
            'source_host': '46.62.249.188', 'recovery_host': '2.28.230.81',
            'manifests_retained': counts, 'deletion_enabled': False,
            'transfer_completed': True, 'full_content_audit_completed': False}
        atomic_json(root / 'status.json', record)
        print(json.dumps(record), flush=True)


if __name__ == '__main__':
    main()
