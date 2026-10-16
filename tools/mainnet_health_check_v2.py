"""Local preparation health record and disk guard for a mainnet host; no external notifications.

Installed as /opt/innopool-v2-mainnet/operations/mainnet_health_check.py and run by
innopoolv2mainnet-health.service every five minutes. Block history is judged by
the deployed missing-block tolerance: rounds held because their missing blocks
exceed the policy, a current round that already exceeds it, and unresolved
conflicting heights are issues; tolerated or waived gaps are recorded only.
"""
from datetime import datetime, timezone
from pathlib import Path
import json
import os
import shutil
import subprocess
import sys
import time

sys.dont_write_bytecode = True
BASE = Path('/opt/innopool-v2-mainnet')
CONFIG = Path('/etc/innopool-v2-mainnet')
DATA = Path('/var/lib/innopool-v2-mainnet')
DISK_RESERVE = 25 * 1024 ** 3
FRESHNESS_SECONDS = 180


def block_issues(status, now):
    """Issues raised by the block observation status under the tolerance policy."""
    issues = []
    if not status.get('initialized', True):
        return ['block observation is not initialized']
    if 'missing_by_round' not in status:
        # Older application releases report no per-round tolerance; keep the strict rule.
        unresolved = status.get('unresolved_missing_heights', status.get('missing_heights', []))
        if unresolved:
            issues.append('block history has unresolved gaps')
    held = status.get('held_rounds') or []
    if held:
        issues.append('rounds held: missing blocks exceed the tolerance: ' + ', '.join(map(str, held)))
    limit = (status.get('missing_block_policy') or {}).get('max_missing_per_round')
    current = [row['round'] for row in status.get('missing_by_round', [])
               if row.get('state') == 'open' and limit is not None and row.get('missing', 0) > limit]
    if current:
        issues.append('current round already exceeds the missing-block tolerance: ' + ', '.join(map(str, current)))
    if status.get('conflicting_heights'):
        issues.append('block history has unresolved conflicting heights: ' + ', '.join(map(str, status['conflicting_heights'])))
    if not status.get('latest_timestamp') or now - status['latest_timestamp'] > FRESHNESS_SECONDS:
        issues.append('protocol observation is more than three minutes behind')
    return issues


def application_directory():
    application = Path(subprocess.check_output(['systemctl', 'show', 'innopoolv2mainnet-api.service', '--value',
                                                '-p', 'WorkingDirectory'], text=True).strip())
    assert application.parent == BASE / 'releases'
    assert len(application.name) == 40 and all(x in '0123456789abcdef' for x in application.name)
    assert (application / 'BUILD_COMMIT').read_text().strip() == application.name
    return application


def spending_issues(status):
    """Pure rule: no cap, or a window more than 90% committed, needs the operator's attention."""
    if not status['configured']:
        return ['no spending cap is recorded; the pool commits no fees without one']
    issues = []
    for asset, used, limit in (('TIG fee', status['window']['tig_fee_units'], status['cap']['tig_fee_window_units']),
                               ('custody gas', status['window']['native_fee_units'], status['cap']['native_fee_window_units'])):
        if int(limit) and (int(limit) - int(used)) * 10 < int(limit):
            issues.append(asset + ' spending cap window is more than 90% committed')
    return issues


def main():
    sys.path.insert(0, str(application_directory()))
    from pool_manager.pool_v2.database import Database
    from pool_manager.pool_v2.block_observer import BlockStore
    from pool_manager.pool_v2 import controls, ledger, spending
    os.umask(0o077)
    deployment = json.loads((CONFIG / 'deployment.json').read_text())
    role = deployment['role']
    free = shutil.disk_usage(DATA).free
    issues = []
    if free < DISK_RESERVE:
        # These are unfunded preparation collectors. Preserve all existing evidence
        # and the OS reserve instead of consuming the last free disk space.
        subprocess.run(['systemctl', 'stop', 'innopoolv2mainnet-blocks.service',
                        'innopoolv2mainnet-reports.service'], check=True, timeout=90)
        issues.append('disk reserve below 25 GiB; preparation collectors stopped; expand storage before resuming')
    names = ['db', 'blocks', 'reports'] + (['api'] if role == 'primary' else [])
    services = {}
    for name in names:
        result = subprocess.run(['systemctl', 'is-active', f'innopoolv2mainnet-{name}.service'],
                                capture_output=True, text=True, timeout=10)
        services[name] = result.stdout.strip()
        if services[name] != 'active':
            issues.append(f'{name} service is not active')
    record = {'checked_at_utc': datetime.now(timezone.utc).isoformat(), 'role': role,
              'free_disk_bytes': free, 'disk_reserve_bytes': DISK_RESERVE,
              'services': services, 'issues': issues, 'external_notifications_configured': False}
    try:
        database = Database((CONFIG / 'observer-dsn').read_text().strip())
        record['blocks'] = BlockStore(database).status()
        record['new_work_paused'] = controls.paused(database)
        record['spending'] = spending.status(database)
        issues.extend(spending_issues(record['spending']))
        with database.transaction() as cursor:
            record['ledger_audit'] = ledger.audit(cursor)
        if not record['new_work_paused']:
            issues.append('preparation database no longer has the recorded work pause')
        if record['ledger_audit']:
            issues.append('ledger audit requires attention')
        issues.extend(block_issues(record['blocks'], time.time()))
    except Exception as error:
        issues.append('database health check failed: ' + type(error).__name__)
    if role == 'primary' and (deployment.get('origin_https_activated') or (CONFIG / 'tls/cloudflare-origin.pem').exists()):
        record['origin_tls'] = {'expiry_warning_days': 60, 'certificates': {}}
        for name in ('cloudflare-origin.pem', 'cloudflare-origin-rsa-root.pem'):
            try:
                certificate = CONFIG / 'tls' / name
                result = subprocess.run(['openssl', 'x509', '-in', str(certificate), '-noout', '-enddate'],
                                        check=True, capture_output=True, text=True, timeout=10)
                expires = datetime.strptime(result.stdout.strip().split('=', 1)[1], '%b %d %H:%M:%S %Y %Z').replace(tzinfo=timezone.utc)
                remaining_days = (expires - datetime.now(timezone.utc)).total_seconds() / 86400
                record['origin_tls']['certificates'][name] = {'expires_at_utc': expires.isoformat(),
                                                              'remaining_days': int(remaining_days)}
                if remaining_days < 60:
                    issues.append('origin TLS certificate needs renewal or trust-root refresh: ' + name)
            except Exception as error:
                issues.append('origin TLS certificate check failed for ' + name + ': ' + type(error).__name__)
    for kind in ('blocks', 'reports'):
        record[kind + '_pending_manifests'] = len(list((DATA / 'spool' / kind / 'pending').glob('*.json.gz')))
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from mainnet_backup_health_v2 import backup_health
    try:
        record['backup_protection'] = backup_health(role)
        issues.extend(record['backup_protection']['issues'])
    except Exception as error:
        issues.append('backup health check failed: ' + type(error).__name__)
    directory = DATA / 'health'
    directory.mkdir(mode=0o700, exist_ok=True)
    temporary = directory / 'status.json.pending'
    temporary.write_text(json.dumps(record, indent=2, default=str) + '\n')
    os.replace(temporary, directory / 'status.json')
    print(json.dumps(record, default=str), flush=True)
    raise SystemExit(1 if issues else 0)


if __name__ == '__main__':
    main()
