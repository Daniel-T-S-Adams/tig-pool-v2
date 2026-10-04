"""Health checks for the dedicated two-host backup installation; no auto-failover."""
from datetime import datetime, timezone
import grp
import json
import os
from pathlib import Path
import subprocess

from mainnet_backup_v2 import atomic_json


def backup_health(role):
    config = Path('/etc/innopool-v2-mainnet')
    data = Path('/var/lib/innopool-v2-mainnet')
    now = datetime.now(timezone.utc)
    result = {'checked_at_utc': now.isoformat(), 'issues': [],
              'primary_commits_wait_for_recovery': False}
    issues = result['issues']

    def age_file(path, field, hours, label):
        try:
            record = json.loads(path.read_text())
            age = (now - datetime.fromisoformat(record[field])).total_seconds()
            result[label] = {'age_seconds': round(age), 'maximum_age_seconds': hours * 3600}
            if age < -60 or age > hours * 3600:
                issues.append(label + ' is stale')
            return record
        except (OSError, ValueError, KeyError, TypeError):
            issues.append(label + ' is missing or invalid')

    if role == 'primary':
        import psycopg2
        try:
            with psycopg2.connect(host='127.0.0.1', port=55440, user='postgres', dbname='postgres',
                    password=(config / 'postgres-password').read_text().strip(), connect_timeout=5) as connection:
                with connection.cursor() as cursor:
                    cursor.execute("SET statement_timeout='10s'")
                    cursor.execute('SHOW synchronous_standby_names')
                    required = cursor.fetchone()[0]
                    cursor.execute("SELECT rolname,rolconfig FROM pg_roles WHERE rolname IN ('innopool_runtime','innopool_owner')")
                    roles = dict(cursor.fetchall())
                    protected = (required == 'FIRST 1 (innopool_wal_archive)' and len(roles) == 2
                                 and all('synchronous_commit=on' in (value or []) for value in roles.values()))
                    result['primary_commits_wait_for_recovery'] = protected
                    if not protected:
                        issues.append('financial database commit acknowledgement is not protected by the recovery stream')
                    cursor.execute("""SELECT s.active,s.wal_status,
                        pg_wal_lsn_diff(pg_current_wal_lsn(),r.flush_lsn)::bigint,
                        extract(epoch FROM clock_timestamp()-r.reply_time)::float,
                        pg_wal_lsn_diff(pg_current_wal_lsn(),s.restart_lsn)::bigint
                        FROM pg_replication_slots s LEFT JOIN pg_stat_replication r ON r.pid=s.active_pid
                        WHERE s.slot_name='innopool_germany_archive'""")
                    row = cursor.fetchone()
            if not row:
                issues.append('continuous backup replication slot is missing')
            else:
                active, status, behind, reply_age, retained = row
                result['wal'] = {'active': active, 'slot_status': status, 'bytes_behind': behind,
                                 'reply_age_seconds': reply_age, 'retained_primary_bytes': retained}
                if not active or status not in ('reserved', 'extended'):
                    issues.append('continuous backup stream is unavailable; inspect before resuming work')
                if behind is None or behind > 64 * 1024**2 or reply_age is None or reply_age > 30:
                    issues.append('continuous backup stream is behind or its heartbeat is stale')
                if retained is None or retained > 3 * 1024**3:
                    issues.append('replication slot is approaching its 4 GiB retention limit')
        except Exception as error:
            issues.append('continuous backup query failed: ' + type(error).__name__)
        exports = Path('/var/lib/innopool-v2-mainnet-backups')
        age_file(exports / 'latest-configuration.json', 'created_at_utc', 30,
                 'encrypted configuration backup')
        names = ('config-backup',)
    elif role == 'recovery':
        names = ('basebackup', 'evidence-pull', 'backup-pull', 'evidence-verify')
        for name in ('wal-tunnel', 'wal', 'basebackup.timer', 'evidence-pull.timer', 'backup-pull.timer'):
            unit = 'innopoolv2mainnet-' + name + ('' if name.endswith('.timer') else '.service')
            state = subprocess.run(['systemctl', 'is-active', unit], capture_output=True,
                                   text=True, timeout=10).stdout.strip()
            if state != 'active':
                issues.append(unit + ' is not active')
        age_file(data / 'primary-continuous/latest.json', 'completed_at_utc', 30, 'verified database backup')
        age_file(data / 'primary-evidence/status.json', 'completed_at_utc', 1/3, 'primary evidence copy')
        age_file(data / 'primary-backups/latest-configuration.json', 'created_at_utc', 30,
                 'off-host encrypted configuration')
        primary = age_file(data / 'primary-backups/continuous-health.json', 'checked_at_utc', .25,
                           'primary backup health')
        if primary and primary.get('issues'):
            issues.append('primary reports a backup protection issue')
        if primary:
            result['primary_commits_wait_for_recovery'] = bool(primary.get('primary_commits_wait_for_recovery'))
    else:
        raise ValueError('unknown deployment role')
    for name in names:
        status = subprocess.run(['systemctl', 'show', 'innopoolv2mainnet-' + name + '.service',
                                 '--value', '-p', 'Result'], capture_output=True, text=True, timeout=10)
        if status.returncode or status.stdout.strip() != 'success':
            issues.append(name + ' last run failed; inspect its protected service journal')
    if role == 'primary':
        # The restricted pull carries this non-secret record to Germany. Publish
        # only after every check, including failed recent timer executions.
        path = Path('/var/lib/innopool-v2-mainnet-backups/continuous-health.json')
        atomic_json(path, result)
        os.chown(path, 0, grp.getgrnam('innopoolbackup').gr_gid)
        path.chmod(0o640)
    return result
