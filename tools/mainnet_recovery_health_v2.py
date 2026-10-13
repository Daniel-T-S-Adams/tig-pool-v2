#!/usr/bin/env python3
"""Health for a recovery host protecting the primary without serving the pool."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess

from mainnet_backup_health_v2 import backup_health
from mainnet_backup_v2 import atomic_json


def recovery_health(config=Path('/etc/innopool-v2-mainnet'),
                    data=Path('/var/lib/innopool-v2-mainnet')):
    issues = []
    record = {'checked_at_utc': datetime.now(timezone.utc).isoformat(),
        'role': 'recovery', 'independent_collectors_enabled': False,
        'pool_takeover_enabled': False, 'issues': issues}
    try:
        profile = json.loads((config / 'recovery-protection.json').read_text())
        if profile != {'evidence_mode': 'pending', 'independent_collectors': False}:
            raise ValueError('recovery profile requires separate review')
    except (OSError, ValueError, TypeError):
        issues.append('recovery protection profile is missing or invalid')
    record['free_bytes'] = shutil.disk_usage(data).free
    if record['free_bytes'] < 35 * 1024**3:
        issues.append('backup disk reserve is below 35 GiB')
    # Bind-mounted archives can live on a different filesystem from data.
    record['protected_disks'] = {}
    for label, directory in (('database backup', data / 'primary-continuous'),
                             ('pending evidence', data / 'primary-pending-evidence')):
        try:
            free = shutil.disk_usage(directory).free
            record['protected_disks'][label] = {'path': str(directory), 'free_bytes': free}
            if free < 35 * 1024**3:
                issues.append(label + ' disk reserve is below 35 GiB')
        except OSError:
            issues.append(label + ' storage is unavailable')
    services = {}
    for name in ('api', 'coordinator', 'custody', 'funding', 'blocks', 'reports'):
        unit = 'innopoolv2mainnet-' + name + '.service'
        state = subprocess.run(['systemctl', 'is-active', unit],
            capture_output=True, text=True, timeout=10).stdout.strip()
        services[name] = state
        if state not in ('inactive', 'failed', 'unknown'):
            issues.append(unit + ' is unexpectedly serving or collecting on the recovery host')
    record['pool_services'] = services
    try:
        record['backup_protection'] = backup_health('recovery')
        issues.extend(record['backup_protection']['issues'])
    except Exception as error:
        issues.append('backup protection check failed: ' + type(error).__name__)
    record['healthy'] = not issues
    return record


if __name__ == '__main__':
    if os.geteuid() != 0:
        raise SystemExit('run as root on the designated recovery host')
    os.umask(0o077)
    record = recovery_health()
    root = Path('/var/lib/innopool-v2-mainnet/health')
    root.mkdir(mode=0o700, exist_ok=True)
    atomic_json(root / 'status.json', record)
    print(json.dumps(record))
    raise SystemExit(0 if record['healthy'] else 1)
